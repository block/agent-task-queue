"""
Agent Task Queue Server

A FIFO queue for serializing expensive build operations (Gradle, Docker, etc.)
across multiple AI agents. Prevents resource contention by ensuring only one
heavy task runs at a time per queue.
"""

import argparse
import asyncio
import os
import resource
import signal
import sqlite3
import time
import threading
import uuid
from collections import deque
from datetime import datetime
from pathlib import Path

from fastmcp import FastMCP
from fastmcp.server.dependencies import get_context

# Import shared queue infrastructure
from queue_core import (
    QueuePaths,
    get_db as _get_db,
    init_db as _init_db,
    ensure_db as _ensure_db,
    cleanup_queue as _cleanup_queue,
    log_metric as _log_metric,
    log_fmt,
    is_process_alive,
    kill_process_tree,
    POLL_INTERVAL_WAITING,
    POLL_INTERVAL_READY,
)

# Unique identifier for this server instance - used to detect orphaned tasks
# from previous server instances even if the PID is reused
SERVER_INSTANCE_ID = str(uuid.uuid4())[:8]

# Track active task IDs being processed by this server instance
# Used to detect orphaned queue entries when clients disconnect without proper cleanup
_active_task_ids: set[int] = set()
_active_task_ids_lock = threading.Lock()


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
    return parser.parse_args()


# Parse args at module load (before MCP server starts)
_args = parse_args() if __name__ == "__main__" else argparse.Namespace(
    data_dir="/tmp/agent-task-queue",
    max_log_size=5,
    max_output_files=50,
    tail_lines=50,
    lock_timeout=120,
)

# --- Configuration ---
PATHS = QueuePaths.from_data_dir(Path(_args.data_dir))
OUTPUT_DIR = PATHS.output_dir
MAX_METRICS_SIZE_MB = _args.max_log_size
MAX_OUTPUT_FILES = _args.max_output_files
TAIL_LINES_ON_FAILURE = _args.tail_lines
SERVER_NAME = "Task Queue"
MAX_LOCK_AGE_MINUTES = _args.lock_timeout

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


def cleanup_queue(conn, queue_name: str):
    """Clean up queue using configured paths and detect orphaned tasks."""
    _cleanup_queue(
        conn,
        queue_name,
        PATHS.metrics_path,
        MAX_LOCK_AGE_MINUTES,
        log_fn=lambda msg: print(log_fmt(msg)),
    )

    my_pid = os.getpid()

    # Cleanup 1: Tasks with our PID but DIFFERENT server_id (from old server instance)
    # This handles the edge case where PID is reused after server restart
    stale_server_tasks = conn.execute(
        "SELECT id, status, child_pid, server_id FROM queue WHERE queue_name = ? AND pid = ? AND server_id IS NOT NULL AND server_id != ?",
        (queue_name, my_pid, SERVER_INSTANCE_ID),
    ).fetchall()

    for task in stale_server_tasks:
        if task["child_pid"] and is_process_alive(task["child_pid"]):
            print(log_fmt(f"WARNING: Killing orphaned subprocess {task['child_pid']} from old server"))
            kill_process_tree(task["child_pid"])

        conn.execute("DELETE FROM queue WHERE id = ?", (task["id"],))
        log_metric(
            "orphan_cleared",
            task_id=task["id"],
            queue_name=queue_name,
            status=task["status"],
            old_server_id=task["server_id"],
            reason="stale_server_instance",
        )
        print(log_fmt(f"WARNING: Cleared task from old server instance (ID: {task['id']}, old_server: {task['server_id']})"))

    # Cleanup 2: Tasks with our PID AND server_id but not in active tracking set
    # This catches tasks left behind when clients disconnect without proper cleanup
    our_tasks = conn.execute(
        "SELECT id, status, child_pid FROM queue WHERE queue_name = ? AND pid = ? AND (server_id = ? OR server_id IS NULL)",
        (queue_name, my_pid, SERVER_INSTANCE_ID),
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
                queue_name=queue_name,
                status=orphan["status"],
                reason="not_in_active_set",
            )
            print(log_fmt(f"WARNING: Cleared orphaned task (ID: {orphan['id']}, status: {orphan['status']})"))


# --- Output File Management ---
def cleanup_output_files():
    """Remove oldest output files if over MAX_OUTPUT_FILES limit."""
    if not OUTPUT_DIR.exists():
        return

    files = sorted(OUTPUT_DIR.glob("task_*.log"), key=lambda f: f.stat().st_mtime)
    if len(files) > MAX_OUTPUT_FILES:
        for old_file in files[: len(files) - MAX_OUTPUT_FILES]:
            try:
                old_file.unlink()
            except OSError:
                pass


def clear_output_files() -> int:
    """Delete all output files. Returns number of files deleted."""
    if not OUTPUT_DIR.exists():
        return 0

    count = 0
    for f in OUTPUT_DIR.glob("task_*.log"):
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
async def wait_for_turn(queue_name: str) -> int:
    """Register task, wait for turn, return task ID when acquired."""
    # Ensure database exists and is valid
    ensure_db()

    # Run cleanup BEFORE inserting - this clears orphaned tasks that would otherwise
    # block the queue forever (since cleanup only runs during polling)
    with get_db() as conn:
        cleanup_queue(conn, queue_name)

    my_pid = os.getpid()
    ctx = None
    try:
        ctx = get_context()
    except LookupError:
        pass  # Running outside request context (e.g., in tests)

    with get_db() as conn:
        cursor = conn.execute(
            "INSERT INTO queue (queue_name, status, pid, server_id) VALUES (?, ?, ?, ?)",
            (queue_name, "waiting", my_pid, SERVER_INSTANCE_ID),
        )
        task_id = cursor.lastrowid

    # Track this task as active for orphan detection
    with _active_task_ids_lock:
        _active_task_ids.add(task_id)

    log_metric("task_queued", task_id=task_id, queue_name=queue_name, pid=my_pid)
    queued_at = time.time()

    if ctx:
        await ctx.info(
            log_fmt(f"Request #{task_id} received. Entering '{queue_name}' queue.")
        )

    last_pos = -1
    wait_ticks = 0

    try:
        while True:
            with get_db() as conn:
                cleanup_queue(conn, queue_name)

                runner = conn.execute(
                    "SELECT id FROM queue WHERE queue_name = ? AND status = 'running'",
                    (queue_name,),
                ).fetchone()

                if runner:
                    pos = (
                        conn.execute(
                            "SELECT COUNT(*) as c FROM queue WHERE queue_name = ? AND status = 'waiting' AND id < ?",
                            (queue_name, task_id),
                        ).fetchone()["c"]
                        + 1
                    )

                    wait_ticks += 1

                    if pos != last_pos:
                        if ctx:
                            await ctx.info(log_fmt(f"Position #{pos} in queue. Waiting..."))
                        last_pos = pos
                    elif wait_ticks % 10 == 0 and ctx:  # Update every ~10 polls
                        await ctx.info(
                            log_fmt(
                                f"Still waiting... Position #{pos} ({int(wait_ticks * POLL_INTERVAL_WAITING)}s elapsed)"
                            )
                        )

                    await asyncio.sleep(POLL_INTERVAL_WAITING)
                    continue

                # Atomic lock acquisition: UPDATE only succeeds if we're the first
                # waiting task AND no one is currently running. This prevents race
                # conditions where two tasks both think they're next.
                cursor = conn.execute(
                    """UPDATE queue SET status = 'running', updated_at = ?, pid = ?
                       WHERE id = ? AND status = 'waiting'
                       AND NOT EXISTS (
                           SELECT 1 FROM queue WHERE queue_name = ? AND status = 'running'
                       )
                       AND id = (
                           SELECT MIN(id) FROM queue WHERE queue_name = ? AND status = 'waiting'
                       )""",
                    (datetime.now().isoformat(), my_pid, task_id, queue_name, queue_name),
                )

                if cursor.rowcount > 0:
                    wait_time = time.time() - queued_at
                    log_metric(
                        "task_started",
                        task_id=task_id,
                        queue_name=queue_name,
                        wait_time_seconds=round(wait_time, 2),
                    )
                    if ctx:
                        await ctx.info(log_fmt("Lock ACQUIRED. Starting execution."))
                    return task_id

            await asyncio.sleep(POLL_INTERVAL_READY)
    except asyncio.CancelledError:
        # Client disconnected (e.g., sub-agent cancelled) - clean up our queue entry
        with _active_task_ids_lock:
            _active_task_ids.discard(task_id)
        log_metric(
            "task_cancelled",
            task_id=task_id,
            queue_name=queue_name,
            reason="client_disconnected",
        )
        with get_db() as conn:
            conn.execute("DELETE FROM queue WHERE id = ?", (task_id,))
        raise  # Re-raise to propagate cancellation


async def release_lock(task_id: int):
    """Release a queue lock."""
    # Remove from active tracking
    with _active_task_ids_lock:
        _active_task_ids.discard(task_id)

    ctx = None
    try:
        ctx = get_context()
    except LookupError:
        pass

    try:
        with get_db() as conn:
            conn.execute("DELETE FROM queue WHERE id = ?", (task_id,))
    except sqlite3.OperationalError:
        # Database was deleted (e.g., by tests) - nothing to release
        pass

    if ctx:
        await ctx.info(log_fmt("Task complete. Queue slot released."))


# --- The Tool ---
@mcp.tool()
async def run_task(
    command: str,
    working_directory: str,
    queue_name: str = "global",
    timeout_seconds: int = 1200,
    env_vars: str = "",
) -> str:
    """
    Execute a command through the task queue for sequential processing.

    YOU MUST USE THIS TOOL instead of running shell commands directly when the
    command involves ANY of the following:

    BUILD TOOLS (always use this tool):
    - gradle, gradlew, ./gradlew (any Gradle command)
    - bazel, bazelisk (any Bazel command)
    - make, cmake, ninja
    - mvn, maven
    - cargo build, cargo test
    - go build, go test
    - npm run build, npm test, yarn build, pnpm build
    - dotnet build, dotnet test, msbuild

    CONTAINER/VM OPERATIONS (always use this tool):
    - docker build, docker-compose up, docker compose
    - podman build, podman-compose
    - kubectl apply, helm install

    PACKAGE OPERATIONS (always use this tool):
    - pip install (with compilation)
    - npm install, yarn install, pnpm install
    - bundle install
    - composer install

    TEST SUITES (always use this tool):
    - pytest, jest, mocha, rspec
    - Any command running a full test suite

    WHY: Running multiple builds simultaneously causes system freeze and race
    conditions. This tool ensures only one heavy task runs at a time using a
    FIFO queue.

    Args:
        command: The full shell command to run.
        working_directory: ABSOLUTE path to the execution root.
        queue_name: Queue identifier for grouping tasks (default: "global").
        timeout_seconds: Max **execution** time before killing the task (default: 1200 = 20 mins).
            Queue wait time does NOT count against this timeout.
        env_vars: Environment variables to set, format: "KEY1=value1,KEY2=value2"

    Returns:
        Command output including stdout, stderr, and exit code.
    """
    if not command or not command.strip():
        return "ERROR: Command cannot be empty"

    if not os.path.exists(working_directory):
        return f"ERROR: Working directory does not exist: {working_directory}"

    # Parse environment variables
    env = os.environ.copy()
    if env_vars:
        for pair in env_vars.split(","):
            if "=" in pair:
                key, value = pair.split("=", 1)
                env[key.strip()] = value.strip()

    task_id = await wait_for_turn(queue_name)
    mem_before = get_memory_mb()

    start = time.time()
    # Use bounded deques - only keep last N lines in memory for error messages
    stdout_tail: deque = deque(maxlen=TAIL_LINES_ON_FAILURE)
    stderr_tail: deque = deque(maxlen=TAIL_LINES_ON_FAILURE)
    stdout_count = 0
    stderr_count = 0

    # Create output file early and stream directly to it
    OUTPUT_DIR.mkdir(parents=True, exist_ok=True)
    output_file = OUTPUT_DIR / f"task_{task_id}.log"

    try:
        # nosec B602: shell execution is intentional - this MCP tool executes user-provided
        # build commands (gradle, docker, pytest, etc.). Shell features (pipes, redirects,
        # globs) are required. Input comes from AI agents which users explicitly invoke.
        proc = await asyncio.create_subprocess_shell(  # nosec B602
            command,
            cwd=working_directory,
            env=env,
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.PIPE,
            start_new_session=True,  # Run in own process group for clean kill
        )

        # Record child PID for zombie protection
        with get_db() as conn:
            conn.execute(
                "UPDATE queue SET child_pid = ? WHERE id = ?", (proc.pid, task_id)
            )

        # Open file for streaming output - write header first
        with open(output_file, "w") as f:
            f.write(f"COMMAND: {command}\n")
            f.write(f"WORKING DIR: {working_directory}\n")
            f.write(f"STARTED: {datetime.now().isoformat()}\n")
            f.write("\n--- STDOUT ---\n")

            async def stream_to_file(stream, tail_buffer: deque, label: str):
                """Stream output directly to file, keeping only tail in memory."""
                nonlocal stdout_count, stderr_count
                while True:
                    line = await stream.readline()
                    if not line:
                        break
                    decoded = line.decode().rstrip()
                    f.write(decoded + "\n")
                    f.flush()  # Ensure immediate write to disk
                    tail_buffer.append(decoded)
                    if label == "stdout":
                        stdout_count += 1
                    else:
                        stderr_count += 1

            try:
                # Stream stdout first, then stderr (written sequentially to file)
                await asyncio.wait_for(
                    stream_to_file(proc.stdout, stdout_tail, "stdout"),
                    timeout=timeout_seconds,
                )
                f.write("\n--- STDERR ---\n")
                await asyncio.wait_for(
                    stream_to_file(proc.stderr, stderr_tail, "stderr"),
                    timeout=timeout_seconds,
                )
                await proc.wait()
                duration = time.time() - start

                # Append summary to file
                f.write("\n--- SUMMARY ---\n")
                f.write(f"EXIT CODE: {proc.returncode}\n")
                f.write(f"DURATION: {duration:.1f}s\n")

            except asyncio.TimeoutError:
                try:
                    os.killpg(proc.pid, signal.SIGKILL)
                    await proc.wait()
                except Exception:
                    pass
                f.write("\n--- SUMMARY ---\n")
                f.write(f"EXIT CODE: TIMEOUT (killed after {timeout_seconds}s)\n")

                log_metric(
                    "task_timeout",
                    task_id=task_id,
                    queue_name=queue_name,
                    command=command,
                    timeout_seconds=timeout_seconds,
                    memory_mb=round(get_memory_mb(), 1),
                )
                cleanup_output_files()

                tail = list(stderr_tail) if stderr_tail else list(stdout_tail)
                tail_text = "\n".join(tail) if tail else "(no output)"
                return f"TIMEOUT killed after {timeout_seconds}s output={output_file}\n{tail_text}"

        # File is now closed, log metrics
        mem_after = get_memory_mb()
        log_metric(
            "task_completed",
            task_id=task_id,
            queue_name=queue_name,
            command=command,
            exit_code=proc.returncode,
            duration_seconds=round(duration, 2),
            stdout_lines=stdout_count,
            stderr_lines=stderr_count,
            memory_before_mb=round(mem_before, 1),
            memory_after_mb=round(mem_after, 1),
        )
        cleanup_output_files()

        # Return concise summary for agents
        if proc.returncode == 0:
            return f"SUCCESS exit=0 {duration:.1f}s output={output_file}"
        else:
            # On failure, include tail of output for context
            tail = list(stderr_tail) if stderr_tail else list(stdout_tail)
            tail_text = "\n".join(tail) if tail else "(no output)"
            return f"FAILED exit={proc.returncode} {duration:.1f}s output={output_file}\n{tail_text}"

    except asyncio.CancelledError:
        # Client disconnected while task was running - kill the subprocess
        log_metric(
            "task_cancelled",
            task_id=task_id,
            queue_name=queue_name,
            command=command,
            reason="client_disconnected_during_execution",
        )
        try:
            os.killpg(proc.pid, signal.SIGTERM)
            await asyncio.wait_for(proc.wait(), timeout=5.0)
        except Exception:
            try:
                os.killpg(proc.pid, signal.SIGKILL)
            except Exception:
                pass
        raise  # Re-raise to propagate cancellation

    except Exception as e:
        log_metric(
            "task_error",
            task_id=task_id,
            queue_name=queue_name,
            command=command,
            error=str(e),
        )
        return f"ERROR: {str(e)}"

    finally:
        await release_lock(task_id)


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
