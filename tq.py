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
import uuid
from datetime import datetime
from pathlib import Path

# Import shared queue infrastructure
from queue_core import (
    QueuePaths,
    get_db,
    init_db,
    ensure_db,
    cleanup_queue as _cleanup_queue,
    log_metric as _log_metric,
    release_lock,
    is_process_alive,
    kill_process_tree,
    POLL_INTERVAL_WAITING,
    DEFAULT_MAX_LOCK_AGE_MINUTES,
    DEFAULT_MAX_METRICS_SIZE_MB,
)

# Unique identifier for this CLI instance - used to detect orphaned tasks
# from previous CLI instances even if the PID is reused
CLI_INSTANCE_ID = str(uuid.uuid4())[:8]


def get_paths(args) -> QueuePaths:
    """Get queue paths from args or environment."""
    if args.data_dir:
        data_dir = Path(args.data_dir)
    else:
        data_dir = Path(os.environ.get("TASK_QUEUE_DATA_DIR", "/tmp/agent-task-queue"))
    return QueuePaths.from_data_dir(data_dir)


def cmd_list(args):
    """List all tasks in the queue."""
    paths = get_paths(args)
    json_output = getattr(args, "json", False)

    if not paths.db_path.exists():
        if json_output:
            print(json.dumps({"tasks": [], "summary": {"total": 0, "running": 0, "waiting": 0}}))
        else:
            print(f"No queue database found at {paths.db_path}")
            print("Queue is empty (no tasks have been run yet)")
        return

    conn = sqlite3.connect(paths.db_path, timeout=5.0)
    conn.row_factory = sqlite3.Row

    try:
        rows = conn.execute(
            "SELECT * FROM queue ORDER BY queue_name, id"
        ).fetchall()

        if json_output:
            tasks = []
            running_count = 0
            waiting_count = 0
            for row in rows:
                task = {
                    "id": row["id"],
                    "queue_name": row["queue_name"],
                    "status": row["status"],
                    "command": row["command"] if "command" in row.keys() else None,
                    "pid": row["pid"],
                    "child_pid": row["child_pid"],
                    "created_at": row["created_at"],
                    "updated_at": row["updated_at"],
                }
                tasks.append(task)
                if row["status"] == "running":
                    running_count += 1
                elif row["status"] == "waiting":
                    waiting_count += 1

            output = {
                "tasks": tasks,
                "summary": {
                    "total": len(tasks),
                    "running": running_count,
                    "waiting": waiting_count,
                },
            }
            print(json.dumps(output))
            return

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
    paths = get_paths(args)
    json_output = getattr(args, "json", False)

    if not paths.db_path.exists():
        if json_output:
            print(json.dumps({"cleared": 0, "success": True}))
        else:
            print("No queue database found")
        return

    conn = sqlite3.connect(paths.db_path, timeout=5.0)
    try:
        # Check how many tasks exist
        count = conn.execute("SELECT COUNT(*) FROM queue").fetchone()[0]
        if count == 0:
            if json_output:
                print(json.dumps({"cleared": 0, "success": True}))
            else:
                print("Queue is already empty")
            return

        # JSON mode skips confirmation (implies --force)
        if not json_output:
            response = input(f"Clear {count} task(s) from queue? [y/N] ")
            if response.lower() != 'y':
                print("Cancelled")
                return

        cursor = conn.execute("DELETE FROM queue")
        conn.commit()

        if json_output:
            print(json.dumps({"cleared": cursor.rowcount, "success": True}))
        else:
            print(f"Cleared {cursor.rowcount} task(s) from queue")
    finally:
        conn.close()


def cmd_logs(args):
    """Show recent log entries."""
    paths = get_paths(args)
    json_output = getattr(args, "json", False)

    if not paths.metrics_path.exists():
        if json_output:
            print(json.dumps({"entries": []}))
        else:
            print(f"No log file found at {paths.metrics_path}")
        return

    lines = paths.metrics_path.read_text().strip().split("\n")
    recent = lines[-args.n:] if len(lines) > args.n else lines

    if json_output:
        entries = []
        for line in recent:
            try:
                entry = json.loads(line)
                entries.append(entry)
            except json.JSONDecodeError:
                # Skip malformed lines in JSON mode
                pass
        print(json.dumps({"entries": entries}))
        return

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
            elif event == "orphan_cleared":
                reason = entry.get("reason", "?")
                print(f"{ts} [{queue}] #{task_id} orphan cleared ({reason})")
            else:
                print(f"{ts} {event}")
        except json.JSONDecodeError:
            print(line)


# --- Run Command Implementation ---

def log_metric(paths: QueuePaths, event: str, **kwargs):
    """Log metric using paths (wrapper for CLI)."""
    _log_metric(paths.metrics_path, event, DEFAULT_MAX_METRICS_SIZE_MB, **kwargs)


def cleanup_queue(conn, queue_name: str, paths: QueuePaths):
    """Clean up queue (wrapper for CLI)."""
    _cleanup_queue(conn, queue_name, paths.metrics_path, DEFAULT_MAX_LOCK_AGE_MINUTES)

    # Additional cleanup: Tasks with our PID but DIFFERENT instance_id (from old CLI instance)
    # This handles the edge case where PID is reused after CLI crash
    my_pid = os.getpid()
    stale_tasks = conn.execute(
        "SELECT id, status, child_pid, server_id FROM queue WHERE queue_name = ? AND pid = ? AND server_id IS NOT NULL AND server_id != ?",
        (queue_name, my_pid, CLI_INSTANCE_ID),
    ).fetchall()

    for task in stale_tasks:
        if task["child_pid"] and is_process_alive(task["child_pid"]):
            print(f"[tq] WARNING: Killing orphaned subprocess {task['child_pid']} from old CLI instance")
            kill_process_tree(task["child_pid"])

        conn.execute("DELETE FROM queue WHERE id = ?", (task["id"],))
        log_metric(
            paths,
            "orphan_cleared",
            task_id=task["id"],
            queue_name=queue_name,
            status=task["status"],
            old_instance_id=task["server_id"],
            reason="stale_cli_instance",
        )
        print(f"[tq] WARNING: Cleared task from old CLI instance (ID: {task['id']}, old_instance: {task['server_id']})")


def register_task(conn, queue_name: str, paths: QueuePaths, command: str = None) -> int:
    """Register a task in the queue. Returns task_id immediately."""
    my_pid = os.getpid()

    cursor = conn.execute(
        "INSERT INTO queue (queue_name, status, pid, server_id, command) VALUES (?, ?, ?, ?, ?)",
        (queue_name, "waiting", my_pid, CLI_INSTANCE_ID, command),
    )
    conn.commit()
    task_id = cursor.lastrowid

    log_metric(paths, "task_queued", task_id=task_id, queue_name=queue_name, pid=my_pid)
    print(f"[tq] Task #{task_id} queued in '{queue_name}'")
    return task_id


def wait_for_turn(conn, queue_name: str, task_id: int, paths: QueuePaths) -> None:
    """Wait for the task's turn to run. Task must already be registered."""
    my_pid = os.getpid()
    queued_at = time.time()

    last_pos = -1

    while True:
        cleanup_queue(conn, queue_name, paths)

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

            time.sleep(POLL_INTERVAL_WAITING)
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
                paths,
                "task_started",
                task_id=task_id,
                queue_name=queue_name,
                wait_time_seconds=round(wait_time, 2),
            )
            if wait_time > 1:
                print(f"[tq] Lock acquired after {wait_time:.1f}s wait")
            else:
                print("[tq] Lock acquired")
            return  # Lock acquired, task_id was passed in

        time.sleep(POLL_INTERVAL_WAITING)


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

    paths = get_paths(args)
    paths.data_dir.mkdir(parents=True, exist_ok=True)

    # Ensure database exists and is valid (recover if corrupted)
    ensure_db(paths)

    # Get database connection
    with get_db(paths.db_path) as conn:
        # Initialize schema if needed (idempotent via IF NOT EXISTS)
        init_db(paths)

    # Open connection for the duration of the run
    conn = sqlite3.connect(paths.db_path, timeout=60.0)
    conn.execute("PRAGMA journal_mode=WAL")
    conn.execute("PRAGMA busy_timeout=60000")
    conn.row_factory = sqlite3.Row

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
        # Run cleanup BEFORE inserting - this clears orphaned tasks that would otherwise
        # block the queue forever (since cleanup only runs during polling)
        cleanup_queue(conn, queue_name, paths)

        # Register task first so task_id is available for cleanup if interrupted
        task_id = register_task(conn, queue_name, paths, command=command)
        wait_for_turn(conn, queue_name, task_id, paths)

        print(f"[tq] Running: {command}")
        print(f"[tq] Directory: {working_dir}")
        print("-" * 60)

        start = time.time()

        # Run subprocess in passthrough mode - direct terminal connection
        # This preserves rich output (progress bars, colors, etc.)
        # nosec B602: shell=True is intentional - this CLI tool executes user-provided
        # commands, similar to bash -c or make. Users control their own CLI arguments.
        # Shell features (pipes, redirects, globs) are required for build commands.
        proc = subprocess.Popen(
            command,
            shell=True,  # nosec B602
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
                paths,
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
            paths,
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
                paths,
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
    list_parser = subparsers.add_parser("list", help="List tasks in queue")
    list_parser.add_argument("--json", action="store_true", help="Output in JSON format")

    # clear
    clear_parser = subparsers.add_parser("clear", help="Clear all tasks from queue")
    clear_parser.add_argument("--json", action="store_true", help="Output in JSON format and skip confirmation")

    # logs
    logs_parser = subparsers.add_parser("logs", help="Show recent log entries")
    logs_parser.add_argument("-n", type=int, default=20, help="Number of entries (default: 20)")
    logs_parser.add_argument("--json", action="store_true", help="Output in JSON format")

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
