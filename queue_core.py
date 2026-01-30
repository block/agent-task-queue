"""
Queue Core - Shared infrastructure for agent-task-queue.

This module contains the shared logic used by both:
- task_queue.py (MCP server)
- tq.py (CLI tool)
"""

import json
import os
import signal
import sqlite3
from contextlib import contextmanager
from dataclasses import dataclass
from datetime import datetime, timedelta
from pathlib import Path


# --- Configuration ---
DEFAULT_DATA_DIR = Path(os.environ.get("TASK_QUEUE_DATA_DIR", "/tmp/agent-task-queue"))
POLL_INTERVAL_WAITING = float(os.environ.get("TASK_QUEUE_POLL_WAITING", "1"))
POLL_INTERVAL_READY = float(os.environ.get("TASK_QUEUE_POLL_READY", "1"))
DEFAULT_MAX_LOCK_AGE_MINUTES = 120
DEFAULT_MAX_METRICS_SIZE_MB = 5


@dataclass
class QueuePaths:
    """Paths for queue data files."""

    data_dir: Path
    db_path: Path
    metrics_path: Path
    output_dir: Path

    @classmethod
    def from_data_dir(cls, data_dir: Path) -> "QueuePaths":
        return cls(
            data_dir=data_dir,
            db_path=data_dir / "queue.db",
            metrics_path=data_dir / "agent-task-queue-logs.json",
            output_dir=data_dir / "output",
        )


# --- Database Schema ---
QUEUE_SCHEMA = """
CREATE TABLE IF NOT EXISTS queue (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    queue_name TEXT NOT NULL,
    status TEXT NOT NULL,
    pid INTEGER,
    server_id TEXT,
    child_pid INTEGER,
    command TEXT,
    created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
    updated_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP
)
"""

# Migration to add server_id column to existing databases
QUEUE_MIGRATION_SERVER_ID = """
ALTER TABLE queue ADD COLUMN server_id TEXT
"""

# Migration to add command column to existing databases
QUEUE_MIGRATION_COMMAND = """
ALTER TABLE queue ADD COLUMN command TEXT
"""

QUEUE_INDEX = """
CREATE INDEX IF NOT EXISTS idx_queue_status ON queue(queue_name, status)
"""


# --- Database Functions ---
@contextmanager
def get_db(db_path: Path):
    """Get database connection with WAL mode for better concurrency."""
    conn = sqlite3.connect(db_path, timeout=60.0)
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


def init_db(paths: QueuePaths):
    """Initialize DB with queue table."""
    paths.data_dir.mkdir(parents=True, exist_ok=True)
    with get_db(paths.db_path) as conn:
        conn.execute(QUEUE_SCHEMA)
        conn.execute(QUEUE_INDEX)
        # Run migrations for existing databases
        for migration in [QUEUE_MIGRATION_SERVER_ID, QUEUE_MIGRATION_COMMAND]:
            try:
                conn.execute(migration)
            except sqlite3.OperationalError:
                pass  # Column already exists


def ensure_db(paths: QueuePaths):
    """Ensure database exists and is valid. Recreates if corrupted."""
    try:
        with get_db(paths.db_path) as conn:
            conn.execute("SELECT 1 FROM queue LIMIT 1")
    except sqlite3.OperationalError:
        # Database missing or corrupted - clean up and reinitialize
        for suffix in ["", "-wal", "-shm"]:
            path = Path(str(paths.db_path) + suffix)
            if path.exists():
                try:
                    path.unlink()
                except OSError:
                    pass
        init_db(paths)


# --- Process Management ---
def is_process_alive(pid: int) -> bool:
    """Check if a process ID exists on the host OS."""
    if pid is None:
        return False
    try:
        os.kill(pid, 0)
        return True
    except OSError:
        return False


def is_task_queue_process(pid: int) -> bool:
    """Check if a PID is running our task_queue MCP server or tq CLI.

    Returns True if:
    - Process is dead (handled separately)
    - Process command line contains 'task_queue' or 'agent-task-queue'

    Returns False if process is alive but running something else (PID reused).
    """
    if not is_process_alive(pid):
        return False

    try:
        import subprocess

        result = subprocess.run(
            ["ps", "-p", str(pid), "-o", "args="],
            capture_output=True,
            text=True,
            timeout=5,
        )
        if result.returncode != 0:
            return False

        cmdline = result.stdout.strip().lower()
        return (
            "task_queue" in cmdline
            or "agent-task-queue" in cmdline
            or "tq.py" in cmdline
            or "pytest" in cmdline  # For pytest running tests
        )
    except Exception:
        # If we can't check, assume valid (conservative - avoid false orphan cleanup)
        return True


def kill_process_tree(pid: int):
    """Kill a process and all its children via process group."""
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


# --- Logging ---
def log_fmt(msg: str) -> str:
    """Format log message with timestamp."""
    timestamp = datetime.now().strftime("%H:%M:%S")
    return f"[{timestamp}] [TASK-QUEUE] {msg}"


def log_metric(
    metrics_path: Path,
    event: str,
    max_size_mb: float = DEFAULT_MAX_METRICS_SIZE_MB,
    **kwargs,
):
    """
    Append a JSON metric entry to the log file.
    Rotates log file when it exceeds max_size_mb.
    """
    if metrics_path.exists():
        try:
            size_mb = metrics_path.stat().st_size / (1024 * 1024)
            if size_mb > max_size_mb:
                rotated = metrics_path.with_suffix(".json.1")
                metrics_path.rename(rotated)
        except OSError:
            pass

    entry = {
        "event": event,
        "timestamp": datetime.now().isoformat(),
        **kwargs,
    }
    with open(metrics_path, "a") as f:
        f.write(json.dumps(entry) + "\n")


# --- Queue Cleanup ---
def cleanup_queue(
    conn,
    queue_name: str,
    metrics_path: Path,
    max_lock_age_minutes: int = DEFAULT_MAX_LOCK_AGE_MINUTES,
    log_fn=None,
):
    """
    Clean up dead/stale locks and orphaned waiting tasks.

    Args:
        conn: SQLite connection (must have row_factory=sqlite3.Row)
        queue_name: Name of the queue to clean
        metrics_path: Path to metrics log file
        max_lock_age_minutes: Timeout for stale locks
        log_fn: Optional function for logging (signature: log_fn(message))
    """

    def log(msg):
        if log_fn:
            log_fn(msg)
        else:
            print(log_fmt(msg))

    # Check for dead parents (running tasks)
    runners = conn.execute(
        "SELECT id, pid, child_pid FROM queue WHERE queue_name = ? AND status = 'running'",
        (queue_name,),
    ).fetchall()

    for runner in runners:
        if not is_task_queue_process(runner["pid"]):
            child = runner["child_pid"]
            if child and is_process_alive(child):
                log(
                    f"WARNING: Parent PID {runner['pid']} died. Killing orphan child PID {child}..."
                )
                kill_process_tree(child)

            conn.execute("DELETE FROM queue WHERE id = ?", (runner["id"],))
            log_metric(
                metrics_path,
                "zombie_cleared",
                task_id=runner["id"],
                queue_name=queue_name,
                dead_pid=runner["pid"],
                reason="parent_died",
            )
            log(f"WARNING: Cleared zombie lock (ID: {runner['id']}).")

    # Check for orphaned waiting tasks (parent process died before acquiring lock)
    waiters = conn.execute(
        "SELECT id, pid FROM queue WHERE queue_name = ? AND status = 'waiting'",
        (queue_name,),
    ).fetchall()

    for waiter in waiters:
        if not is_task_queue_process(waiter["pid"]):
            conn.execute("DELETE FROM queue WHERE id = ?", (waiter["id"],))
            log_metric(
                metrics_path,
                "orphan_cleared",
                task_id=waiter["id"],
                queue_name=queue_name,
                dead_pid=waiter["pid"],
                reason="waiting_parent_died",
            )
            log(f"WARNING: Cleared orphaned waiting task (ID: {waiter['id']}).")

    # Check for timeouts (stale locks)
    cutoff = (datetime.now() - timedelta(minutes=max_lock_age_minutes)).isoformat()
    stale = conn.execute(
        "SELECT id, child_pid FROM queue WHERE queue_name = ? AND status = 'running' AND updated_at < ?",
        (queue_name, cutoff),
    ).fetchall()

    for task in stale:
        if task["child_pid"]:
            kill_process_tree(task["child_pid"])
        conn.execute("DELETE FROM queue WHERE id = ?", (task["id"],))
        log_metric(
            metrics_path,
            "zombie_cleared",
            task_id=task["id"],
            queue_name=queue_name,
            reason="timeout",
            timeout_minutes=max_lock_age_minutes,
        )
        log(f"WARNING: Cleared stale lock (ID: {task['id']}) active > {max_lock_age_minutes}m")


def release_lock(conn, task_id: int):
    """Release a queue lock by deleting the task entry."""
    try:
        conn.execute("DELETE FROM queue WHERE id = ?", (task_id,))
        conn.commit()
    except sqlite3.OperationalError:
        pass
