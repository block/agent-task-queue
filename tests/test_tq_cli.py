"""
Test suite for the tq CLI tool.
Tests the command-line interface for running tasks and inspecting the queue.
"""

import json
import os
import subprocess
import sys
import tempfile
import time
from pathlib import Path

import pytest

# Path to tq.py
TQ_PATH = Path(__file__).parent.parent / "tq.py"


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
        lines = [l for l in result.stdout.strip().split("\n") if l]
        assert len(lines) == 3


class TestTqClear:
    """Tests for the tq clear command."""

    def test_clear_empty_queue(self, temp_data_dir):
        """Test clear command with empty queue."""
        # Initialize database by running a task that completes
        run_tq("echo", "init", data_dir=temp_data_dir)

        result = run_tq("clear", data_dir=temp_data_dir, timeout=5)

        assert result.returncode == 0
        assert "already empty" in result.stdout.lower()


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
