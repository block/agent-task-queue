#!/usr/bin/env python3
"""
tq - Agent Task Queue CLI

CLI to inspect and run commands through the Agent Task Queue.
"""

import argparse
import json
import os
import shlex
import signal
import sqlite3
import subprocess
import sys
import time
from datetime import datetime, timedelta
from pathlib import Path


def get_data_dir(args):
    """Get data directory from args or environment."""
    if args.data_dir:
        return Path(args.data_dir)
    return Path(os.environ.get("TASK_QUEUE_DATA_DIR", "/tmp/agent-task-queue"))


def cmd_list(args):
    """List all tasks in the queue."""
    data_dir = get_data_dir(args)
    db_path = data_dir / "queue.db"

    if not db_path.exists():
        print(f"No queue database found at {db_path}")
        print("Queue is empty (no tasks have been run yet)")
        return

    conn = sqlite3.connect(db_path, timeout=5.0)
    conn.row_factory = sqlite3.Row

    try:
        rows = conn.execute(
            "SELECT * FROM queue ORDER BY queue_name, id"
        ).fetchall()

        if not rows:
            print("Queue is empty")
            return

        # Group by queue name
        queues = {}
        for row in rows:
            qname = row["queue_name"]
            if qname not in queues:
                queues[qname] = []
            queues[qname].append(row)

        for qname, tasks in queues.items():
            print(f"\n[{qname}] ({len(tasks)} task(s))")
            print("-" * 50)

            for task in tasks:
                status = task["status"].upper()
                task_id = task["id"]
                pid = task["pid"] or "-"
                child_pid = task["child_pid"] or "-"
                created = task["created_at"]

                # Format timestamp
                if created:
                    try:
                        dt = datetime.fromisoformat(created)
                        created = dt.strftime("%H:%M:%S")
                    except ValueError:
                        pass

                status_icon = "🔄" if status == "RUNNING" else "⏳"
                print(f"  {status_icon} #{task_id} {status} (pid={pid}, child={child_pid}) @ {created}")

    finally:
        conn.close()


def cmd_clear(args):
    """Clear all tasks from the queue."""
    data_dir = get_data_dir(args)
    db_path = data_dir / "queue.db"

    if not db_path.exists():
        print("No queue database found")
        return

    conn = sqlite3.connect(db_path, timeout=5.0)
    try:
        # Check how many tasks exist
        count = conn.execute("SELECT COUNT(*) FROM queue").fetchone()[0]
        if count == 0:
            print("Queue is already empty")
            return

        response = input(f"Clear {count} task(s) from queue? [y/N] ")
        if response.lower() != 'y':
            print("Cancelled")
            return

        cursor = conn.execute("DELETE FROM queue")
        conn.commit()
        print(f"Cleared {cursor.rowcount} task(s) from queue")
    finally:
        conn.close()


def cmd_logs(args):
    """Show recent log entries."""
    data_dir = get_data_dir(args)
    log_path = data_dir / "agent-task-queue-logs.json"

    if not log_path.exists():
        print(f"No log file found at {log_path}")
        return

    import json

    lines = log_path.read_text().strip().split("\n")
    recent = lines[-args.n:] if len(lines) > args.n else lines

    for line in recent:
        try:
            entry = json.loads(line)
            ts = entry.get("timestamp", "")[:19].replace("T", " ")
            event = entry.get("event", "unknown")
            task_id = entry.get("task_id", "")
            queue = entry.get("queue_name", "")

            # Format based on event type
            if event == "task_completed":
                exit_code = entry.get("exit_code", "?")
                duration = entry.get("duration_seconds", "?")
                print(f"{ts} [{queue}] #{task_id} completed exit={exit_code} {duration}s")
            elif event == "task_started":
                wait = entry.get("wait_time_seconds", 0)
                print(f"{ts} [{queue}] #{task_id} started (waited {wait}s)")
            elif event == "task_queued":
                print(f"{ts} [{queue}] #{task_id} queued")
            elif event == "task_timeout":
                print(f"{ts} [{queue}] #{task_id} TIMEOUT")
            elif event == "task_error":
                error = entry.get("error", "?")
                print(f"{ts} [{queue}] #{task_id} ERROR: {error}")
            elif event == "zombie_cleared":
                reason = entry.get("reason", "?")
                print(f"{ts} [{queue}] #{task_id} zombie cleared ({reason})")
            else:
                print(f"{ts} {event}")
        except json.JSONDecodeError:
            print(line)


# --- Run Command Implementation ---

# Configuration
POLL_INTERVAL = 1.0  # seconds between queue checks
MAX_LOCK_AGE_MINUTES = 120  # stale lock timeout
MAX_METRICS_SIZE_MB = 5  # rotate log when exceeds this size


def ensure_db(db_path: Path):
    """Ensure database exists and is valid. Recreates if corrupted."""
    try:
        conn = sqlite3.connect(db_path, timeout=5.0)
        conn.execute("SELECT 1 FROM queue LIMIT 1")
        conn.close()
    except sqlite3.OperationalError:
        # Database missing or corrupted - clean up and reinitialize
        for suffix in ["", "-wal", "-shm"]:
            path = Path(str(db_path) + suffix)
            if path.exists():
                try:
                    path.unlink()
                except OSError:
                    pass


def log_metric(data_dir: Path, event: str, **kwargs):
    """Append a JSON metric entry to the log file. Rotates when size exceeds limit."""
    log_path = data_dir / "agent-task-queue-logs.json"

    # Rotate if file exceeds size limit
    if log_path.exists():
        try:
            size_mb = log_path.stat().st_size / (1024 * 1024)
            if size_mb > MAX_METRICS_SIZE_MB:
                rotated = log_path.with_suffix(".json.1")
                log_path.rename(rotated)
        except OSError:
            pass

    entry = {
        "event": event,
        "timestamp": datetime.now().isoformat(),
        **kwargs,
    }
    with open(log_path, "a") as f:
        f.write(json.dumps(entry) + "\n")


def is_process_alive(pid: int) -> bool:
    """Check if a process ID exists."""
    if pid is None:
        return False
    try:
        os.kill(pid, 0)
        return True
    except OSError:
        return False


def kill_process_tree(pid: int):
    """Kill a process and all its children."""
    if not pid or not is_process_alive(pid):
        return
    try:
        os.killpg(pid, signal.SIGTERM)
    except OSError:
        try:
            os.kill(pid, signal.SIGTERM)
        except OSError:
            pass


def cleanup_queue(conn, queue_name: str, data_dir: Path):
    """Clean up dead/stale locks and log metrics."""
    # Check for dead parents
    runners = conn.execute(
        "SELECT id, pid, child_pid FROM queue WHERE queue_name = ? AND status = 'running'",
        (queue_name,),
    ).fetchall()

    for runner in runners:
        if not is_process_alive(runner["pid"]):
            child = runner["child_pid"]
            if child and is_process_alive(child):
                kill_process_tree(child)
            conn.execute("DELETE FROM queue WHERE id = ?", (runner["id"],))
            conn.commit()
            log_metric(
                data_dir,
                "zombie_cleared",
                task_id=runner["id"],
                queue_name=queue_name,
                dead_pid=runner["pid"],
                reason="parent_died",
            )

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
        conn.commit()
        log_metric(
            data_dir,
            "zombie_cleared",
            task_id=task["id"],
            queue_name=queue_name,
            reason="timeout",
            timeout_minutes=MAX_LOCK_AGE_MINUTES,
        )


def wait_for_turn(conn, queue_name: str, data_dir: Path) -> int:
    """Register task, wait for turn, return task ID when acquired."""
    my_pid = os.getpid()

    cursor = conn.execute(
        "INSERT INTO queue (queue_name, status, pid) VALUES (?, ?, ?)",
        (queue_name, "waiting", my_pid),
    )
    conn.commit()
    task_id = cursor.lastrowid

    log_metric(data_dir, "task_queued", task_id=task_id, queue_name=queue_name, pid=my_pid)
    queued_at = time.time()

    print(f"[tq] Task #{task_id} queued in '{queue_name}'")

    last_pos = -1

    while True:
        cleanup_queue(conn, queue_name, data_dir)

        runner = conn.execute(
            "SELECT id FROM queue WHERE queue_name = ? AND status = 'running'",
            (queue_name,),
        ).fetchone()

        if runner:
            pos = conn.execute(
                "SELECT COUNT(*) as c FROM queue WHERE queue_name = ? AND status = 'waiting' AND id < ?",
                (queue_name, task_id),
            ).fetchone()["c"] + 1

            if pos != last_pos:
                print(f"[tq] Position #{pos} in queue. Waiting...")
                last_pos = pos

            time.sleep(POLL_INTERVAL)
            continue

        # Try to acquire lock atomically
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
        conn.commit()

        if cursor.rowcount > 0:
            wait_time = time.time() - queued_at
            log_metric(
                data_dir,
                "task_started",
                task_id=task_id,
                queue_name=queue_name,
                wait_time_seconds=round(wait_time, 2),
            )
            if wait_time > 1:
                print(f"[tq] Lock acquired after {wait_time:.1f}s wait")
            else:
                print(f"[tq] Lock acquired")
            return task_id

        time.sleep(POLL_INTERVAL)


def release_lock(conn, task_id: int):
    """Release a queue lock."""
    try:
        conn.execute("DELETE FROM queue WHERE id = ?", (task_id,))
        conn.commit()
    except sqlite3.OperationalError:
        pass


def cmd_run(args):
    """Run a command through the task queue."""
    if not args.run_command:
        print("Error: No command specified", file=sys.stderr)
        sys.exit(1)

    # Use shlex.join to properly quote arguments with spaces
    command = shlex.join(args.run_command)
    working_dir = os.path.abspath(args.dir) if args.dir else os.getcwd()
    queue_name = args.queue
    timeout = args.timeout

    if not os.path.exists(working_dir):
        print(f"Error: Working directory does not exist: {working_dir}", file=sys.stderr)
        sys.exit(1)

    data_dir = get_data_dir(args)
    data_dir.mkdir(parents=True, exist_ok=True)
    db_path = data_dir / "queue.db"
    output_dir = data_dir / "output"

    # Ensure database exists and is valid (recover if corrupted)
    ensure_db(db_path)

    # Initialize database if needed
    conn = sqlite3.connect(db_path, timeout=60.0)
    conn.execute("PRAGMA journal_mode=WAL")
    conn.execute("PRAGMA busy_timeout=60000")
    conn.row_factory = sqlite3.Row

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
    conn.commit()

    task_id = None
    proc = None
    cleaned_up = False

    def cleanup_handler(signum, frame):
        """Handle Ctrl+C - clean up and exit."""
        nonlocal cleaned_up
        if cleaned_up:
            return
        cleaned_up = True

        print("\n[tq] Interrupted. Cleaning up...")
        if proc and proc.poll() is None:
            try:
                os.killpg(proc.pid, signal.SIGTERM)
                proc.wait(timeout=5)
            except Exception:
                try:
                    os.killpg(proc.pid, signal.SIGKILL)
                except Exception:
                    pass
        if task_id:
            try:
                release_lock(conn, task_id)
            except Exception:
                pass
        try:
            conn.close()
        except Exception:
            pass
        sys.exit(130)

    signal.signal(signal.SIGINT, cleanup_handler)
    signal.signal(signal.SIGTERM, cleanup_handler)

    try:
        task_id = wait_for_turn(conn, queue_name, data_dir)

        print(f"[tq] Running: {command}")
        print(f"[tq] Directory: {working_dir}")
        print("-" * 60)

        start = time.time()

        # Run subprocess in passthrough mode - direct terminal connection
        # This preserves rich output (progress bars, colors, etc.)
        proc = subprocess.Popen(
            command,
            shell=True,
            cwd=working_dir,
            start_new_session=True,  # For clean process group kill
        )

        # Record child PID for zombie protection
        conn.execute(
            "UPDATE queue SET child_pid = ? WHERE id = ?", (proc.pid, task_id)
        )
        conn.commit()

        # Wait for process (Ctrl+C will trigger cleanup_handler)
        try:
            proc.wait(timeout=timeout if timeout else None)
        except subprocess.TimeoutExpired:
            print(f"\n[tq] TIMEOUT after {timeout}s")
            try:
                os.killpg(proc.pid, signal.SIGKILL)
            except OSError:
                pass
            proc.wait()
            log_metric(
                data_dir,
                "task_timeout",
                task_id=task_id,
                queue_name=queue_name,
                command=command,
                timeout_seconds=timeout,
            )
            return 124  # Standard timeout exit code

        duration = time.time() - start
        exit_code = proc.returncode

        print("-" * 60)
        if exit_code == 0:
            print(f"[tq] SUCCESS in {duration:.1f}s")
        else:
            print(f"[tq] FAILED exit={exit_code} in {duration:.1f}s")

        log_metric(
            data_dir,
            "task_completed",
            task_id=task_id,
            queue_name=queue_name,
            command=command,
            exit_code=exit_code,
            duration_seconds=round(duration, 2),
        )

        return exit_code

    except Exception as e:
        print(f"[tq] Error: {e}", file=sys.stderr)
        if task_id:
            log_metric(
                data_dir,
                "task_error",
                task_id=task_id,
                queue_name=queue_name,
                error=str(e),
            )
        return 1

    finally:
        if not cleaned_up:
            if task_id:
                try:
                    release_lock(conn, task_id)
                except Exception:
                    pass
            try:
                conn.close()
            except Exception:
                pass


def main():
    parser = argparse.ArgumentParser(
        prog="tq",
        description="Agent Task Queue CLI - inspect and manage the task queue",
    )
    parser.add_argument(
        "--data-dir",
        help="Data directory (default: $TASK_QUEUE_DATA_DIR or /tmp/agent-task-queue)",
    )

    subparsers = parser.add_subparsers(dest="command", help="Commands")

    # run
    run_parser = subparsers.add_parser("run", help="Run a command through the queue")
    run_parser.add_argument("-q", "--queue", default="global", help="Queue name (default: global)")
    run_parser.add_argument("-t", "--timeout", type=int, default=1200, help="Timeout in seconds (default: 1200)")
    run_parser.add_argument("-C", "--dir", help="Working directory (default: current)")
    run_parser.add_argument("run_command", nargs=argparse.REMAINDER, metavar="COMMAND", help="Command to run")

    # list
    subparsers.add_parser("list", help="List tasks in queue")

    # clear
    subparsers.add_parser("clear", help="Clear all tasks from queue")

    # logs
    logs_parser = subparsers.add_parser("logs", help="Show recent log entries")
    logs_parser.add_argument("-n", type=int, default=20, help="Number of entries (default: 20)")

    # Handle implicit run: tq ./gradlew build -> tq run ./gradlew build
    # Pre-process argv to insert 'run' if needed
    known_subcommands = {"run", "list", "clear", "logs"}
    args_list = sys.argv[1:]

    # Find the first non-option argument (skip --data-dir and its value)
    first_positional_idx = None
    i = 0
    while i < len(args_list):
        arg = args_list[i]
        if arg.startswith("--data-dir"):
            # Skip --data-dir=value or --data-dir value
            if "=" not in arg:
                i += 1  # Skip the next arg (value)
            i += 1
            continue
        if arg in ("-h", "--help"):
            i += 1
            continue
        # Found first positional argument
        first_positional_idx = i
        break

    # If first positional is not a known subcommand, insert 'run'
    if first_positional_idx is not None and args_list[first_positional_idx] not in known_subcommands:
        args_list.insert(first_positional_idx, "run")

    args = parser.parse_args(args_list)

    if args.command == "run":
        exit_code = cmd_run(args)
        sys.exit(exit_code if exit_code else 0)
    elif args.command == "list":
        cmd_list(args)
    elif args.command == "clear":
        cmd_clear(args)
    elif args.command == "logs":
        cmd_logs(args)
    else:
        parser.print_help()


if __name__ == "__main__":
    main()
