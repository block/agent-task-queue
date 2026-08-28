"""
Agent Task Queue Server

A FIFO queue for serializing expensive build operations (Gradle, Docker, etc.)
across multiple AI agents. Prevents resource contention by ensuring only one
heavy task runs at a time per queue.
"""

import argparse
import asyncio
import codecs
import json
import os
import resource
import signal
import sqlite3
import sys
import time
import threading
import uuid
from collections import deque
from datetime import datetime, timezone
from pathlib import Path

from fastmcp import FastMCP
from fastmcp.server.dependencies import get_context
from fastmcp.tools.tool import ToolResult
from mcp.types import TextContent

# Import shared queue infrastructure
from queue_core import (
    QueuePaths,
    TaskOrigin,
    get_db as _get_db,
    init_db as _init_db,
    ensure_db as _ensure_db,
    cleanup_queue as _cleanup_queue,
    cleanup_targets_for_queue,
    collect_task_origin,
    log_metric as _log_metric,
    log_fmt,
    is_process_alive,
    kill_process_tree,
    insert_waiting_task,
    normalize_queue_name,
    parse_queue_capacities,
    attempt_task_start,
    task_origin_kwargs,
    POLL_INTERVAL_WAITING,
)

# Unique identifier for this server instance - used to detect orphaned tasks
# from previous server instances even if the PID is reused
SERVER_INSTANCE_ID = str(uuid.uuid4())[:8]

# Track active task IDs being processed by this server instance
# Used to detect orphaned queue entries when clients disconnect without proper cleanup
_active_task_ids: set[int] = set()
_active_task_ids_lock = threading.Lock()

# Background command tasks are deliberately independent from individual MCP requests. Cancelling
# a run_task/task_status request only stops that wait; cancel_task is the explicit command cancel.
_background_tasks: dict[int, asyncio.Task] = {}

MAX_TOOL_WAIT_SECONDS = 30
STATUS_POLL_INTERVAL_SECONDS = 0.25
PROGRESS_INTERVAL_SECONDS = 5
MAX_INLINE_OUTPUT_BYTES = 16 * 1024


# --- Argument Parsing ---
def parse_args():
    parser = argparse.ArgumentParser(
        description="Agent Task Queue - FIFO queue for serializing build operations"
    )
    parser.add_argument(
        "--data-dir",
        type=str,
        default=os.environ.get("TASK_QUEUE_DATA_DIR", "/tmp/agent-task-queue"),
        help="Directory for database and logs (default: /tmp/agent-task-queue)",
    )
    parser.add_argument(
        "--max-log-size",
        type=int,
        default=5,
        help="Max metrics log size in MB before rotation (default: 5)",
    )
    parser.add_argument(
        "--max-output-files",
        type=int,
        default=50,
        help="Number of task output files to retain (default: 50)",
    )
    parser.add_argument(
        "--tail-lines",
        type=int,
        default=50,
        help="Lines of output to include on failure (default: 50)",
    )
    parser.add_argument(
        "--lock-timeout",
        type=int,
        default=120,
        help="Minutes before stale locks are cleared (default: 120)",
    )
    parser.add_argument(
        "--queue-capacity",
        action="append",
        default=[],
        metavar="SCOPE=CAPACITY",
        help=(
            "Hierarchical queue capacity override. Repeatable. "
            "Example: --queue-capacity=gradle=2 --queue-capacity=gradle/emu-5557=1"
        ),
    )
    return parser.parse_args()


def _should_parse_module_args(argv0: str | None = None, module_name: str | None = None) -> bool:
    """Return True when the module is being launched as the task queue server."""
    module_name = module_name or __name__
    if module_name == "__main__":
        return True

    executable = Path(argv0 or sys.argv[0]).name
    return executable in {"agent-task-queue", "task_queue", "task_queue.py"}


# Parse args at module load (before MCP server starts)
_args = parse_args() if _should_parse_module_args() else argparse.Namespace(
    data_dir=os.environ.get("TASK_QUEUE_DATA_DIR", "/tmp/agent-task-queue"),
    max_log_size=5,
    max_output_files=50,
    tail_lines=50,
    lock_timeout=120,
    queue_capacity=[],
)

# --- Configuration ---
PATHS = QueuePaths.from_data_dir(Path(_args.data_dir))
OUTPUT_DIR = PATHS.output_dir
MAX_METRICS_SIZE_MB = _args.max_log_size
MAX_OUTPUT_FILES = _args.max_output_files
TAIL_LINES_ON_FAILURE = _args.tail_lines
SERVER_NAME = "Task Queue"
MAX_LOCK_AGE_MINUTES = _args.lock_timeout
QUEUE_CAPACITIES = parse_queue_capacities(_args.queue_capacity)

mcp = FastMCP(SERVER_NAME)


# --- Wrappers for shared functions (use module-level paths) ---
def get_db():
    """Get database connection using configured path."""
    return _get_db(PATHS.db_path)


def init_db():
    """Initialize database using configured paths."""
    _init_db(PATHS)


def ensure_db():
    """Ensure database exists and is valid using configured paths."""
    _ensure_db(PATHS)


def log_metric(event: str, **kwargs):
    """Log metric using configured paths."""
    PATHS.data_dir.mkdir(parents=True, exist_ok=True)
    _log_metric(PATHS.metrics_path, event, MAX_METRICS_SIZE_MB, **kwargs)


def _current_context():
    """Best-effort FastMCP request context; unavailable in tests and background codepaths."""
    try:
        return get_context()
    except (LookupError, RuntimeError):
        return None


def _current_client_id() -> str | None:
    ctx = _current_context()
    return ctx.client_id if ctx and ctx.client_id else None


def cleanup_queue(conn, queue_name: str, queue_capacities: dict[str, int] | None = None):
    """Clean up queue using configured paths and detect orphaned tasks."""
    if queue_capacities is None:
        queue_capacities = QUEUE_CAPACITIES

    for target_queue in cleanup_targets_for_queue(conn, queue_name, queue_capacities):
        _cleanup_queue(
            conn,
            target_queue,
            PATHS.metrics_path,
            MAX_LOCK_AGE_MINUTES,
            log_fn=lambda msg: print(log_fmt(msg)),
        )

        my_pid = os.getpid()

        # Cleanup 1: Tasks with our PID but DIFFERENT server_id (from old server instance)
        # This handles the edge case where PID is reused after server restart
        stale_server_tasks = conn.execute(
            "SELECT id, status, child_pid, server_id FROM queue WHERE queue_name = ? AND pid = ? AND server_id IS NOT NULL AND server_id != ?",
            (target_queue, my_pid, SERVER_INSTANCE_ID),
        ).fetchall()

        for task in stale_server_tasks:
            if task["child_pid"] and is_process_alive(task["child_pid"]):
                print(log_fmt(f"WARNING: Killing orphaned subprocess {task['child_pid']} from old server"))
                kill_process_tree(task["child_pid"])

            conn.execute("DELETE FROM queue WHERE id = ?", (task["id"],))
            log_metric(
                "orphan_cleared",
                task_id=task["id"],
                queue_name=target_queue,
                status=task["status"],
                old_server_id=task["server_id"],
                reason="stale_server_instance",
            )
            print(log_fmt(f"WARNING: Cleared task from old server instance (ID: {task['id']}, old_server: {task['server_id']})"))

        # Cleanup 2: Tasks with our PID AND server_id but not in active tracking set
        # This catches tasks left behind when clients disconnect without proper cleanup
        our_tasks = conn.execute(
            "SELECT id, status, child_pid FROM queue WHERE queue_name = ? AND pid = ? AND (server_id = ? OR server_id IS NULL)",
            (target_queue, my_pid, SERVER_INSTANCE_ID),
        ).fetchall()

        with _active_task_ids_lock:
            active_ids = _active_task_ids.copy()

        for orphan in our_tasks:
            if orphan["id"] not in active_ids:
                # This task belongs to us but we're not tracking it - it's orphaned
                if orphan["child_pid"] and is_process_alive(orphan["child_pid"]):
                    print(log_fmt(f"WARNING: Killing orphaned subprocess {orphan['child_pid']}"))
                    kill_process_tree(orphan["child_pid"])

                conn.execute("DELETE FROM queue WHERE id = ?", (orphan["id"],))
                log_metric(
                    "orphan_cleared",
                    task_id=orphan["id"],
                    queue_name=target_queue,
                    status=orphan["status"],
                    reason="not_in_active_set",
                )
                print(log_fmt(f"WARNING: Cleared orphaned task (ID: {orphan['id']}, status: {orphan['status']})"))

    if conn.in_transaction:
        conn.commit()


# --- Output File Management ---
def cleanup_output_files():
    """Remove the oldest task output groups and terminal results if over the limit."""
    old_task_ids = []
    retained_task_count = max(0, MAX_OUTPUT_FILES)
    if OUTPUT_DIR.exists():
        formatted_logs = sorted(
            (
                path
                for path in OUTPUT_DIR.glob("task_*.log")
                if not path.name.endswith(".raw.log")
            ),
            key=lambda path: path.stat().st_mtime,
        )
        if len(formatted_logs) > retained_task_count:
            old_logs = (
                formatted_logs
                if retained_task_count == 0
                else formatted_logs[:-retained_task_count]
            )
            old_task_ids = [
                path.name.removeprefix("task_").removesuffix(".log")
                for path in old_logs
            ]

    for task_id in old_task_ids:
        for old_file in (
            OUTPUT_DIR / f"task_{task_id}.log",
            OUTPUT_DIR / f"task_{task_id}.raw.log",
        ):
            try:
                old_file.unlink()
            except OSError:
                pass

    with get_db() as conn:
        conn.execute(
            """DELETE FROM task_results
               WHERE task_id NOT IN (
                   SELECT task_id FROM task_results
                   ORDER BY completed_at DESC, task_id DESC
                   LIMIT ?
               )""",
            (retained_task_count,),
        )


def clear_output_files() -> int:
    """Delete all output files. Returns number of files deleted."""
    if not OUTPUT_DIR.exists():
        return 0

    count = 0
    for f in OUTPUT_DIR.glob("task_*"):
        try:
            f.unlink()
            count += 1
        except OSError:
            pass
    return count


def get_memory_mb() -> float:
    """Get current process memory usage in MB (RSS - resident set size)."""
    usage = resource.getrusage(resource.RUSAGE_SELF)
    # ru_maxrss is in bytes on Linux, kilobytes on macOS
    if os.uname().sysname == "Darwin":
        return usage.ru_maxrss / (1024 * 1024)  # KB to MB
    return usage.ru_maxrss / 1024  # bytes to MB on Linux


# --- Core Queue Logic ---
class TaskNoLongerActive(Exception):
    """Raised when an explicitly cancelled task disappears while waiting for its turn."""


async def register_task(
    queue_name: str,
    command: str,
    task_origin: TaskOrigin,
) -> int:
    """Register a task and return its durable ID without waiting for execution."""
    ensure_db()
    with get_db() as conn:
        cleanup_queue(conn, queue_name, QUEUE_CAPACITIES)
        task_id = insert_waiting_task(
            conn,
            queue_name,
            os.getpid(),
            SERVER_INSTANCE_ID,
            command=command,
            task_origin=task_origin,
        )

    # SQLite may reuse low IDs after the database is recreated. Never expose stale output under
    # a newly registered handle (most notably when this project tests its own queue server).
    for output_file in (
        OUTPUT_DIR / f"task_{task_id}.log",
        OUTPUT_DIR / f"task_{task_id}.raw.log",
    ):
        try:
            output_file.unlink()
        except OSError:
            pass

    with _active_task_ids_lock:
        _active_task_ids.add(task_id)

    log_metric(
        "task_queued",
        task_id=task_id,
        queue_name=queue_name,
        pid=os.getpid(),
        **task_origin_kwargs(task_origin),
    )

    ctx = _current_context()
    if ctx:
        await ctx.info(log_fmt(f"Request #{task_id} received. Entering '{queue_name}' queue."))
    return task_id


async def wait_for_turn(
    task_id: int,
    queue_name: str,
    task_origin: TaskOrigin,
) -> None:
    """Wait until an already-registered task acquires its queue slot."""
    queued_at = time.time()
    while True:
        try:
            with get_db() as conn:
                cleanup_queue(conn, queue_name, QUEUE_CAPACITIES)
                row = conn.execute(
                    "SELECT status FROM queue WHERE id = ?",
                    (task_id,),
                ).fetchone()
                if row is None:
                    raise TaskNoLongerActive(f"Task #{task_id} is no longer queued")

                started, _ = attempt_task_start(
                    conn,
                    task_id,
                    queue_name,
                    QUEUE_CAPACITIES,
                    os.getpid(),
                )
                if started:
                    wait_time = time.time() - queued_at
                    log_metric(
                        "task_started",
                        task_id=task_id,
                        queue_name=queue_name,
                        pid=os.getpid(),
                        wait_time_seconds=round(wait_time, 2),
                        **task_origin_kwargs(task_origin),
                    )
                    return
        except sqlite3.OperationalError as exc:
            if "database is locked" not in str(exc).lower():
                raise

        await asyncio.sleep(POLL_INTERVAL_WAITING)


async def release_lock(task_id: int):
    """Release a queue slot and remove the task from active tracking."""
    with _active_task_ids_lock:
        _active_task_ids.discard(task_id)

    try:
        with get_db() as conn:
            conn.execute("DELETE FROM queue WHERE id = ?", (task_id,))
    except sqlite3.OperationalError:
        pass


def _load_task_result(task_id: int) -> dict | None:
    ensure_db()
    with get_db() as conn:
        row = conn.execute(
            "SELECT result_json FROM task_results WHERE task_id = ?",
            (task_id,),
        ).fetchone()
    return json.loads(row["result_json"]) if row else None


def _store_task_result(result: dict) -> dict:
    """Persist a terminal result once; explicit cancellation wins completion races."""
    ensure_db()
    with get_db() as conn:
        conn.execute(
            "INSERT OR IGNORE INTO task_results (task_id, result_json) VALUES (?, ?)",
            (result["task_id"], json.dumps(result)),
        )
        row = conn.execute(
            "SELECT result_json FROM task_results WHERE task_id = ?",
            (result["task_id"],),
        ).fetchone()
    return json.loads(row["result_json"])


def _read_new_output(task_id: int, output_offset: int) -> tuple[str, int, float | None]:
    raw_output_file = OUTPUT_DIR / f"task_{task_id}.raw.log"
    if not raw_output_file.exists():
        return "", 0, None

    stat = raw_output_file.stat()
    size = stat.st_size
    if output_offset > size:
        output_offset = 0
    start = output_offset
    omitted = 0
    if size - start > MAX_INLINE_OUTPUT_BYTES:
        start = size - MAX_INLINE_OUTPUT_BYTES
        omitted = start - output_offset

    with open(raw_output_file, "rb") as output:
        output.seek(start)
        text = output.read(size - start).decode(errors="replace").rstrip()

    if omitted:
        text = f"[... {omitted} earlier output bytes omitted ...]\n{text}"
    return text, size, round(max(0.0, time.time() - stat.st_mtime), 1)


def _active_task_snapshot(task_id: int, output_offset: int, include_output: bool = True) -> dict:
    with get_db() as conn:
        row = conn.execute("SELECT * FROM queue WHERE id = ?", (task_id,)).fetchone()
        if row is None:
            return {
                "task_id": task_id,
                "status": "unknown",
                "message": "Task is not active and no terminal result is available",
            }
        position = None
        if row["status"] == "waiting":
            position = conn.execute(
                """SELECT COUNT(*) + 1 AS position FROM queue
                   WHERE queue_name = ? AND status = 'waiting' AND id < ?""",
                (row["queue_name"], task_id),
            ).fetchone()["position"]

    created_at = datetime.fromisoformat(str(row["created_at"]).replace(" ", "T"))
    utc_now = datetime.now(timezone.utc).replace(tzinfo=None)
    elapsed_seconds = max(0.0, (utc_now - created_at).total_seconds())
    new_output, next_output_offset, output_age = (
        _read_new_output(task_id, output_offset) if include_output else ("", output_offset, None)
    )
    external_status = "queued" if row["status"] == "waiting" else "running"
    return {
        "task_id": task_id,
        "status": external_status,
        "queue_name": row["queue_name"],
        "queue_position": position,
        "elapsed_seconds": round(elapsed_seconds, 1),
        "command": row["command"],
        "working_directory": row["working_directory"],
        "process_alive": (
            is_process_alive(row["child_pid"])
            if row["status"] == "running" and row["child_pid"]
            else None
        ),
        "output_file": str(OUTPUT_DIR / f"task_{task_id}.log"),
        "new_output": new_output,
        "next_output_offset": next_output_offset,
        "last_output_seconds_ago": output_age,
    }


def _task_snapshot(task_id: int, output_offset: int, include_output: bool = True) -> dict:
    result = _load_task_result(task_id)
    if result is None:
        return _active_task_snapshot(task_id, output_offset, include_output=include_output)

    new_output, next_output_offset, output_age = (
        _read_new_output(task_id, output_offset) if include_output else ("", output_offset, None)
    )
    return {
        **result,
        "new_output": new_output,
        "next_output_offset": next_output_offset,
        "last_output_seconds_ago": output_age,
    }


def _format_task_snapshot(snapshot: dict) -> ToolResult:
    status = snapshot["status"]
    task_id = snapshot["task_id"]
    output_file = snapshot.get("output_file")

    if status == "success":
        text = (
            f"SUCCESS task_id={task_id} exit=0 {snapshot['duration_seconds']:.1f}s "
            f"command={snapshot['command']} output={output_file}"
        )
    elif status == "failed":
        text = (
            f"FAILED task_id={task_id} exit={snapshot['exit_code']} "
            f"{snapshot['duration_seconds']:.1f}s command={snapshot['command']} "
            f"output={output_file}\n{snapshot['tail']}"
        )
    elif status == "timeout":
        text = (
            f"TIMEOUT task_id={task_id} killed after {snapshot['timeout_seconds']}s "
            f"command={snapshot['command']} output={output_file}\n{snapshot['tail']}"
        )
    elif status == "cancelled":
        text = (
            f"CANCELLED task_id={task_id} after {snapshot['duration_seconds']:.1f}s "
            f"command={snapshot['command']} output={output_file}"
        )
    elif status == "error":
        text = f"ERROR task_id={task_id}: {snapshot['error']} output={output_file}"
    elif status == "unknown":
        text = f"ERROR task_id={task_id}: {snapshot['message']}"
    else:
        details = [
            f"{status.upper()} task_id={task_id}",
            f"queue={snapshot['queue_name']}",
            f"elapsed={snapshot['elapsed_seconds']:.1f}s",
        ]
        if snapshot.get("queue_position") is not None:
            details.append(f"position={snapshot['queue_position']}")
        if snapshot.get("process_alive") is not None:
            details.append(f"process_alive={str(snapshot['process_alive']).lower()}")
        if snapshot.get("last_output_seconds_ago") is not None:
            details.append(f"last_output={snapshot['last_output_seconds_ago']:.1f}s_ago")
        text = " ".join(details)
        if snapshot.get("new_output"):
            text += f"\n\n--- NEW OUTPUT ---\n{snapshot['new_output']}"
        else:
            text += "\n\n(no new output)"
        text += (
            "\n\nTask continues in the background. Do not rerun it. "
            f"Call task_status(task_id={task_id}, "
            f"output_offset={snapshot['next_output_offset']}) for the next update, "
            f"or cancel_task(task_id={task_id}) to stop it."
        )

    if status in {"success", "cancelled", "error"} and snapshot.get("new_output"):
        text += f"\n\n--- NEW OUTPUT ---\n{snapshot['new_output']}"

    return ToolResult(
        content=[TextContent(type="text", text=text)],
        structured_content={"result": snapshot},
    )


async def _report_progress(snapshot: dict) -> None:
    ctx = _current_context()
    if not ctx or snapshot["status"] not in {"queued", "running"}:
        return

    message = f"Task #{snapshot['task_id']} {snapshot['status']} for {snapshot['elapsed_seconds']:.0f}s"
    if snapshot.get("queue_position") is not None:
        message += f" (queue position {snapshot['queue_position']})"
    try:
        await ctx.report_progress(snapshot["elapsed_seconds"], message=message)
    except Exception:
        # Progress notifications are optional and unsupported clients must not break execution.
        pass


async def _wait_for_initial_result(task_id: int, wait_seconds: int) -> dict:
    deadline = time.monotonic() + wait_seconds
    next_progress = time.monotonic() + PROGRESS_INTERVAL_SECONDS
    while True:
        snapshot = _task_snapshot(task_id, 0, include_output=False)
        if snapshot["status"] not in {"queued", "running"}:
            return snapshot
        if time.monotonic() >= deadline:
            return _task_snapshot(task_id, 0)
        if time.monotonic() >= next_progress:
            await _report_progress(snapshot)
            next_progress = time.monotonic() + PROGRESS_INTERVAL_SECONDS
        remaining = max(0.0, deadline - time.monotonic())
        await asyncio.sleep(min(STATUS_POLL_INTERVAL_SECONDS, remaining))


async def _wait_for_status(task_id: int, wait_seconds: int, output_offset: int) -> dict:
    snapshot = _task_snapshot(task_id, output_offset)
    if snapshot["status"] not in {"queued", "running"} or snapshot.get("new_output"):
        return snapshot

    initial_status = snapshot["status"]
    deadline = time.monotonic() + wait_seconds
    next_progress = time.monotonic() + PROGRESS_INTERVAL_SECONDS
    while time.monotonic() < deadline:
        remaining = max(0.0, deadline - time.monotonic())
        await asyncio.sleep(min(STATUS_POLL_INTERVAL_SECONDS, remaining))
        snapshot = _task_snapshot(task_id, output_offset)
        if (
            snapshot["status"] not in {"queued", "running"}
            or snapshot["status"] != initial_status
            or snapshot.get("new_output")
        ):
            return snapshot
        if time.monotonic() >= next_progress:
            await _report_progress(snapshot)
            next_progress = time.monotonic() + PROGRESS_INTERVAL_SECONDS
    return snapshot


async def _terminate_process(proc: asyncio.subprocess.Process) -> None:
    if proc.returncode is not None:
        return
    kill_process_tree(proc.pid)
    try:
        await asyncio.wait_for(asyncio.shield(proc.wait()), timeout=5.0)
    except asyncio.TimeoutError:
        try:
            os.killpg(proc.pid, signal.SIGKILL)
        except OSError:
            try:
                os.kill(proc.pid, signal.SIGKILL)
            except OSError:
                pass
        await proc.wait()
    else:
        # The shell can exit before a signal-resistant descendant in its process group.
        try:
            os.killpg(proc.pid, 0)
            os.killpg(proc.pid, signal.SIGKILL)
        except OSError:
            pass


async def _terminate_external_process_group(pid: int) -> None:
    """Terminate a task owned by another MCP server, escalating after a grace period."""
    kill_process_tree(pid)
    await asyncio.sleep(1.0)
    try:
        os.killpg(pid, 0)
    except OSError:
        return
    try:
        os.killpg(pid, signal.SIGKILL)
    except OSError:
        try:
            os.kill(pid, signal.SIGKILL)
        except OSError:
            pass


async def _execute_command(
    task_id: int,
    queue_name: str,
    command: str,
    working_directory: str,
    timeout_seconds: int,
    env: dict[str, str],
    task_origin: TaskOrigin,
) -> dict:
    mem_before = get_memory_mb()
    start = time.time()
    stdout_tail: deque = deque(maxlen=TAIL_LINES_ON_FAILURE)
    stderr_tail: deque = deque(maxlen=TAIL_LINES_ON_FAILURE)
    stdout_count = 0
    stderr_count = 0

    OUTPUT_DIR.mkdir(parents=True, exist_ok=True)
    output_file = OUTPUT_DIR / f"task_{task_id}.log"
    raw_output_file = OUTPUT_DIR / f"task_{task_id}.raw.log"

    # nosec B602: shell execution is intentional. Users explicitly provide build commands and
    # shell features such as pipes, redirects, and globs are part of the tool contract.
    proc = await asyncio.create_subprocess_shell(  # nosec B602
        command,
        cwd=working_directory,
        env=env,
        stdout=asyncio.subprocess.PIPE,
        stderr=asyncio.subprocess.PIPE,
        start_new_session=True,
    )
    with get_db() as conn:
        conn.execute("UPDATE queue SET child_pid = ? WHERE id = ?", (proc.pid, task_id))

    with open(output_file, "w") as formatted, open(raw_output_file, "w") as raw:
        formatted.write(f"COMMAND: {command}\n")
        formatted.write(f"WORKING DIR: {working_directory}\n")
        formatted.write(f"STARTED: {datetime.now().isoformat()}\n")
        # Keep the historical markers for older output viewers. Output is interleaved because
        # both pipes must be drained concurrently to prevent a full stderr pipe from deadlocking.
        formatted.write("\n--- STDOUT ---\n--- STDERR ---\n")
        formatted.flush()

        async def stream_to_files(stream, tail_buffer: deque, label: str):
            """Drain one pipe concurrently so neither subprocess pipe can block the command."""
            nonlocal stdout_count, stderr_count
            decoder = codecs.getincrementaldecoder("utf-8")(errors="replace")
            while True:
                chunk = await stream.read(8192)
                if not chunk:
                    decoded = decoder.decode(b"", final=True)
                    if decoded:
                        formatted.write(decoded)
                        raw.write(decoded)
                    break
                decoded = decoder.decode(chunk)
                formatted.write(decoded)
                formatted.flush()
                raw.write(decoded)
                raw.flush()
                tail_buffer.extend(decoded.splitlines())
                line_count = max(1, decoded.count("\n"))
                if label == "stdout":
                    stdout_count += line_count
                else:
                    stderr_count += line_count

        stdout_task = asyncio.create_task(stream_to_files(proc.stdout, stdout_tail, "stdout"))
        stderr_task = asyncio.create_task(stream_to_files(proc.stderr, stderr_tail, "stderr"))
        process_task = asyncio.create_task(proc.wait())
        execution = asyncio.gather(stdout_task, stderr_task, process_task)

        try:
            await asyncio.wait_for(asyncio.shield(execution), timeout=timeout_seconds)
        except asyncio.TimeoutError:
            await _terminate_process(proc)
            await asyncio.gather(execution, return_exceptions=True)
            duration = time.time() - start
            formatted.write("\n--- SUMMARY ---\n")
            formatted.write(f"EXIT CODE: TIMEOUT (killed after {timeout_seconds}s)\n")
            formatted.write(f"DURATION: {duration:.1f}s\n")
            tail = list(stderr_tail) if stderr_tail else list(stdout_tail)
            tail_text = "\n".join(tail) if tail else "(no output)"
            log_metric(
                "task_timeout",
                task_id=task_id,
                queue_name=queue_name,
                pid=os.getpid(),
                command=command,
                timeout_seconds=timeout_seconds,
                memory_mb=round(get_memory_mb(), 1),
                **task_origin_kwargs(task_origin),
            )
            return {
                "task_id": task_id,
                "status": "timeout",
                "exit_code": None,
                "duration_seconds": round(duration, 1),
                "timeout_seconds": timeout_seconds,
                "command": command,
                "output_file": str(output_file),
                "tail": tail_text,
            }
        except asyncio.CancelledError:
            await _terminate_process(proc)
            await asyncio.gather(execution, return_exceptions=True)
            duration = time.time() - start
            formatted.write("\n--- SUMMARY ---\n")
            formatted.write("EXIT CODE: CANCELLED\n")
            formatted.write(f"DURATION: {duration:.1f}s\n")
            raise
        except Exception:
            await _terminate_process(proc)
            await asyncio.gather(execution, return_exceptions=True)
            raise

        duration = time.time() - start
        formatted.write("\n--- SUMMARY ---\n")
        formatted.write(f"EXIT CODE: {proc.returncode}\n")
        formatted.write(f"DURATION: {duration:.1f}s\n")

    mem_after = get_memory_mb()
    log_metric(
        "task_completed",
        task_id=task_id,
        queue_name=queue_name,
        pid=os.getpid(),
        command=command,
        exit_code=proc.returncode,
        duration_seconds=round(duration, 2),
        stdout_lines=stdout_count,
        stderr_lines=stderr_count,
        memory_before_mb=round(mem_before, 1),
        memory_after_mb=round(mem_after, 1),
        **task_origin_kwargs(task_origin),
    )
    tail = list(stderr_tail) if stderr_tail else list(stdout_tail)
    tail_text = "\n".join(tail) if tail else "(no output)"
    return {
        "task_id": task_id,
        "status": "success" if proc.returncode == 0 else "failed",
        "exit_code": proc.returncode,
        "duration_seconds": round(duration, 1),
        "command": command,
        "output_file": str(output_file),
        "tail": None if proc.returncode == 0 else tail_text,
    }


async def _run_registered_task(
    task_id: int,
    queue_name: str,
    command: str,
    working_directory: str,
    timeout_seconds: int,
    env: dict[str, str],
    task_origin: TaskOrigin,
) -> None:
    try:
        await wait_for_turn(task_id, queue_name, task_origin)
        result = await _execute_command(
            task_id,
            queue_name,
            command,
            working_directory,
            timeout_seconds,
            env,
            task_origin,
        )
        _store_task_result(result)
    except TaskNoLongerActive:
        if _load_task_result(task_id) is None:
            _store_task_result({
                "task_id": task_id,
                "status": "cancelled",
                "duration_seconds": 0.0,
                "command": command,
                "output_file": str(OUTPUT_DIR / f"task_{task_id}.log"),
            })
    except asyncio.CancelledError:
        if _load_task_result(task_id) is None:
            log_metric(
                "task_cancelled",
                task_id=task_id,
                queue_name=queue_name,
                pid=os.getpid(),
                command=command,
                reason="background_task_cancelled",
                **task_origin_kwargs(task_origin),
            )
            _store_task_result({
                "task_id": task_id,
                "status": "cancelled",
                "duration_seconds": 0.0,
                "command": command,
                "output_file": str(OUTPUT_DIR / f"task_{task_id}.log"),
            })
        raise
    except Exception as exc:
        log_metric(
            "task_error",
            task_id=task_id,
            queue_name=queue_name,
            pid=os.getpid(),
            command=command,
            error=str(exc),
            **task_origin_kwargs(task_origin),
        )
        _store_task_result({
            "task_id": task_id,
            "status": "error",
            "duration_seconds": 0.0,
            "command": command,
            "output_file": str(OUTPUT_DIR / f"task_{task_id}.log"),
            "error": str(exc),
        })
    finally:
        await release_lock(task_id)
        cleanup_output_files()


def _forget_background_task(task_id: int, completed: asyncio.Task) -> None:
    _background_tasks.pop(task_id, None)
    try:
        completed.exception()
    except asyncio.CancelledError:
        pass


def _start_background_task(
    task_id: int,
    queue_name: str,
    command: str,
    working_directory: str,
    timeout_seconds: int,
    env: dict[str, str],
    task_origin: TaskOrigin,
) -> None:
    background_task = asyncio.create_task(
        _run_registered_task(
            task_id,
            queue_name,
            command,
            working_directory,
            timeout_seconds,
            env,
            task_origin,
        ),
        name=f"queued-task-{task_id}",
    )
    _background_tasks[task_id] = background_task
    background_task.add_done_callback(
        lambda completed: _forget_background_task(task_id, completed)
    )


def _validate_wait_seconds(wait_seconds: int) -> str | None:
    if not 0 <= wait_seconds <= MAX_TOOL_WAIT_SECONDS:
        return f"wait_seconds must be between 0 and {MAX_TOOL_WAIT_SECONDS}"
    return None


# --- Tools ---
@mcp.tool(
    title="Run Queued Task",
    annotations={
        "destructiveHint": True,
        "openWorldHint": False,
        "idempotentHint": False,
    },
)
async def run_task(
    command: str,
    working_directory: str,
    queue_name: str = "global",
    timeout_seconds: int = 1200,
    env_vars: str = "",
    agent_name: str = "",
    wait_seconds: int = MAX_TOOL_WAIT_SECONDS,
):
    """
    Execute a command through the task queue for sequential processing.

    IMPORTANT: Before calling this tool, tell the user the exact command you are
    about to run. The tool returns the final result for short commands. After at
    most 30 seconds it returns a queued/running task handle with inline output;
    the command continues in the background. Use task_status with the returned
    task_id and next_output_offset for further updates. Do not rerun the command.

    Cancelling this tool's wait does not cancel the command. Use cancel_task for
    explicit command cancellation.

    When a command fails, analyze the output tail to identify the root cause and
    show the user the specific error with the responsible file/line if available.

    YOU MUST USE THIS TOOL instead of running shell commands directly when the
    command involves ANY of the following:

    BUILD TOOLS (always use this tool):
    - gradle, gradlew, ./gradlew, bazel, bazelisk, make, cmake, ninja
    - mvn, maven, cargo build, cargo test, go build, go test
    - npm run build, npm test, yarn build, pnpm build
    - dotnet build, dotnet test, msbuild

    CONTAINER/VM OPERATIONS (always use this tool):
    - docker build, docker-compose up, docker compose
    - podman build, podman-compose, kubectl apply, helm install

    PACKAGE OPERATIONS (always use this tool):
    - pip install, npm install, yarn install, pnpm install
    - bundle install, composer install

    TEST SUITES (always use this tool):
    - pytest, jest, mocha, rspec, or any full test suite

    WHY: Running multiple builds simultaneously causes system freeze and race
    conditions. This tool ensures only the configured number of heavy tasks run.

    Args:
        command: The full shell command to run.
        working_directory: ABSOLUTE path to the execution root.
        queue_name: Queue identifier (default: "global").
        timeout_seconds: Max execution time. Queue wait time does not count.
        env_vars: Environment variables as "KEY1=value1,KEY2=value2".
        agent_name: Optional caller label such as "amp".
        wait_seconds: Initial bounded wait, from 0 through 30 seconds (default: 30).

    Returns:
        A terminal result, or a queued/running handle for task_status.
    """
    if not command or not command.strip():
        return "ERROR: Command cannot be empty"
    if not os.path.exists(working_directory):
        return f"ERROR: Working directory does not exist: {working_directory}"
    if timeout_seconds < 1:
        return "ERROR: timeout_seconds must be at least 1"
    if wait_error := _validate_wait_seconds(wait_seconds):
        return f"ERROR: {wait_error}"

    try:
        queue_name = normalize_queue_name(queue_name)
    except ValueError as exc:
        return f"ERROR: {str(exc)}"

    env = os.environ.copy()
    if env_vars:
        for pair in env_vars.split(","):
            if "=" in pair:
                key, value = pair.split("=", 1)
                env[key.strip()] = value.strip()

    caller_name = agent_name.strip() or _current_client_id()
    task_origin = collect_task_origin(working_directory, caller_name)
    task_id = await register_task(queue_name, command, task_origin)
    _start_background_task(
        task_id,
        queue_name,
        command,
        working_directory,
        timeout_seconds,
        env,
        task_origin,
    )
    snapshot = await _wait_for_initial_result(task_id, wait_seconds)
    return _format_task_snapshot(snapshot)


@mcp.tool(
    title="Check Queued Task",
    annotations={
        "readOnlyHint": True,
        "openWorldHint": False,
        "idempotentHint": True,
    },
)
async def task_status(
    task_id: int,
    output_offset: int = 0,
    wait_seconds: int = MAX_TOOL_WAIT_SECONDS,
):
    """
    Wait for and return the next inline update from a background queued task.

    Returns when new output appears, task state changes, the task completes, or
    after at most 30 seconds as a liveness heartbeat. Pass next_output_offset from
    the previous result to avoid repeating output. Cancelling this status wait
    does not cancel the command; use cancel_task to stop it explicitly.
    """
    if task_id < 1:
        return "ERROR: task_id must be a positive integer"
    if output_offset < 0:
        return "ERROR: output_offset cannot be negative"
    if wait_error := _validate_wait_seconds(wait_seconds):
        return f"ERROR: {wait_error}"

    snapshot = await _wait_for_status(task_id, wait_seconds, output_offset)
    return _format_task_snapshot(snapshot)


@mcp.tool(
    title="Cancel Queued Task",
    annotations={
        "destructiveHint": True,
        "openWorldHint": False,
        "idempotentHint": True,
    },
)
async def cancel_task(task_id: int):
    """Explicitly cancel one queued or running background task by task ID."""
    if task_id < 1:
        return "ERROR: task_id must be a positive integer"

    existing_result = _load_task_result(task_id)
    if existing_result is not None:
        return _format_task_snapshot(_task_snapshot(task_id, 0))

    snapshot = _active_task_snapshot(task_id, 0)
    if snapshot["status"] == "unknown":
        return _format_task_snapshot(snapshot)

    result = _store_task_result({
        "task_id": task_id,
        "status": "cancelled",
        "duration_seconds": snapshot["elapsed_seconds"],
        "command": snapshot["command"],
        "output_file": snapshot["output_file"],
    })
    log_metric(
        "task_cancelled",
        task_id=task_id,
        queue_name=snapshot["queue_name"],
        pid=os.getpid(),
        command=snapshot["command"],
        reason="explicit_cancel",
    )

    background_task = _background_tasks.get(task_id)
    if background_task is not None:
        background_task.cancel()
        await asyncio.gather(background_task, return_exceptions=True)
    else:
        with get_db() as conn:
            row = conn.execute(
                "SELECT child_pid FROM queue WHERE id = ?",
                (task_id,),
            ).fetchone()
            if row and row["child_pid"]:
                await _terminate_external_process_group(row["child_pid"])
            conn.execute("DELETE FROM queue WHERE id = ?", (task_id,))

    return _format_task_snapshot({
        **result,
        "new_output": "",
        "next_output_offset": snapshot["next_output_offset"],
        "last_output_seconds_ago": snapshot["last_output_seconds_ago"],
    })


@mcp.tool()
async def clear_task_logs() -> str:
    """
    Delete all task output log files.

    Use this to free up disk space after reviewing build outputs.
    Log files are stored in /tmp/agent-task-queue/output/.

    Returns:
        Number of files deleted.
    """
    count = clear_output_files()
    return f"Deleted {count} log file(s) from {OUTPUT_DIR}"


# Initialize database on module load
init_db()


def main():
    """Entry point for uvx/CLI."""
    mcp.run()


if __name__ == "__main__":
    main()
