"""
Test suite for Agent Task Queue Server.
Uses FastMCP's Client API for proper in-memory testing.
"""

import pytest
import asyncio
import os
import time
from pathlib import Path

# Set fast polling intervals for tests BEFORE importing task_queue
os.environ["TASK_QUEUE_POLL_WAITING"] = "0.1"
os.environ["TASK_QUEUE_POLL_READY"] = "0.1"

from datetime import datetime, timedelta
from fastmcp import Client
from task_queue import (
    mcp,
    PATHS,
    OUTPUT_DIR,
    get_db,
    init_db,
    clear_output_files,
    cleanup_queue,
    MAX_LOCK_AGE_MINUTES,
)

# Use PATHS for database path
DB_PATH = PATHS.db_path


@pytest.fixture(autouse=True)
def clean_db():
    """Clean database before each test."""
    if DB_PATH.exists():
        DB_PATH.unlink()
    # Also remove WAL files if present
    wal_path = Path(str(DB_PATH) + "-wal")
    shm_path = Path(str(DB_PATH) + "-shm")
    if wal_path.exists():
        wal_path.unlink()
    if shm_path.exists():
        shm_path.unlink()
    init_db()
    yield
    # Cleanup after test
    if DB_PATH.exists():
        DB_PATH.unlink()


@pytest.fixture
def client():
    """Create FastMCP client connected to our server."""
    return Client(mcp)


def read_output_file(result_str: str) -> str:
    """Extract output file path from result and read its contents."""
    import re

    match = re.search(r"output=([^\s\\]+\.log)", result_str)
    if match:
        path = match.group(1)
        if Path(path).exists():
            return Path(path).read_text()
    return ""


@pytest.mark.asyncio
async def test_single_task_execution(client):
    """Test that a single task executes successfully."""
    async with client:
        result = await client.call_tool(
            "run_task",
            {
                "command": "echo 'Hello World'",
                "working_directory": "/tmp",
                "queue_name": "test",
            },
        )

        output = str(result)
        assert "SUCCESS" in output
        assert "exit=0" in output

        # Verify output file contains actual output
        file_content = read_output_file(output)
        assert "Hello World" in file_content


@pytest.mark.asyncio
async def test_invalid_working_directory(client):
    """Test that invalid working directory returns error."""
    async with client:
        result = await client.call_tool(
            "run_task",
            {
                "command": "echo test",
                "working_directory": "/nonexistent/path/that/does/not/exist",
                "queue_name": "test",
            },
        )

        output = str(result)
        assert "ERROR" in output
        assert "does not exist" in output


@pytest.mark.asyncio
async def test_empty_command(client):
    """Test that empty command returns error."""
    async with client:
        result = await client.call_tool(
            "run_task",
            {
                "command": "",
                "working_directory": "/tmp",
                "queue_name": "test",
            },
        )

        output = str(result)
        assert "ERROR" in output
        assert "cannot be empty" in output

        # Also test whitespace-only command
        result2 = await client.call_tool(
            "run_task",
            {
                "command": "   ",
                "working_directory": "/tmp",
                "queue_name": "test",
            },
        )

        output2 = str(result2)
        assert "ERROR" in output2
        assert "cannot be empty" in output2


@pytest.mark.asyncio
async def test_command_timeout(client):
    """Test that long-running commands are killed after timeout."""
    async with client:
        result = await client.call_tool(
            "run_task",
            {
                "command": "sleep 10",
                "working_directory": "/tmp",
                "queue_name": "test",
                "timeout_seconds": 1,  # 1 second timeout
            },
        )

        output = str(result)
        assert "TIMEOUT" in output


@pytest.mark.asyncio
async def test_sequential_execution():
    """
    Test that two concurrent tasks execute sequentially.
    Task A starts first (takes 3s), Task B should wait.
    """
    results = {}
    start_times = {}
    end_times = {}

    async def run_task_a():
        client = Client(mcp)
        async with client:
            start_times["A"] = time.time()
            result = await client.call_tool(
                "run_task",
                {
                    "command": "sleep 2 && echo 'Task A done'",
                    "working_directory": "/tmp",
                    "queue_name": "sequential_test",
                },
            )
            end_times["A"] = time.time()
            results["A"] = str(result)

    async def run_task_b():
        # Small delay to ensure A gets queued first
        await asyncio.sleep(0.5)
        client = Client(mcp)
        async with client:
            start_times["B"] = time.time()
            result = await client.call_tool(
                "run_task",
                {
                    "command": "echo 'Task B done'",
                    "working_directory": "/tmp",
                    "queue_name": "sequential_test",
                },
            )
            end_times["B"] = time.time()
            results["B"] = str(result)

    # Run both tasks concurrently
    await asyncio.gather(run_task_a(), run_task_b())

    # Verify both completed successfully
    assert "SUCCESS" in results["A"]
    assert "SUCCESS" in results["B"]

    # Verify output files contain expected content
    assert "Task A done" in read_output_file(results["A"])
    assert "Task B done" in read_output_file(results["B"])

    # Verify B completed after A (sequential execution)
    assert end_times["B"] > end_times["A"] - 0.5, "Task B should complete after Task A"


@pytest.mark.asyncio
async def test_different_queues_isolation():
    """
    Test that tasks in different queues are isolated from each other.
    """
    client = Client(mcp)

    async with client:
        # First task in queue_alpha
        result1 = await client.call_tool(
            "run_task",
            {
                "command": "echo 'Queue Alpha'",
                "working_directory": "/tmp",
                "queue_name": "queue_alpha",
            },
        )
        assert "SUCCESS" in str(result1)
        assert "Queue Alpha" in read_output_file(str(result1))

        # Second task in queue_beta (different queue)
        result2 = await client.call_tool(
            "run_task",
            {
                "command": "echo 'Queue Beta'",
                "working_directory": "/tmp",
                "queue_name": "queue_beta",
            },
        )
        assert "SUCCESS" in str(result2)
        assert "Queue Beta" in read_output_file(str(result2))

        # Third task back in queue_alpha
        result3 = await client.call_tool(
            "run_task",
            {
                "command": "echo 'Queue Alpha Again'",
                "working_directory": "/tmp",
                "queue_name": "queue_alpha",
            },
        )
        assert "SUCCESS" in str(result3)
        assert "Queue Alpha Again" in read_output_file(str(result3))


@pytest.mark.asyncio
async def test_tool_available(client):
    """Test that the run_task tool is available."""
    async with client:
        tools = await client.list_tools()
        tool_names = [t.name for t in tools]
        assert "run_task" in tool_names


@pytest.mark.asyncio
async def test_environment_variables(client):
    """Test that environment variables are passed to the command."""
    async with client:
        result = await client.call_tool(
            "run_task",
            {
                "command": "echo $MY_TEST_VAR",
                "working_directory": "/tmp",
                "queue_name": "env_test",
                "env_vars": "MY_TEST_VAR=hello_from_env",
            },
        )

        output = str(result)
        assert "SUCCESS" in output
        assert "hello_from_env" in read_output_file(output)


@pytest.mark.asyncio
async def test_multiple_environment_variables(client):
    """Test that multiple environment variables work."""
    async with client:
        result = await client.call_tool(
            "run_task",
            {
                "command": "echo $VAR1 $VAR2 $VAR3",
                "working_directory": "/tmp",
                "queue_name": "env_test",
                "env_vars": "VAR1=one,VAR2=two,VAR3=three",
            },
        )

        output = str(result)
        assert "SUCCESS" in output
        assert "one two three" in read_output_file(output)


@pytest.mark.asyncio
async def test_exit_code_preserved(client):
    """Test that non-zero exit codes are captured."""
    async with client:
        result = await client.call_tool(
            "run_task",
            {
                "command": "exit 42",
                "working_directory": "/tmp",
                "queue_name": "exit_test",
            },
        )

        output = str(result)
        assert "FAILED" in output
        assert "exit=42" in output

        # Verify output file has exit code
        file_content = read_output_file(output)
        assert "EXIT CODE: 42" in file_content


@pytest.mark.asyncio
async def test_stderr_captured(client):
    """Test that stderr output is captured."""
    async with client:
        result = await client.call_tool(
            "run_task",
            {
                "command": "echo 'this is stderr' >&2",
                "working_directory": "/tmp",
                "queue_name": "stderr_test",
            },
        )

        output = str(result)
        assert "SUCCESS" in output

        file_content = read_output_file(output)
        assert "this is stderr" in file_content
        assert "STDERR" in file_content


@pytest.mark.asyncio
async def test_working_directory_respected(client):
    """Test that commands run in the specified directory."""
    async with client:
        result = await client.call_tool(
            "run_task",
            {"command": "pwd", "working_directory": "/tmp", "queue_name": "cwd_test"},
        )

        output = str(result)
        assert "SUCCESS" in output

        file_content = read_output_file(output)
        # On macOS, /tmp is a symlink to /private/tmp
        assert "/tmp" in file_content or "/private/tmp" in file_content


@pytest.mark.asyncio
async def test_command_with_special_characters(client):
    """Test that commands with special characters work."""
    async with client:
        result = await client.call_tool(
            "run_task",
            {
                "command": "echo 'hello \"world\"' && echo 'foo=bar'",
                "working_directory": "/tmp",
                "queue_name": "special_test",
            },
        )

        output = str(result)
        assert "SUCCESS" in output

        file_content = read_output_file(output)
        assert 'hello "world"' in file_content
        assert "foo=bar" in file_content


@pytest.mark.asyncio
async def test_long_output(client):
    """Test that long output is captured correctly in the file."""
    async with client:
        result = await client.call_tool(
            "run_task",
            {
                "command": 'for i in $(seq 1 100); do echo "Line $i"; done',
                "working_directory": "/tmp",
                "queue_name": "long_output_test",
            },
        )

        output = str(result)
        assert "SUCCESS" in output

        # Verify all lines are in the output file
        file_content = read_output_file(output)
        assert "Line 1" in file_content
        assert "Line 50" in file_content
        assert "Line 100" in file_content


@pytest.mark.asyncio
async def test_queue_clears_after_completion(client):
    """Test that the queue is empty after task completes."""
    async with client:
        # Run a task
        await client.call_tool(
            "run_task",
            {
                "command": "echo done",
                "working_directory": "/tmp",
                "queue_name": "clear_test",
            },
        )

    # Check queue is empty
    with get_db() as conn:
        count = conn.execute(
            "SELECT COUNT(*) as c FROM queue WHERE queue_name = 'clear_test'"
        ).fetchone()["c"]
        assert count == 0, "Queue should be empty after task completion"


@pytest.mark.asyncio
async def test_default_queue_name(client):
    """Test that default queue name 'global' is used when not specified."""
    async with client:
        result = await client.call_tool(
            "run_task",
            {
                "command": "echo 'using default queue'",
                "working_directory": "/tmp",
                # queue_name not specified, should default to "global"
            },
        )

        output = str(result)
        assert "SUCCESS" in output
        assert "using default queue" in read_output_file(output)


@pytest.mark.asyncio
async def test_clear_task_logs(client):
    """Test that clear_task_logs tool removes output files."""
    async with client:
        # Create some output files by running tasks
        await client.call_tool(
            "run_task",
            {
                "command": "echo test1",
                "working_directory": "/tmp",
                "queue_name": "cleanup_test",
            },
        )
        await client.call_tool(
            "run_task",
            {
                "command": "echo test2",
                "working_directory": "/tmp",
                "queue_name": "cleanup_test",
            },
        )

        # Verify output files exist
        assert len(list(OUTPUT_DIR.glob("task_*.log"))) >= 2

        # Call clear_task_logs
        result = await client.call_tool("clear_task_logs", {})
        output = str(result)
        assert "Deleted" in output

        # Verify files are gone
        assert len(list(OUTPUT_DIR.glob("task_*.log"))) == 0


@pytest.mark.asyncio
async def test_output_file_rotation():
    """Test that old output files are cleaned up when limit is exceeded."""
    from task_queue import MAX_OUTPUT_FILES

    # Clear any existing files
    clear_output_files()

    client = Client(mcp)
    async with client:
        # Create more files than the limit
        for i in range(MAX_OUTPUT_FILES + 5):
            await client.call_tool(
                "run_task",
                {
                    "command": f"echo 'task {i}'",
                    "working_directory": "/tmp",
                    "queue_name": "rotation_test",
                },
            )

        # Should only have MAX_OUTPUT_FILES files
        files = list(OUTPUT_DIR.glob("task_*.log"))
        assert len(files) <= MAX_OUTPUT_FILES


@pytest.mark.asyncio
async def test_oldest_logs_deleted_first():
    """Test that the oldest log files are deleted when rotation occurs."""
    from task_queue import MAX_OUTPUT_FILES

    # Clear any existing files
    clear_output_files()

    task_ids = []
    client = Client(mcp)
    async with client:
        # Create exactly MAX_OUTPUT_FILES + 3 tasks
        for i in range(MAX_OUTPUT_FILES + 3):
            result = await client.call_tool(
                "run_task",
                {
                    "command": f"echo 'task {i}'",
                    "working_directory": "/tmp",
                    "queue_name": "oldest_delete_test",
                },
            )
            # Extract task ID from output file path
            import re

            match = re.search(r"task_(\d+)\.log", str(result))
            if match:
                task_ids.append(int(match.group(1)))

    # Get remaining files
    remaining_files = list(OUTPUT_DIR.glob("task_*.log"))
    remaining_ids = []
    for f in remaining_files:
        import re

        match = re.search(r"task_(\d+)\.log", f.name)
        if match:
            remaining_ids.append(int(match.group(1)))

    # The first 3 task IDs should be gone (oldest deleted)
    for old_id in task_ids[:3]:
        assert old_id not in remaining_ids, f"Old task {old_id} should have been deleted"

    # The last MAX_OUTPUT_FILES task IDs should still exist
    for new_id in task_ids[-MAX_OUTPUT_FILES:]:
        assert new_id in remaining_ids, f"New task {new_id} should still exist"


def test_zombie_cleanup_dead_parent():
    """Test that tasks with dead parent PIDs are cleaned up."""
    # Insert a task with a definitely-dead PID (PID 1 is init, use a very high PID)
    dead_pid = 999999999  # This PID should not exist

    with get_db() as conn:
        conn.execute(
            """INSERT INTO queue (queue_name, status, pid, child_pid, created_at, updated_at)
               VALUES (?, ?, ?, ?, ?, ?)""",
            (
                "zombie_test",
                "running",
                dead_pid,
                None,
                datetime.now().isoformat(),
                datetime.now().isoformat(),
            ),
        )

        # Verify the task exists
        count_before = conn.execute(
            "SELECT COUNT(*) as c FROM queue WHERE queue_name = 'zombie_test'"
        ).fetchone()["c"]
        assert count_before == 1

        # Run cleanup
        cleanup_queue(conn, "zombie_test")

        # Verify the task was removed
        count_after = conn.execute(
            "SELECT COUNT(*) as c FROM queue WHERE queue_name = 'zombie_test'"
        ).fetchone()["c"]
        assert count_after == 0, "Dead parent task should be cleaned up"


def test_zombie_cleanup_stale_lock():
    """Test that tasks exceeding MAX_LOCK_AGE_MINUTES are cleaned up."""
    import os

    my_pid = os.getpid()  # Use our own PID so it's "alive"

    # Create a timestamp older than MAX_LOCK_AGE_MINUTES
    old_time = (datetime.now() - timedelta(minutes=MAX_LOCK_AGE_MINUTES + 10)).isoformat()

    with get_db() as conn:
        conn.execute(
            """INSERT INTO queue (queue_name, status, pid, child_pid, created_at, updated_at)
               VALUES (?, ?, ?, ?, ?, ?)""",
            (
                "stale_test",
                "running",
                my_pid,  # Use live PID to test timeout, not dead parent
                None,
                old_time,
                old_time,  # This is what triggers timeout cleanup
            ),
        )

        # Verify the task exists
        count_before = conn.execute(
            "SELECT COUNT(*) as c FROM queue WHERE queue_name = 'stale_test'"
        ).fetchone()["c"]
        assert count_before == 1

        # Run cleanup
        cleanup_queue(conn, "stale_test")

        # Verify the task was removed due to timeout
        count_after = conn.execute(
            "SELECT COUNT(*) as c FROM queue WHERE queue_name = 'stale_test'"
        ).fetchone()["c"]
        assert count_after == 0, "Stale lock should be cleaned up"


def test_zombie_cleanup_preserves_valid_tasks():
    """Test that cleanup doesn't remove valid running tasks."""
    import os
    from task_queue import _active_task_ids, _active_task_ids_lock

    my_pid = os.getpid()  # Use our own PID so it's "alive"

    with get_db() as conn:
        cursor = conn.execute(
            """INSERT INTO queue (queue_name, status, pid, child_pid, created_at, updated_at)
               VALUES (?, ?, ?, ?, ?, ?)""",
            (
                "valid_test",
                "running",
                my_pid,
                None,
                datetime.now().isoformat(),
                datetime.now().isoformat(),  # Recent timestamp
            ),
        )
        task_id = cursor.lastrowid

        # Register this task as active (simulating normal operation)
        with _active_task_ids_lock:
            _active_task_ids.add(task_id)

        try:
            # Verify the task exists
            count_before = conn.execute(
                "SELECT COUNT(*) as c FROM queue WHERE queue_name = 'valid_test'"
            ).fetchone()["c"]
            assert count_before == 1

            # Run cleanup
            cleanup_queue(conn, "valid_test")

            # Verify the task is still there (not removed)
            count_after = conn.execute(
                "SELECT COUNT(*) as c FROM queue WHERE queue_name = 'valid_test'"
            ).fetchone()["c"]
            assert count_after == 1, "Valid running task should NOT be cleaned up"
        finally:
            # Clean up for other tests
            with _active_task_ids_lock:
                _active_task_ids.discard(task_id)
            conn.execute("DELETE FROM queue WHERE queue_name = 'valid_test'")


def test_orphan_cleanup_dead_parent_waiting():
    """Test that waiting tasks with dead parent PIDs are cleaned up."""
    dead_pid = 999999999  # This PID should not exist

    with get_db() as conn:
        conn.execute(
            """INSERT INTO queue (queue_name, status, pid, child_pid, created_at, updated_at)
               VALUES (?, ?, ?, ?, ?, ?)""",
            (
                "orphan_test",
                "waiting",  # Key difference: this is a WAITING task, not running
                dead_pid,
                None,
                datetime.now().isoformat(),
                datetime.now().isoformat(),
            ),
        )

        # Verify the task exists
        count_before = conn.execute(
            "SELECT COUNT(*) as c FROM queue WHERE queue_name = 'orphan_test'"
        ).fetchone()["c"]
        assert count_before == 1

        # Run cleanup
        cleanup_queue(conn, "orphan_test")

        # Verify the orphaned waiting task was removed
        count_after = conn.execute(
            "SELECT COUNT(*) as c FROM queue WHERE queue_name = 'orphan_test'"
        ).fetchone()["c"]
        assert count_after == 0, "Orphaned waiting task should be cleaned up"


def test_orphan_cleanup_preserves_valid_waiting():
    """Test that cleanup doesn't remove valid waiting tasks."""
    import os
    from task_queue import _active_task_ids, _active_task_ids_lock

    my_pid = os.getpid()  # Use our own PID so it's "alive"

    with get_db() as conn:
        cursor = conn.execute(
            """INSERT INTO queue (queue_name, status, pid, child_pid, created_at, updated_at)
               VALUES (?, ?, ?, ?, ?, ?)""",
            (
                "valid_waiting_test",
                "waiting",
                my_pid,
                None,
                datetime.now().isoformat(),
                datetime.now().isoformat(),
            ),
        )
        task_id = cursor.lastrowid

        # Register this task as active (simulating normal operation)
        with _active_task_ids_lock:
            _active_task_ids.add(task_id)

        try:
            # Verify the task exists
            count_before = conn.execute(
                "SELECT COUNT(*) as c FROM queue WHERE queue_name = 'valid_waiting_test'"
            ).fetchone()["c"]
            assert count_before == 1

            # Run cleanup
            cleanup_queue(conn, "valid_waiting_test")

            # Verify the task is still there (not removed)
            count_after = conn.execute(
                "SELECT COUNT(*) as c FROM queue WHERE queue_name = 'valid_waiting_test'"
            ).fetchone()["c"]
            assert count_after == 1, "Valid waiting task should NOT be cleaned up"
        finally:
            # Clean up for other tests
            with _active_task_ids_lock:
                _active_task_ids.discard(task_id)
            conn.execute("DELETE FROM queue WHERE queue_name = 'valid_waiting_test'")


def test_orphan_cleanup_removes_untracked_task():
    """Test that cleanup removes tasks for our PID that aren't in the active set.

    This tests the fix for orphaned tasks left behind when MCP clients
    disconnect without proper cleanup (e.g., when sub-agents are cancelled).
    """
    import os

    my_pid = os.getpid()  # Use our own PID

    with get_db() as conn:
        conn.execute(
            """INSERT INTO queue (queue_name, status, pid, child_pid, created_at, updated_at)
               VALUES (?, ?, ?, ?, ?, ?)""",
            (
                "untracked_orphan_test",
                "waiting",
                my_pid,
                None,
                datetime.now().isoformat(),
                datetime.now().isoformat(),
            ),
        )

        # Do NOT add to _active_task_ids - simulating an orphaned task

        # Verify the task exists
        count_before = conn.execute(
            "SELECT COUNT(*) as c FROM queue WHERE queue_name = 'untracked_orphan_test'"
        ).fetchone()["c"]
        assert count_before == 1

        # Run cleanup
        cleanup_queue(conn, "untracked_orphan_test")

        # Verify the task was removed (it's orphaned - our PID but not tracked)
        count_after = conn.execute(
            "SELECT COUNT(*) as c FROM queue WHERE queue_name = 'untracked_orphan_test'"
        ).fetchone()["c"]
        assert count_after == 0, "Untracked task for our PID should be cleaned up"


def test_stale_server_instance_cleanup():
    """Test that cleanup removes tasks from old server instances even if PID is reused.

    This tests the fix for the edge case where:
    1. MCP server A creates tasks with PID 1234 and server_id "abc123"
    2. Server A dies
    3. A new process reuses PID 1234
    4. MCP server B starts with PID 1234 and server_id "xyz789"
    5. Server B's cleanup should remove Server A's orphaned tasks
    """
    import os

    my_pid = os.getpid()
    old_server_id = "old12345"  # Simulated old server instance

    with get_db() as conn:
        # Insert a task as if from an old server instance (same PID, different server_id)
        conn.execute(
            """INSERT INTO queue (queue_name, status, pid, server_id, created_at, updated_at)
               VALUES (?, ?, ?, ?, ?, ?)""",
            (
                "stale_server_test",
                "running",
                my_pid,  # Same PID as current process
                old_server_id,  # Different server_id
                datetime.now().isoformat(),
                datetime.now().isoformat(),
            ),
        )

        # Verify the task exists
        count_before = conn.execute(
            "SELECT COUNT(*) as c FROM queue WHERE queue_name = 'stale_server_test'"
        ).fetchone()["c"]
        assert count_before == 1

        # Run cleanup
        cleanup_queue(conn, "stale_server_test")

        # Verify the task was removed (different server_id means it's from old instance)
        count_after = conn.execute(
            "SELECT COUNT(*) as c FROM queue WHERE queue_name = 'stale_server_test'"
        ).fetchone()["c"]
        assert count_after == 0, "Task from old server instance should be cleaned up"


# --- Configuration Tests ---


def test_parse_args_defaults():
    """Test that parse_args returns correct defaults."""
    import sys
    from task_queue import parse_args

    # Save original argv and replace with empty args
    original_argv = sys.argv
    sys.argv = ["task_queue.py"]

    try:
        args = parse_args()
        assert args.data_dir == "/tmp/agent-task-queue"
        assert args.max_log_size == 5
        assert args.max_output_files == 50
        assert args.tail_lines == 50
        assert args.lock_timeout == 120
    finally:
        sys.argv = original_argv


def test_parse_args_data_dir():
    """Test --data-dir argument parsing."""
    import sys
    from task_queue import parse_args

    original_argv = sys.argv
    sys.argv = ["task_queue.py", "--data-dir=/custom/path"]

    try:
        args = parse_args()
        assert args.data_dir == "/custom/path"
    finally:
        sys.argv = original_argv


def test_parse_args_max_log_size():
    """Test --max-log-size argument parsing."""
    import sys
    from task_queue import parse_args

    original_argv = sys.argv
    sys.argv = ["task_queue.py", "--max-log-size=10"]

    try:
        args = parse_args()
        assert args.max_log_size == 10
    finally:
        sys.argv = original_argv


def test_parse_args_max_output_files():
    """Test --max-output-files argument parsing."""
    import sys
    from task_queue import parse_args

    original_argv = sys.argv
    sys.argv = ["task_queue.py", "--max-output-files=100"]

    try:
        args = parse_args()
        assert args.max_output_files == 100
    finally:
        sys.argv = original_argv


def test_parse_args_tail_lines():
    """Test --tail-lines argument parsing."""
    import sys
    from task_queue import parse_args

    original_argv = sys.argv
    sys.argv = ["task_queue.py", "--tail-lines=25"]

    try:
        args = parse_args()
        assert args.tail_lines == 25
    finally:
        sys.argv = original_argv


def test_parse_args_lock_timeout():
    """Test --lock-timeout argument parsing."""
    import sys
    from task_queue import parse_args

    original_argv = sys.argv
    sys.argv = ["task_queue.py", "--lock-timeout=60"]

    try:
        args = parse_args()
        assert args.lock_timeout == 60
    finally:
        sys.argv = original_argv


def test_parse_args_multiple_options():
    """Test multiple arguments together."""
    import sys
    from task_queue import parse_args

    original_argv = sys.argv
    sys.argv = [
        "task_queue.py",
        "--data-dir=/custom/data",
        "--max-log-size=20",
        "--max-output-files=200",
        "--tail-lines=100",
        "--lock-timeout=30",
    ]

    try:
        args = parse_args()
        assert args.data_dir == "/custom/data"
        assert args.max_log_size == 20
        assert args.max_output_files == 200
        assert args.tail_lines == 100
        assert args.lock_timeout == 30
    finally:
        sys.argv = original_argv
