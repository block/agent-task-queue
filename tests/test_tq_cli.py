"""
Test suite for the tq CLI tool.
Tests the command-line interface for running tasks and inspecting the queue.
"""

import argparse
import json
import os
import signal
import sqlite3
import subprocess
import sys
import tempfile
import time
from collections.abc import Callable
from pathlib import Path
from typing import TypeVar

import pytest
import tq

# Path to tq.py
TQ_PATH = Path(__file__).parent.parent / "tq.py"
T = TypeVar("T")


@pytest.fixture
def temp_data_dir():
    """Create a temporary data directory for tests."""
    with tempfile.TemporaryDirectory() as tmpdir:
        yield tmpdir


def run_tq(*args, data_dir=None, cwd=None, timeout=30):
    """Run tq CLI and return result."""
    cmd = [sys.executable, str(TQ_PATH)]
    if data_dir:
        cmd.append(f"--data-dir={data_dir}")
    cmd.extend(args)

    result = subprocess.run(
        cmd,
        capture_output=True,
        text=True,
        cwd=cwd,
        timeout=timeout,
    )
    return result


def poll_with_linear_backoff(
    operation: Callable[[], T],
    *,
    is_ready: Callable[[T], bool],
    timeout: float,
    description: str,
    retriable_exceptions: tuple[type[Exception], ...] = (),
    initial_delay: float = 0.05,
    delay_step: float = 0.05,
    max_delay: float = 0.5,
) -> T:
    """Poll until a condition is ready using a bounded linear backoff."""
    deadline = time.monotonic() + timeout
    delay = initial_delay
    last_error: Exception | None = None

    while True:
        try:
            value = operation()
            last_error = None
            if is_ready(value):
                return value
        except retriable_exceptions as exc:
            last_error = exc

        remaining = deadline - time.monotonic()
        if remaining <= 0:
            break

        time.sleep(min(delay, remaining))
        delay = min(delay + delay_step, max_delay)

    if last_error is not None:
        raise last_error
    raise AssertionError(f"Timed out waiting for {description}")


def wait_for_queue_rows(db_path: Path, status: str, expected_count: int, timeout: float = 5.0):
    """Poll until the queue has the expected number of rows with the given status."""

    def read_count() -> int:
        conn = sqlite3.connect(db_path, timeout=5.0)
        conn.row_factory = sqlite3.Row
        try:
            return conn.execute(
                "SELECT COUNT(*) as c FROM queue WHERE status = ?",
                (status,),
            ).fetchone()["c"]
        finally:
            conn.close()

    poll_with_linear_backoff(
        read_count,
        is_ready=lambda count: count == expected_count,
        timeout=timeout,
        description=f"{expected_count} queue rows with status={status!r}",
        retriable_exceptions=(sqlite3.OperationalError,),
    )


def wait_for_queue_row(
    db_path: Path,
    query: str,
    params: tuple = (),
    *,
    timeout: float = 5.0,
    predicate: Callable[[sqlite3.Row], bool] | None = None,
):
    """Poll until a query returns a row, optionally requiring an additional predicate."""

    def read_row() -> sqlite3.Row | None:
        conn = sqlite3.connect(db_path, timeout=5.0)
        conn.row_factory = sqlite3.Row
        try:
            return conn.execute(query, params).fetchone()
        finally:
            conn.close()

    return poll_with_linear_backoff(
        read_row,
        is_ready=lambda row: row is not None and (predicate is None or predicate(row)),
        timeout=timeout,
        description=f"queue query: {query}",
        retriable_exceptions=(sqlite3.OperationalError,),
    )


def wait_for_process_exit(proc: subprocess.Popen, timeout: float = 10.0):
    """Poll until a subprocess exits so signal-handling tests do not depend on one wait tick."""
    try:
        return poll_with_linear_backoff(
            proc.poll,
            is_ready=lambda returncode: returncode is not None,
            timeout=timeout,
            description=f"process exit: {proc.args}",
        )
    except AssertionError as exc:
        raise subprocess.TimeoutExpired(proc.args, timeout) from exc


class TestTqRun:
    """Tests for the tq run command."""

    def test_explicit_run_echo(self, temp_data_dir):
        """Test explicit 'tq run echo' command."""
        result = run_tq("run", "echo", "hello world", data_dir=temp_data_dir)

        assert result.returncode == 0
        assert "[tq] Lock acquired" in result.stdout
        assert "[tq] SUCCESS" in result.stdout

    def test_implicit_run_echo(self, temp_data_dir):
        """Test implicit run: 'tq echo' should work like 'tq run echo'."""
        result = run_tq("echo", "implicit run", data_dir=temp_data_dir)

        assert result.returncode == 0
        assert "[tq] SUCCESS" in result.stdout

    def test_command_with_flags(self, temp_data_dir):
        """Test that commands with flags work correctly."""
        result = run_tq("ls", "-la", "/tmp", data_dir=temp_data_dir)

        assert result.returncode == 0
        assert "[tq] SUCCESS" in result.stdout

    def test_queue_option(self, temp_data_dir):
        """Test -q/--queue option."""
        result = run_tq("-q", "myqueue", "echo", "queue test", data_dir=temp_data_dir)

        assert result.returncode == 0
        assert "queued in 'myqueue'" in result.stdout
        assert "[tq] SUCCESS" in result.stdout

    def test_queue_option_long_form(self, temp_data_dir):
        """Test --queue option (long form)."""
        result = run_tq("run", "--queue", "longqueue", "echo", "test", data_dir=temp_data_dir)

        assert result.returncode == 0
        assert "queued in 'longqueue'" in result.stdout

    def test_queue_capacity_option(self, temp_data_dir):
        """Test global --queue-capacity option with implicit run mode."""
        result = run_tq(
            "--queue-capacity=gradle=2",
            "-q",
            "gradle/emu-5557",
            "echo",
            "capacity test",
            data_dir=temp_data_dir,
        )

        assert result.returncode == 0
        assert "queued in 'gradle/emu-5557'" in result.stdout
        assert "[tq] SUCCESS" in result.stdout

    def test_invalid_queue_capacity_option(self, temp_data_dir):
        """Test invalid --queue-capacity value is rejected."""
        result = run_tq(
            "--queue-capacity=gradle=abc",
            "echo",
            "oops",
            data_dir=temp_data_dir,
        )

        assert result.returncode == 1
        assert "Invalid capacity 'abc'" in result.stderr

    def test_working_directory_option(self, temp_data_dir):
        """Test -C/--dir option."""
        result = run_tq("-C", "/tmp", "pwd", data_dir=temp_data_dir)

        assert result.returncode == 0
        assert "[tq] Directory: /tmp" in result.stdout or "[tq] Directory: /private/tmp" in result.stdout

    def test_timeout_option(self, temp_data_dir):
        """Test -t/--timeout option."""
        result = run_tq("-t", "1", "sleep", "10", data_dir=temp_data_dir)

        assert result.returncode == 124  # Standard timeout exit code
        assert "[tq] TIMEOUT" in result.stdout

    def test_exit_code_propagation_success(self, temp_data_dir):
        """Test that exit code 0 is propagated."""
        result = run_tq("true", data_dir=temp_data_dir)
        assert result.returncode == 0

    def test_exit_code_propagation_failure(self, temp_data_dir):
        """Test that non-zero exit codes are propagated."""
        # Use sh -c with proper quoting (exit 42 as single argument)
        result = run_tq("sh", "-c", "exit 42", data_dir=temp_data_dir)

        assert result.returncode == 42
        assert "[tq] FAILED exit=42" in result.stdout

    def test_no_command_error(self, temp_data_dir):
        """Test error when no command is provided."""
        result = run_tq("run", data_dir=temp_data_dir)

        assert result.returncode == 1
        assert "No command specified" in result.stderr

    def test_invalid_working_directory(self, temp_data_dir):
        """Test error for non-existent working directory."""
        result = run_tq("-C", "/nonexistent/path/xyz", "echo", "test", data_dir=temp_data_dir)

        assert result.returncode == 1
        assert "does not exist" in result.stderr

    def test_metrics_logged(self, temp_data_dir):
        """Test that metrics are logged."""
        run_tq("echo", "metrics test", data_dir=temp_data_dir)

        metrics_path = Path(temp_data_dir) / "agent-task-queue-logs.json"
        assert metrics_path.exists()

        lines = metrics_path.read_text().strip().split("\n")
        events = [json.loads(line)["event"] for line in lines]

        assert "task_queued" in events
        assert "task_started" in events
        assert "task_completed" in events


class TestTqList:
    """Tests for the tq list command."""

    def test_list_empty_queue(self, temp_data_dir):
        """Test list command with empty queue."""
        result = run_tq("list", data_dir=temp_data_dir)

        assert result.returncode == 0
        # Either no database or empty queue message
        assert "empty" in result.stdout.lower() or "no queue" in result.stdout.lower()

    def test_list_no_database(self, temp_data_dir):
        """Test list command when database doesn't exist."""
        result = run_tq("list", data_dir=temp_data_dir)

        assert result.returncode == 0
        assert "No queue database" in result.stdout or "empty" in result.stdout.lower()

    def test_list_json_empty_queue(self, temp_data_dir):
        """Test list --json command with DB exists but queue is empty."""
        # Initialize DB by running a task that completes
        run_tq("echo", "init", data_dir=temp_data_dir)

        result = run_tq("list", "--json", data_dir=temp_data_dir)

        assert result.returncode == 0
        output = json.loads(result.stdout)
        assert output == {
            "tasks": [],
            "summary": {"total": 0, "running": 0, "waiting": 0}
        }

    def test_list_json_no_database(self, temp_data_dir):
        """Test list --json command when database doesn't exist."""
        result = run_tq("list", "--json", data_dir=temp_data_dir)

        assert result.returncode == 0
        output = json.loads(result.stdout)
        assert output["tasks"] == []
        assert output["summary"]["total"] == 0


class TestJsonSchemaContracts:
    """
    Schema contract tests for JSON output.

    These tests ensure the JSON structure remains stable for programmatic consumers
    (e.g., Claude Code status lines). Any changes to these schemas should be
    intentional and backward-compatible.
    """

    # Expected fields for each schema - used to enforce contracts
    LIST_REQUIRED_KEYS = {"tasks", "summary"}
    LIST_SUMMARY_REQUIRED_KEYS = {"total", "running", "waiting"}
    LIST_TASK_REQUIRED_KEYS = {"id", "queue_name", "status", "command", "pid", "child_pid", "created_at", "updated_at"}

    LOGS_REQUIRED_KEYS = {"entries"}
    LOGS_ENTRY_REQUIRED_KEYS = {"event", "timestamp"}  # Base keys all entries must have

    CLEAR_REQUIRED_KEYS = {"cleared", "success"}

    def test_list_json_schema_empty(self, temp_data_dir):
        """Verify list --json schema structure when empty."""
        result = run_tq("list", "--json", data_dir=temp_data_dir)
        output = json.loads(result.stdout)

        # Top-level keys
        assert set(output.keys()) == self.LIST_REQUIRED_KEYS, \
            f"list --json must have exactly keys {self.LIST_REQUIRED_KEYS}"

        # Summary keys
        assert set(output["summary"].keys()) == self.LIST_SUMMARY_REQUIRED_KEYS, \
            f"list --json summary must have exactly keys {self.LIST_SUMMARY_REQUIRED_KEYS}"

        # Type checks
        assert isinstance(output["tasks"], list)
        assert isinstance(output["summary"]["total"], int)
        assert isinstance(output["summary"]["running"], int)
        assert isinstance(output["summary"]["waiting"], int)

    def test_list_json_schema_with_running_task(self, temp_data_dir):
        """Verify list --json task schema with an active task."""
        # Start a long-running task
        proc = subprocess.Popen(
            [sys.executable, str(TQ_PATH), f"--data-dir={temp_data_dir}", "sleep", "30"],
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            start_new_session=True,
        )

        try:
            # Wait for it to start
            time.sleep(0.5)

            result = run_tq("list", "--json", data_dir=temp_data_dir)
            output = json.loads(result.stdout)

            # Verify structure
            assert set(output.keys()) == self.LIST_REQUIRED_KEYS
            assert len(output["tasks"]) >= 1

            # Verify task object schema
            task = output["tasks"][0]
            assert set(task.keys()) == self.LIST_TASK_REQUIRED_KEYS, \
                f"Task object must have exactly keys {self.LIST_TASK_REQUIRED_KEYS}, got {set(task.keys())}"

            # Verify task field types
            assert isinstance(task["id"], int)
            assert isinstance(task["queue_name"], str)
            assert task["status"] in ("running", "waiting")
            assert task["command"] is None or isinstance(task["command"], str)
            assert task["pid"] is None or isinstance(task["pid"], int)
            assert task["child_pid"] is None or isinstance(task["child_pid"], int)
            assert task["created_at"] is None or isinstance(task["created_at"], str)
            assert task["updated_at"] is None or isinstance(task["updated_at"], str)

            # Verify command is populated for the running task
            assert task["command"] == "sleep 30", f"Expected command 'sleep 30', got {task['command']}"

            # Verify summary counts are accurate
            assert output["summary"]["total"] == len(output["tasks"])
            running_count = sum(1 for t in output["tasks"] if t["status"] == "running")
            waiting_count = sum(1 for t in output["tasks"] if t["status"] == "waiting")
            assert output["summary"]["running"] == running_count
            assert output["summary"]["waiting"] == waiting_count

        finally:
            # Clean up
            try:
                os.killpg(os.getpgid(proc.pid), signal.SIGTERM)
            except Exception:
                proc.terminate()
            proc.wait(timeout=5)

    def test_logs_json_schema_empty(self, temp_data_dir):
        """Verify logs --json schema structure when empty."""
        result = run_tq("logs", "--json", data_dir=temp_data_dir)
        output = json.loads(result.stdout)

        assert set(output.keys()) == self.LOGS_REQUIRED_KEYS, \
            f"logs --json must have exactly keys {self.LOGS_REQUIRED_KEYS}"
        assert isinstance(output["entries"], list)

    def test_logs_json_schema_with_entries(self, temp_data_dir):
        """Verify logs --json entry schema with actual log entries."""
        # Generate some logs
        run_tq("echo", "test", data_dir=temp_data_dir)

        result = run_tq("logs", "--json", data_dir=temp_data_dir)
        output = json.loads(result.stdout)

        assert set(output.keys()) == self.LOGS_REQUIRED_KEYS
        assert len(output["entries"]) >= 3  # queued, started, completed

        # Verify each entry has required base keys
        for entry in output["entries"]:
            assert self.LOGS_ENTRY_REQUIRED_KEYS.issubset(set(entry.keys())), \
                f"Log entry must have at least keys {self.LOGS_ENTRY_REQUIRED_KEYS}, got {set(entry.keys())}"
            assert isinstance(entry["event"], str)
            assert isinstance(entry["timestamp"], str)

        # Verify specific event schemas
        for entry in output["entries"]:
            if entry["event"] == "task_queued":
                assert "task_id" in entry
                assert "queue_name" in entry
            elif entry["event"] == "task_started":
                assert "task_id" in entry
                assert "queue_name" in entry
                assert "wait_time_seconds" in entry
            elif entry["event"] == "task_completed":
                assert "task_id" in entry
                assert "queue_name" in entry
                assert "exit_code" in entry
                assert "duration_seconds" in entry

    def test_clear_json_schema(self, temp_data_dir):
        """Verify clear --json schema structure."""
        result = run_tq("clear", "--json", data_dir=temp_data_dir)
        output = json.loads(result.stdout)

        assert set(output.keys()) == self.CLEAR_REQUIRED_KEYS, \
            f"clear --json must have exactly keys {self.CLEAR_REQUIRED_KEYS}"
        assert isinstance(output["cleared"], int)
        assert isinstance(output["success"], bool)
        assert output["cleared"] >= 0
        assert output["success"] is True


class TestTqLogs:
    """Tests for the tq logs command."""

    def test_logs_no_file(self, temp_data_dir):
        """Test logs command when no log file exists."""
        result = run_tq("logs", data_dir=temp_data_dir)

        assert result.returncode == 0
        assert "No log file" in result.stdout

    def test_logs_shows_activity(self, temp_data_dir):
        """Test logs command shows task activity."""
        # Run a task first to generate logs
        run_tq("echo", "test", data_dir=temp_data_dir)

        result = run_tq("logs", data_dir=temp_data_dir)

        assert result.returncode == 0
        assert "queued" in result.stdout
        assert "started" in result.stdout
        assert "completed" in result.stdout

    def test_logs_n_option(self, temp_data_dir):
        """Test logs -n option to limit entries."""
        # Run multiple tasks
        for i in range(5):
            run_tq("echo", f"test {i}", data_dir=temp_data_dir)

        result = run_tq("logs", "-n", "3", data_dir=temp_data_dir)

        assert result.returncode == 0
        # Should have limited output (3 lines)
        lines = [line for line in result.stdout.strip().split("\n") if line]
        assert len(lines) == 3

    def test_logs_json_no_file(self, temp_data_dir):
        """Test logs --json command when no log file exists."""
        result = run_tq("logs", "--json", data_dir=temp_data_dir)

        assert result.returncode == 0
        output = json.loads(result.stdout)
        assert output == {"entries": []}

    def test_logs_json_shows_activity(self, temp_data_dir):
        """Test logs --json command shows task activity."""
        # Run a task first to generate logs
        run_tq("echo", "test", data_dir=temp_data_dir)

        result = run_tq("logs", "--json", data_dir=temp_data_dir)

        assert result.returncode == 0
        output = json.loads(result.stdout)
        assert "entries" in output
        assert len(output["entries"]) >= 3  # queued, started, completed

        events = [e["event"] for e in output["entries"]]
        assert "task_queued" in events
        assert "task_started" in events
        assert "task_completed" in events

    def test_logs_json_n_option(self, temp_data_dir):
        """Test logs --json -n option to limit entries."""
        # Run multiple tasks
        for i in range(5):
            run_tq("echo", f"test {i}", data_dir=temp_data_dir)

        result = run_tq("logs", "--json", "-n", "3", data_dir=temp_data_dir)

        assert result.returncode == 0
        output = json.loads(result.stdout)
        assert len(output["entries"]) == 3


class TestTqClear:
    """Tests for the tq clear command."""

    def test_clear_empty_queue(self, temp_data_dir):
        """Test clear command with empty queue."""
        # Initialize database by running a task that completes
        run_tq("echo", "init", data_dir=temp_data_dir)

        result = run_tq("clear", data_dir=temp_data_dir, timeout=5)

        assert result.returncode == 0
        assert "already empty" in result.stdout.lower()

    def test_clear_json_empty_queue(self, temp_data_dir):
        """Test clear --json command with empty queue."""
        # Initialize database by running a task that completes
        run_tq("echo", "init", data_dir=temp_data_dir)

        result = run_tq("clear", "--json", data_dir=temp_data_dir, timeout=5)

        assert result.returncode == 0
        output = json.loads(result.stdout)
        assert output == {"cleared": 0, "success": True}

    def test_clear_json_no_database(self, temp_data_dir):
        """Test clear --json command when no database exists."""
        result = run_tq("clear", "--json", data_dir=temp_data_dir, timeout=5)

        assert result.returncode == 0
        output = json.loads(result.stdout)
        assert output == {"cleared": 0, "success": True}


class TestTqHelp:
    """Tests for help output."""

    def test_help_flag(self):
        """Test --help flag."""
        result = run_tq("--help")

        assert result.returncode == 0
        assert "Agent Task Queue CLI" in result.stdout
        assert "run" in result.stdout
        assert "list" in result.stdout
        assert "logs" in result.stdout
        assert "clear" in result.stdout

    def test_run_help(self):
        """Test 'tq run --help'."""
        result = run_tq("run", "--help")

        assert result.returncode == 0
        assert "--queue" in result.stdout
        assert "--timeout" in result.stdout
        assert "--dir" in result.stdout


class TestAmpRestart:
    def test_parse_amp_sessions_filters_to_interactive_invocations(self):
        ps_output = """
86296 amp -m deep PWD=/Users/sedwards/Development/block-invert-config AGENT_SESSION_ID=20260420_6
88150 amp threads continue T-019dbffa-53be-708c-b468-b62fff98a27d PWD=/Users/sedwards/Development/agent-task-queue
90000 amp threads search --json repo:block/agent-task-queue PWD=/tmp
91000 /Users/sedwards/.amp/bin/amp --mode=smart PWD=/Users/sedwards/Development/agents AGENT_SESSION_ID=20260422_11
        """

        sessions = tq.parse_amp_sessions_from_ps_output(ps_output)

        assert [(session.pid, session.mode) for session in sessions] == [
            (86296, "deep"),
            (88150, None),
            (91000, "smart"),
        ]
        assert sessions[0].cwd == "/Users/sedwards/Development/block-invert-config"
        assert sessions[0].agent_session_id == "20260420_6"
        assert sessions[1].cwd == "/Users/sedwards/Development/agent-task-queue"

    def test_parse_amp_thread_ids_uses_latest_entry_per_pid(self):
        log_text = "\n".join(
            [
                json.dumps(
                    {
                        "pid": 86296,
                        "timestamp": "2026-04-24T15:00:00.000Z",
                        "threadId": "T-019dbffa-53be-708c-b468-b62fff98a27d",
                    }
                ),
                json.dumps(
                    {
                        "pid": 86296,
                        "timestamp": "2026-04-24T15:05:00.000Z",
                        "message": "[switchToExistingThread] Switching to thread: T-019dc029-f25d-767c-8005-e2996169f6f8",
                    }
                ),
                json.dumps(
                    {
                        "pid": 88150,
                        "timestamp": "2026-04-24T15:10:00.000Z",
                        "newThreadID": "T-019dbfd5-6e1d-7548-b824-f87378e25a8e",
                    }
                ),
            ]
        )

        assert tq.parse_amp_thread_ids_from_log(log_text, {86296, 88150}) == {
            86296: "T-019dc029-f25d-767c-8005-e2996169f6f8",
            88150: "T-019dbfd5-6e1d-7548-b824-f87378e25a8e",
        }

    def test_amp_restart_shell_output_for_targeted_pids(self, monkeypatch, capsys):
        monkeypatch.setattr(
            tq,
            "discover_amp_sessions",
            lambda: [
                tq.AmpSession(
                    pid=86296,
                    cwd="/Users/sedwards/Development/block-invert-config",
                    thread_id="T-019dc029-f25d-767c-8005-e2996169f6f8",
                    agent_session_id="20260420_6",
                    mode="deep",
                ),
                tq.AmpSession(
                    pid=88150,
                    cwd="/Users/sedwards/Development/agent-task-queue",
                    thread_id="T-019dbffa-53be-708c-b468-b62fff98a27d",
                    mode="deep",
                ),
            ],
        )

        exit_code = tq.cmd_amp_restart(
            argparse.Namespace(pid=[86296], json=False, shell=True)
        )

        captured = capsys.readouterr()
        assert exit_code == 0
        assert "kill -TERM 86296" in captured.out
        assert "amp threads continue T-019dc029-f25d-767c-8005-e2996169f6f8" in captured.out
        assert "88150" not in captured.out

    def test_amp_restart_json_fails_for_unresolved_targeted_pid(self, monkeypatch, capsys):
        monkeypatch.setattr(
            tq,
            "discover_amp_sessions",
            lambda: [
                tq.AmpSession(
                    pid=86296,
                    cwd="/Users/sedwards/Development/block-invert-config",
                    thread_id=None,
                    agent_session_id="20260420_6",
                    mode="deep",
                )
            ],
        )

        exit_code = tq.cmd_amp_restart(
            argparse.Namespace(pid=[86296], json=True, shell=False)
        )

        captured = capsys.readouterr()
        payload = json.loads(captured.out)
        assert exit_code == 1
        assert payload["summary"] == {"total": 1, "resolved": 0, "unresolved": 1}
        assert payload["sessions"][0]["thread_id"] is None


class TestQueueIntegration:
    """Tests for queue behavior with CLI."""

    def test_tasks_share_queue(self, temp_data_dir):
        """Test that multiple CLI invocations share the same queue."""
        # Run first task
        run_tq("echo", "first", data_dir=temp_data_dir)

        # Check logs show sequential task IDs
        result = run_tq("logs", data_dir=temp_data_dir)
        assert "#1" in result.stdout

        # Run second task
        run_tq("echo", "second", data_dir=temp_data_dir)

        result = run_tq("logs", data_dir=temp_data_dir)
        assert "#2" in result.stdout

    def test_different_queues_independent(self, temp_data_dir):
        """Test that different queue names are independent."""
        run_tq("-q", "queue_a", "echo", "A", data_dir=temp_data_dir)
        run_tq("-q", "queue_b", "echo", "B", data_dir=temp_data_dir)

        result = run_tq("logs", data_dir=temp_data_dir)

        assert "[queue_a]" in result.stdout
        assert "[queue_b]" in result.stdout

    def test_queue_cleanup_after_completion(self, temp_data_dir):
        """Test that queue entry is removed after task completion."""
        run_tq("echo", "done", data_dir=temp_data_dir)

        result = run_tq("list", data_dir=temp_data_dir)

        # Queue should be empty after task completes
        assert "empty" in result.stdout.lower()


class TestSignalHandling:
    """Test signal handling and cleanup on interrupt."""

    def test_sigint_cleanup_waiting_task(self, temp_data_dir):
        """
        Test that SIGINT (Ctrl+C) properly cleans up a waiting task.

        This tests the fix for the bug where Ctrl+C during the wait phase
        would leave orphaned 'waiting' tasks in the queue.
        """
        import os
        import signal
        import sqlite3

        db_path = Path(temp_data_dir) / "queue.db"

        # First, start a long-running task to hold the lock
        # Use start_new_session to isolate the process group
        blocker = subprocess.Popen(
            [sys.executable, str(TQ_PATH), f"--data-dir={temp_data_dir}", "sleep", "30"],
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            start_new_session=True,
        )

        # Wait for blocker to start and acquire lock
        time.sleep(0.5)

        # Verify blocker has the lock
        conn = sqlite3.connect(db_path, timeout=5.0)
        conn.row_factory = sqlite3.Row
        running = conn.execute("SELECT * FROM queue WHERE status = 'running'").fetchone()
        assert running is not None, "Blocker should be running"
        _ = running["id"]  # blocker_task_id - not used but verifies task exists

        # Now start a second task that will wait in queue
        waiter = subprocess.Popen(
            [sys.executable, str(TQ_PATH), f"--data-dir={temp_data_dir}", "echo", "waited"],
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            start_new_session=True,
        )

        # Wait for waiter to register in queue
        time.sleep(0.5)

        # Verify waiter is in waiting state
        waiting = conn.execute("SELECT * FROM queue WHERE status = 'waiting'").fetchone()
        assert waiting is not None, "Waiter should be in waiting state"
        waiter_task_id = waiting["id"]

        # Send SIGINT to the waiting process (simulating Ctrl+C)
        waiter.send_signal(signal.SIGINT)

        # Wait for waiter to exit
        waiter.wait(timeout=5)
        assert waiter.returncode == 130, "Waiter should exit with 130 (128 + SIGINT)"

        # Verify the waiting task was cleaned up
        conn = sqlite3.connect(db_path, timeout=5.0)
        conn.row_factory = sqlite3.Row
        remaining_waiting = conn.execute(
            "SELECT * FROM queue WHERE status = 'waiting' AND id = ?", (waiter_task_id,)
        ).fetchone()
        assert remaining_waiting is None, "Waiting task should be cleaned up after SIGINT"

        # Clean up: kill the blocker
        try:
            os.killpg(os.getpgid(blocker.pid), signal.SIGTERM)
        except Exception:
            blocker.terminate()
        blocker.wait(timeout=5)
        conn.close()

    def test_sigint_cleanup_running_task(self, temp_data_dir):
        """
        Test that SIGINT properly cleans up a running task and its subprocess.
        """
        import os
        import signal
        import sqlite3

        db_path = Path(temp_data_dir) / "queue.db"

        # Start a task that will run for a while
        proc = subprocess.Popen(
            [sys.executable, str(TQ_PATH), f"--data-dir={temp_data_dir}", "sleep", "30"],
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            start_new_session=True,  # So we can kill the whole group
        )

        # Wait for it to start running
        wait_for_queue_rows(db_path, "running", 1)

        # Wait for the stronger running condition this test depends on.
        running = wait_for_queue_row(
            db_path,
            "SELECT * FROM queue WHERE status = 'running'",
            predicate=lambda row: row["child_pid"] is not None,
        )
        assert running is not None, "Task should be running"
        task_id = running["id"]
        child_pid = running["child_pid"]

        # Send SIGINT
        proc.send_signal(signal.SIGINT)

        # Wait for cleanup
        wait_for_process_exit(proc, timeout=10)
        assert proc.returncode == 130, "Should exit with 130 (128 + SIGINT)"

        # Verify task was cleaned up
        conn = sqlite3.connect(db_path, timeout=5.0)
        conn.row_factory = sqlite3.Row
        remaining = conn.execute(
            "SELECT * FROM queue WHERE id = ?", (task_id,)
        ).fetchone()
        assert remaining is None, "Task should be cleaned up from queue"

        # Verify child process is dead
        try:
            os.kill(child_pid, 0)
            # If we get here, process is still alive - that's bad
            assert False, f"Child process {child_pid} should be dead"
        except OSError:
            pass  # Expected - process is dead

        conn.close()

    def test_multiple_waiters_cancelled(self, temp_data_dir):
        """
        Test that multiple waiting tasks are all cleaned up when cancelled.

        Simulates the scenario where multiple sub-agents are cancelled at once.
        """
        import os
        import signal
        import sqlite3
        import time

        db_path = Path(temp_data_dir) / "queue.db"

        # Start a blocker to hold the lock
        blocker = subprocess.Popen(
            [sys.executable, str(TQ_PATH), f"--data-dir={temp_data_dir}", "sleep", "30"],
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            start_new_session=True,
        )

        wait_for_queue_rows(db_path, "running", 1)

        # Start multiple waiters
        waiters = []
        for i in range(3):
            waiter = subprocess.Popen(
                [sys.executable, str(TQ_PATH), f"--data-dir={temp_data_dir}", "echo", f"waiter_{i}"],
                stdout=subprocess.PIPE,
                stderr=subprocess.PIPE,
                start_new_session=True,
            )
            waiters.append(waiter)
            time.sleep(0.2)  # Stagger registration

        # Verify all waiters are in queue
        wait_for_queue_rows(db_path, "waiting", 3)
        conn = sqlite3.connect(db_path, timeout=5.0)
        conn.row_factory = sqlite3.Row
        waiting_count = conn.execute(
            "SELECT COUNT(*) as c FROM queue WHERE status = 'waiting'"
        ).fetchone()["c"]
        assert waiting_count == 3, f"Expected 3 waiting tasks, got {waiting_count}"

        # Cancel all waiters simultaneously
        for waiter in waiters:
            waiter.send_signal(signal.SIGINT)

        # Wait for all to exit
        for waiter in waiters:
            wait_for_process_exit(waiter, timeout=10)

        # Verify all waiting tasks were cleaned up
        conn = sqlite3.connect(db_path, timeout=5.0)
        conn.row_factory = sqlite3.Row
        remaining = conn.execute(
            "SELECT COUNT(*) as c FROM queue WHERE status = 'waiting'"
        ).fetchone()["c"]
        assert remaining == 0, f"All waiting tasks should be cleaned up, but {remaining} remain"

        # Blocker should still be running
        running = conn.execute("SELECT * FROM queue WHERE status = 'running'").fetchone()
        assert running is not None, "Blocker should still be running"

        # Clean up blocker
        try:
            os.killpg(os.getpgid(blocker.pid), signal.SIGTERM)
        except Exception:
            blocker.terminate()
        blocker.wait(timeout=5)
        conn.close()

    def test_cancel_and_restart_proceeds(self, temp_data_dir):
        """
        Test that after cancelling tasks, new tasks can proceed normally.

        This verifies the queue isn't left in a broken state after cancellation.
        """
        import signal
        import sqlite3
        import time

        db_path = Path(temp_data_dir) / "queue.db"

        # Start and cancel a task
        proc = subprocess.Popen(
            [sys.executable, str(TQ_PATH), f"--data-dir={temp_data_dir}", "sleep", "30"],
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            start_new_session=True,
        )
        time.sleep(0.5)

        # Cancel it
        proc.send_signal(signal.SIGINT)
        proc.wait(timeout=5)

        # Verify queue is empty
        conn = sqlite3.connect(db_path, timeout=5.0)
        conn.row_factory = sqlite3.Row
        count = conn.execute("SELECT COUNT(*) as c FROM queue").fetchone()["c"]
        assert count == 0, "Queue should be empty after cancellation"
        conn.close()

        # Now run a new task - it should succeed
        result = run_tq("echo", "after_cancel", data_dir=temp_data_dir)
        assert result.returncode == 0, "New task should succeed after cancellation"
        assert "[tq] SUCCESS" in result.stdout

    def test_rapid_cancel_restart_cycles(self, temp_data_dir):
        """
        Stress test: rapidly cancel and restart tasks.

        Ensures no race conditions or leaked queue entries.
        """
        import signal
        import sqlite3
        import time

        db_path = Path(temp_data_dir) / "queue.db"

        # Run several cancel/restart cycles
        for cycle in range(5):
            proc = subprocess.Popen(
                [sys.executable, str(TQ_PATH), f"--data-dir={temp_data_dir}", "sleep", "10"],
                stdout=subprocess.PIPE,
                stderr=subprocess.PIPE,
                start_new_session=True,
            )
            time.sleep(0.3)  # Let it register
            proc.send_signal(signal.SIGINT)
            proc.wait(timeout=5)

        # Queue should be empty
        conn = sqlite3.connect(db_path, timeout=5.0)
        conn.row_factory = sqlite3.Row
        count = conn.execute("SELECT COUNT(*) as c FROM queue").fetchone()["c"]
        assert count == 0, f"Queue should be empty after {5} cancel cycles, but has {count} entries"
        conn.close()

        # Final task should work
        result = run_tq("echo", "final", data_dir=temp_data_dir)
        assert result.returncode == 0, "Final task should succeed"
