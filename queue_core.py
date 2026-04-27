"""
Queue Core - Shared infrastructure for agent-task-queue.

This module contains the shared logic used by both:
- task_queue.py (MCP server)
- tq.py (CLI tool)
"""

import json
import os
import signal
import shlex
import sqlite3
import subprocess
from contextlib import contextmanager
from dataclasses import dataclass
from datetime import datetime, timedelta
from pathlib import Path


# --- Configuration ---
DEFAULT_DATA_DIR = Path(os.environ.get("TASK_QUEUE_DATA_DIR", "/tmp/agent-task-queue"))
POLL_INTERVAL_WAITING = float(os.environ.get("TASK_QUEUE_POLL_WAITING", "1"))
DEFAULT_MAX_LOCK_AGE_MINUTES = 120
DEFAULT_MAX_METRICS_SIZE_MB = 5
QUEUE_SCOPE_SEPARATOR = "/"


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


@dataclass(frozen=True)
class TaskOrigin:
    """Optional metadata about where a queued task came from."""

    working_directory: str
    worktree_root: str | None = None
    repo_name: str | None = None
    git_branch: str | None = None
    agent_name: str | None = None


def task_origin_kwargs(task_origin: "TaskOrigin | None") -> dict[str, str]:
    """Return non-empty task origin fields as kwargs suitable for DB/metrics inserts."""
    if task_origin is None:
        return {}

    return {
        key: value
        for key, value in {
            "working_directory": task_origin.working_directory,
            "worktree_root": task_origin.worktree_root,
            "repo_name": task_origin.repo_name,
            "git_branch": task_origin.git_branch,
            "agent_name": task_origin.agent_name,
        }.items()
        if value
    }


def insert_waiting_task(
    conn,
    queue_name: str,
    pid: int,
    server_id: str,
    command: str | None = None,
    task_origin: "TaskOrigin | None" = None,
) -> int:
    """Insert a waiting task row and return its task ID."""
    cursor = conn.execute(
        """INSERT INTO queue (
               queue_name, status, pid, server_id, command,
               working_directory, worktree_root, repo_name, git_branch, agent_name
           ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)""",
        (
            queue_name,
            "waiting",
            pid,
            server_id,
            command,
            task_origin.working_directory if task_origin else None,
            task_origin.worktree_root if task_origin else None,
            task_origin.repo_name if task_origin else None,
            task_origin.git_branch if task_origin else None,
            task_origin.agent_name if task_origin else None,
        ),
    )
    return cursor.lastrowid


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
    working_directory TEXT,
    worktree_root TEXT,
    repo_name TEXT,
    git_branch TEXT,
    agent_name TEXT,
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

QUEUE_MIGRATION_WORKING_DIRECTORY = """
ALTER TABLE queue ADD COLUMN working_directory TEXT
"""

QUEUE_MIGRATION_WORKTREE_ROOT = """
ALTER TABLE queue ADD COLUMN worktree_root TEXT
"""

QUEUE_MIGRATION_REPO_NAME = """
ALTER TABLE queue ADD COLUMN repo_name TEXT
"""

QUEUE_MIGRATION_GIT_BRANCH = """
ALTER TABLE queue ADD COLUMN git_branch TEXT
"""

QUEUE_MIGRATION_AGENT_NAME = """
ALTER TABLE queue ADD COLUMN agent_name TEXT
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
        for migration in [
            QUEUE_MIGRATION_SERVER_ID,
            QUEUE_MIGRATION_COMMAND,
            QUEUE_MIGRATION_WORKING_DIRECTORY,
            QUEUE_MIGRATION_WORKTREE_ROOT,
            QUEUE_MIGRATION_REPO_NAME,
            QUEUE_MIGRATION_GIT_BRANCH,
            QUEUE_MIGRATION_AGENT_NAME,
        ]:
            try:
                conn.execute(migration)
            except sqlite3.OperationalError:
                pass  # Column already exists


def collect_task_origin(working_directory: str, agent_name: str | None = None) -> TaskOrigin:
    """Capture stable repo/worktree metadata for a queued task."""
    resolved_working_directory = str(Path(working_directory).resolve())
    worktree_root = _git_output(resolved_working_directory, "rev-parse", "--show-toplevel")
    repo_name = _git_repo_name(resolved_working_directory)
    git_branch = _git_output(resolved_working_directory, "branch", "--show-current")

    if not git_branch:
        git_branch = _git_output(resolved_working_directory, "rev-parse", "--short", "HEAD")

    return TaskOrigin(
        working_directory=resolved_working_directory,
        worktree_root=worktree_root,
        repo_name=repo_name,
        git_branch=git_branch,
        agent_name=agent_name or None,
    )


def _git_repo_name(working_directory: str) -> str | None:
    remote = _git_output(working_directory, "remote", "get-url", "origin")
    if remote:
        return remote.rstrip("/").rsplit("/", 1)[-1].removesuffix(".git")

    worktree_root = _git_output(working_directory, "rev-parse", "--show-toplevel")
    if worktree_root:
        return Path(worktree_root).name

    return None


def _git_output(working_directory: str, *args: str) -> str | None:
    try:
        result = subprocess.run(
            ["git", "-C", working_directory, *args],
            capture_output=True,
            text=True,
            timeout=5,
        )
    except (OSError, subprocess.SubprocessError):
        return None

    if result.returncode != 0:
        return None

    value = result.stdout.strip()
    return value or None


def normalize_queue_name(queue_name: str) -> str:
    """Collapse redundant separators and whitespace in queue names."""
    parts = [part.strip() for part in queue_name.split(QUEUE_SCOPE_SEPARATOR) if part.strip()]
    if not parts:
        raise ValueError("queue_name must contain at least one non-empty segment")
    return QUEUE_SCOPE_SEPARATOR.join(parts)


def parse_queue_capacities(capacity_args: list[str] | None) -> dict[str, int]:
    """Parse repeated scope=capacity CLI arguments into a normalized map."""
    capacities: dict[str, int] = {}
    for arg in capacity_args or []:
        if "=" not in arg:
            raise ValueError(
                f"Invalid --queue-capacity value '{arg}'. Expected SCOPE=CAPACITY."
            )

        scope, raw_capacity = arg.split("=", 1)
        scope = normalize_queue_name(scope)
        try:
            capacity = int(raw_capacity)
        except ValueError as exc:
            raise ValueError(
                f"Invalid capacity '{raw_capacity}' for scope '{scope}'. Expected a positive integer."
            ) from exc

        if capacity < 1:
            raise ValueError(
                f"Invalid capacity '{capacity}' for scope '{scope}'. Capacity must be >= 1."
            )

        capacities[scope] = capacity

    return capacities


def queue_scopes(queue_name: str) -> list[str]:
    """Return hierarchical scopes from broadest to most specific."""
    normalized = normalize_queue_name(queue_name)
    parts = normalized.split(QUEUE_SCOPE_SEPARATOR)
    return [QUEUE_SCOPE_SEPARATOR.join(parts[:idx]) for idx in range(1, len(parts) + 1)]


def queue_names_in_scope(conn, scope: str) -> list[str]:
    """Return queue names in a scope, including descendant queues."""
    normalized_scope = normalize_queue_name(scope)
    escaped_scope = escape_like_pattern(normalized_scope)
    rows = conn.execute(
        """SELECT DISTINCT queue_name
           FROM queue
           WHERE queue_name = ?
              OR queue_name LIKE ? ESCAPE '\\'
           ORDER BY queue_name""",
        (normalized_scope, f"{escaped_scope}{QUEUE_SCOPE_SEPARATOR}%"),
    ).fetchall()
    return [row["queue_name"] for row in rows]


def cleanup_targets_for_queue(
    conn,
    queue_name: str,
    queue_capacities: dict[str, int] | None,
) -> list[str]:
    """Return queue names that should be reaped before capacity checks."""
    normalized_queue = normalize_queue_name(queue_name)
    targets = [normalized_queue]

    if not queue_capacities:
        return targets

    for scope in queue_scopes(normalized_queue):
        if scope not in queue_capacities:
            continue
        targets.extend(queue_names_in_scope(conn, scope))

    seen: set[str] = set()
    unique_targets: list[str] = []
    for target in targets:
        if target in seen:
            continue
        seen.add(target)
        unique_targets.append(target)
    return unique_targets


def escape_like_pattern(value: str) -> str:
    """Escape SQLite LIKE wildcards in a literal scope name."""
    return value.replace("\\", "\\\\").replace("%", "\\%").replace("_", "\\_")


def count_running_in_scope(conn, scope: str) -> int:
    """Count running tasks in a scope, including descendant queues."""
    escaped_scope = escape_like_pattern(scope)
    row = conn.execute(
        """SELECT COUNT(*) AS c
           FROM queue
           WHERE status = 'running'
             AND (queue_name = ? OR queue_name LIKE ? ESCAPE '\\')""",
        (scope, f"{escaped_scope}{QUEUE_SCOPE_SEPARATOR}%"),
    ).fetchone()
    return row["c"]


def count_running_in_queue(conn, queue_name: str) -> int:
    """Count running tasks in the exact queue only."""
    row = conn.execute(
        "SELECT COUNT(*) AS c FROM queue WHERE queue_name = ? AND status = 'running'",
        (queue_name,),
    ).fetchone()
    return row["c"]


def count_waiting_ahead(conn, queue_name: str, task_id: int) -> int:
    """Count older waiting tasks in the exact queue."""
    row = conn.execute(
        """SELECT COUNT(*) AS c
           FROM queue
           WHERE queue_name = ?
             AND status = 'waiting'
             AND id < ?""",
        (queue_name, task_id),
    ).fetchone()
    return row["c"]


def can_acquire_task(
    conn,
    task_id: int,
    queue_name: str,
    queue_capacities: dict[str, int],
    waiting_ahead: int | None = None,
) -> bool:
    """Return True when the task can start without violating queue capacities."""
    normalized_queue = normalize_queue_name(queue_name)
    scopes = queue_scopes(normalized_queue)

    exact_capacity = queue_capacities.get(normalized_queue, 1)
    if normalized_queue in queue_capacities:
        available_slots = exact_capacity - count_running_in_scope(conn, normalized_queue)
    else:
        available_slots = exact_capacity - count_running_in_queue(conn, normalized_queue)

    for scope in scopes:
        if scope not in queue_capacities or scope == normalized_queue:
            continue
        available_slots = min(
            available_slots,
            queue_capacities[scope] - count_running_in_scope(conn, scope),
        )

    if available_slots <= 0:
        return False

    if waiting_ahead is None:
        waiting_ahead = count_waiting_ahead(conn, normalized_queue, task_id)

    if waiting_ahead >= available_slots:
        return False

    return True


def attempt_task_start(
    conn,
    task_id: int,
    queue_name: str,
    queue_capacities: dict[str, int],
    owner_pid: int,
) -> tuple[bool, int]:
    """Attempt to transition a waiting task into running state.

    Returns:
        Tuple of (started, queue_position). queue_position is only meaningful when started is False.
    """
    previous_busy_timeout = None

    try:
        previous_busy_timeout = conn.execute("PRAGMA busy_timeout").fetchone()[0]
        conn.execute("PRAGMA busy_timeout=100")
        conn.execute("BEGIN IMMEDIATE")
        waiting_ahead = count_waiting_ahead(conn, queue_name, task_id)

        if not can_acquire_task(
            conn,
            task_id,
            queue_name,
            queue_capacities,
            waiting_ahead=waiting_ahead,
        ):
            position = waiting_ahead + 1
            conn.commit()
            return False, position

        cursor = conn.execute(
            """UPDATE queue SET status = 'running', updated_at = ?, pid = ?
               WHERE id = ? AND status = 'waiting'""",
            (datetime.now().isoformat(), owner_pid, task_id),
        )
        if cursor.rowcount > 0:
            conn.commit()
            return True, 0

        position = waiting_ahead + 1
        conn.commit()
        return False, position
    except Exception:
        if conn.in_transaction:
            conn.rollback()
        raise
    finally:
        if previous_busy_timeout is not None:
            # SQLite PRAGMA statements do not support bound parameters.
            conn.execute("PRAGMA busy_timeout=" + str(int(previous_busy_timeout)))


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
        known_entrypoints = {
            "task_queue",
            "task_queue.py",
            "agent-task-queue",
            "tq",
            "tq.py",
            "pytest",
        }

        try:
            argv = shlex.split(cmdline)
        except ValueError:
            argv = cmdline.split()

        # Installed entrypoints appear as basenames like `tq`, while module launches often
        # look like `python .../tq.py`, so inspect the first two argv entries before falling
        # back to the broader historical substring checks.
        for token in argv[:2]:
            if Path(token).name in known_entrypoints:
                return True

        return "task_queue" in cmdline or "agent-task-queue" in cmdline
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
