"""
Agent Task Queue Server

A FIFO queue for serializing expensive build operations (Gradle, Docker, etc.)
across multiple AI agents. Prevents resource contention by ensuring only one
heavy task runs at a time per queue.
"""

import argparse
import asyncio
import json
import os
import signal
import sqlite3
import time
from contextlib import contextmanager
from datetime import datetime, timedelta
from pathlib import Path

from fastmcp import FastMCP
from fastmcp.server.dependencies import get_context


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
DATA_DIR = Path(_args.data_dir)
OUTPUT_DIR = DATA_DIR / "output"
DB_PATH = DATA_DIR / "queue.db"
METRICS_PATH = DATA_DIR / "agent-task-queue-logs.json"
MAX_METRICS_SIZE_MB = _args.max_log_size
MAX_OUTPUT_FILES = _args.max_output_files
TAIL_LINES_ON_FAILURE = _args.tail_lines
SERVER_NAME = "Task Queue"
MAX_LOCK_AGE_MINUTES = _args.lock_timeout

# Polling intervals (configurable via environment)
POLL_INTERVAL_WAITING = float(os.environ.get("TASK_QUEUE_POLL_WAITING", "1"))
POLL_INTERVAL_READY = float(os.environ.get("TASK_QUEUE_POLL_READY", "1"))

mcp = FastMCP(SERVER_NAME)


# --- Database & Logging ---
@contextmanager
def get_db():
    """Get database connection with WAL mode for better concurrency."""
    conn = sqlite3.connect(DB_PATH, timeout=60.0)
    conn.execute("PRAGMA journal_mode=WAL")
    conn.execute("PRAGMA busy_timeout=60000")
    conn.row_factory = sqlite3.Row
    try:
        yield conn
        conn.commit()
    except Exception:
        conn.rollback()
        raise
    finally:
        conn.close()


def init_db():
    """Initialize DB with PID columns for process tracking."""
    DATA_DIR.mkdir(parents=True, exist_ok=True)
    with get_db() as conn:
        conn.execute("""
            CREATE TABLE IF NOT EXISTS queue (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                queue_name TEXT NOT NULL,
                status TEXT NOT NULL,
                pid INTEGER,
                child_pid INTEGER,
                created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
                updated_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP
            )
        """)
        conn.execute("""
            CREATE INDEX IF NOT EXISTS idx_queue_status
            ON queue(queue_name, status)
        """)


def log_fmt(msg: str) -> str:
    """Format log message with timestamp."""
    timestamp = datetime.now().strftime("%H:%M:%S")
    return f"[{timestamp}] [TASK-QUEUE] {msg}"


def log_metric(event: str, **kwargs):
    """
    Append a JSON metric entry to the log file.
    Rotates log file when it exceeds MAX_METRICS_SIZE_MB.
    """
    DATA_DIR.mkdir(parents=True, exist_ok=True)

    # Rotate if file exceeds size limit
    if METRICS_PATH.exists():
        size_mb = METRICS_PATH.stat().st_size / (1024 * 1024)
        if size_mb > MAX_METRICS_SIZE_MB:
            rotated = METRICS_PATH.with_suffix(".json.1")
            METRICS_PATH.rename(rotated)

    entry = {
        "event": event,
        "timestamp": datetime.now().isoformat(),
        **kwargs,
    }
    with open(METRICS_PATH, "a") as f:
        f.write(json.dumps(entry) + "\n")


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


# --- Process Liveness Logic ---
def is_process_alive(pid: int) -> bool:
    """Check if a process ID exists on the host OS."""
    if pid is None:
        return False
    try:
        os.kill(pid, 0)
        return True
    except OSError:
        return False


def kill_process_tree(pid: int):
    """Kill a process and all its children by killing the process group."""
    if not pid or not is_process_alive(pid):
        return
    try:
        # Kill the entire process group (works because we use start_new_session=True)
        os.killpg(pid, signal.SIGTERM)
    except OSError:
        # Fallback: try killing just the process if process group kill fails
        try:
            os.kill(pid, signal.SIGTERM)
        except OSError:
            pass


def cleanup_queue(conn, queue_name: str):
    """
    Active Cleanup:
    1. Check if MCP server holding lock is alive
    2. If not, kill orphaned child process
    3. Check for timeouts
    """
    # Check for dead parents
    runners = conn.execute(
        "SELECT id, pid, child_pid FROM queue WHERE queue_name = ? AND status = 'running'",
        (queue_name,),
    ).fetchall()

    for runner in runners:
        if not is_process_alive(runner["pid"]):
            child = runner["child_pid"]
            if child and is_process_alive(child):
                print(
                    log_fmt(
                        f"WARNING: Parent PID {runner['pid']} died. Killing orphan child PID {child}..."
                    )
                )
                kill_process_tree(child)

            conn.execute("DELETE FROM queue WHERE id = ?", (runner["id"],))
            log_metric(
                "zombie_cleared",
                task_id=runner["id"],
                queue_name=queue_name,
                dead_pid=runner["pid"],
                reason="parent_died",
            )
            print(log_fmt(f"WARNING: Cleared zombie lock (ID: {runner['id']})."))

    # Check for timeouts
    cutoff = (datetime.now() - timedelta(minutes=MAX_LOCK_AGE_MINUTES)).isoformat()
    stale = conn.execute(
        "SELECT id, child_pid FROM queue WHERE queue_name = ? AND status = 'running' AND updated_at < ?",
        (queue_name, cutoff),
    ).fetchall()

    for task in stale:
        if task["child_pid"]:
            kill_process_tree(task["child_pid"])
        conn.execute("DELETE FROM queue WHERE id = ?", (task["id"],))
        log_metric(
            "zombie_cleared",
            task_id=task["id"],
            queue_name=queue_name,
            reason="timeout",
            timeout_minutes=MAX_LOCK_AGE_MINUTES,
        )
        print(
            log_fmt(
                f"WARNING: Cleared stale lock (ID: {task['id']}) active > {MAX_LOCK_AGE_MINUTES}m"
            )
        )


# --- Core Queue Logic ---
def ensure_db():
    """Ensure database exists and is valid. Recreates if corrupted."""
    try:
        with get_db() as conn:
            # Quick check that table exists
            conn.execute("SELECT 1 FROM queue LIMIT 1")
    except sqlite3.OperationalError:
        # Database missing or corrupted - clean up and reinitialize
        for suffix in ["", "-wal", "-shm"]:
            path = Path(str(DB_PATH) + suffix)
            if path.exists():
                try:
                    path.unlink()
                except OSError:
                    pass
        init_db()


async def wait_for_turn(queue_name: str) -> int:
    """Register task, wait for turn, return task ID when acquired."""
    # Ensure database exists and is valid
    ensure_db()

    my_pid = os.getpid()
    ctx = None
    try:
        ctx = get_context()
    except LookupError:
        pass  # Running outside request context (e.g., in tests)

    with get_db() as conn:
        cursor = conn.execute(
            "INSERT INTO queue (queue_name, status, pid) VALUES (?, ?, ?)",
            (queue_name, "waiting", my_pid),
        )
        task_id = cursor.lastrowid

    log_metric("task_queued", task_id=task_id, queue_name=queue_name, pid=my_pid)
    queued_at = time.time()

    if ctx:
        await ctx.info(
            log_fmt(f"Request #{task_id} received. Entering '{queue_name}' queue.")
        )

    last_pos = -1
    wait_ticks = 0

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
                elif wait_ticks % 3 == 0 and ctx:  # Update every ~15 seconds
                    await ctx.info(
                        log_fmt(
                            f"Still waiting... Position #{pos} ({wait_ticks * 5}s elapsed)"
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


async def release_lock(task_id: int):
    """Release a queue lock."""
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
        timeout_seconds: Max runtime before killing the task (default: 1200 = 20 mins).
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

    start = time.time()
    stdout_lines = []
    stderr_lines = []

    try:
        proc = await asyncio.create_subprocess_shell(
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

        async def stream_output(stream, lines: list):
            """Read lines from stream (no real-time streaming to save tokens)."""
            while True:
                line = await stream.readline()
                if not line:
                    break
                lines.append(line.decode().rstrip())

        try:
            # Collect stdout and stderr concurrently (no streaming to save tokens)
            await asyncio.wait_for(
                asyncio.gather(
                    stream_output(proc.stdout, stdout_lines),
                    stream_output(proc.stderr, stderr_lines),
                ),
                timeout=timeout_seconds,
            )
            await proc.wait()
            duration = time.time() - start

            log_metric(
                "task_completed",
                task_id=task_id,
                queue_name=queue_name,
                command=command,
                exit_code=proc.returncode,
                duration_seconds=round(duration, 2),
                stdout_lines=len(stdout_lines),
                stderr_lines=len(stderr_lines),
            )

            # Write full output to file
            OUTPUT_DIR.mkdir(parents=True, exist_ok=True)
            output_file = OUTPUT_DIR / f"task_{task_id}.log"
            with open(output_file, "w") as f:
                f.write(f"COMMAND: {command}\n")
                f.write(f"EXIT CODE: {proc.returncode}\n")
                f.write(f"DURATION: {duration:.1f}s\n")
                f.write(f"WORKING DIR: {working_directory}\n")
                f.write("\n--- STDOUT ---\n")
                f.write("\n".join(stdout_lines))
                f.write("\n\n--- STDERR ---\n")
                f.write("\n".join(stderr_lines))
            cleanup_output_files()

            # Return concise summary for agents
            if proc.returncode == 0:
                return f"SUCCESS exit=0 {duration:.1f}s output={output_file}"
            else:
                # On failure, include tail of output for context
                tail = (
                    stderr_lines[-TAIL_LINES_ON_FAILURE:]
                    if stderr_lines
                    else stdout_lines[-TAIL_LINES_ON_FAILURE:]
                )
                tail_text = "\n".join(tail) if tail else "(no output)"
                return f"FAILED exit={proc.returncode} {duration:.1f}s output={output_file}\n{tail_text}"

        except asyncio.TimeoutError:
            try:
                # Kill entire process group to ensure all child processes die
                os.killpg(proc.pid, signal.SIGKILL)
                await proc.wait()
            except Exception:
                pass
            log_metric(
                "task_timeout",
                task_id=task_id,
                queue_name=queue_name,
                command=command,
                timeout_seconds=timeout_seconds,
            )
            # Write partial output to file
            OUTPUT_DIR.mkdir(parents=True, exist_ok=True)
            output_file = OUTPUT_DIR / f"task_{task_id}.log"
            with open(output_file, "w") as f:
                f.write(f"COMMAND: {command}\n")
                f.write(f"EXIT CODE: TIMEOUT (killed after {timeout_seconds}s)\n")
                f.write(f"WORKING DIR: {working_directory}\n")
                f.write("\n--- STDOUT ---\n")
                f.write("\n".join(stdout_lines))
                f.write("\n\n--- STDERR ---\n")
                f.write("\n".join(stderr_lines))
            cleanup_output_files()

            tail = (
                stderr_lines[-TAIL_LINES_ON_FAILURE:]
                if stderr_lines
                else stdout_lines[-TAIL_LINES_ON_FAILURE:]
            )
            tail_text = "\n".join(tail) if tail else "(no output)"
            return f"TIMEOUT killed after {timeout_seconds}s output={output_file}\n{tail_text}"

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

if __name__ == "__main__":
    mcp.run()
