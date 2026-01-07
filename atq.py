#!/usr/bin/env python3
"""
atq - Agent Task Queue CLI

Simple CLI to inspect the task queue.
"""

import argparse
import os
import sqlite3
from datetime import datetime
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


def main():
    parser = argparse.ArgumentParser(
        prog="atq",
        description="Agent Task Queue CLI - inspect and manage the task queue",
    )
    parser.add_argument(
        "--data-dir",
        help="Data directory (default: $TASK_QUEUE_DATA_DIR or /tmp/agent-task-queue)",
    )

    subparsers = parser.add_subparsers(dest="command", help="Commands")

    # list
    subparsers.add_parser("list", help="List tasks in queue")

    # clear
    subparsers.add_parser("clear", help="Clear all tasks from queue")

    # logs
    logs_parser = subparsers.add_parser("logs", help="Show recent log entries")
    logs_parser.add_argument("-n", type=int, default=20, help="Number of entries (default: 20)")

    args = parser.parse_args()

    if args.command == "list":
        cmd_list(args)
    elif args.command == "clear":
        cmd_clear(args)
    elif args.command == "logs":
        cmd_logs(args)
    else:
        parser.print_help()


if __name__ == "__main__":
    main()
