"""Cross-platform compatibility tests for the Python package."""

import os
import subprocess
import sys
from pathlib import Path


REPO_ROOT = Path(__file__).resolve().parents[1]


def test_memory_reporting_does_not_require_posix_resource(tmp_path):
    script = """
import builtins

real_import = builtins.__import__

def import_without_resource(name, globals=None, locals=None, fromlist=(), level=0):
    if name == "resource" and globals and globals.get("__name__") == "task_queue":
        raise ModuleNotFoundError("No module named 'resource'")
    return real_import(name, globals, locals, fromlist, level)

builtins.__import__ = import_without_resource

import task_queue

assert task_queue.get_memory_mb() > 0

# Verify byte conversion and current RSS rather than a lifetime peak value.
from types import SimpleNamespace
from unittest.mock import patch

with patch.object(task_queue.psutil, "Process") as process:
    process.return_value.memory_info.return_value = SimpleNamespace(rss=3 * 1024 * 1024)
    assert task_queue.get_memory_mb() == 3.0
    process.assert_called_once_with(task_queue.os.getpid())
"""
    env = os.environ.copy()
    env["TASK_QUEUE_DATA_DIR"] = str(tmp_path)

    result = subprocess.run(
        [sys.executable, "-c", script],
        cwd=REPO_ROOT,
        env=env,
        capture_output=True,
        text=True,
        timeout=30,
    )

    assert result.returncode == 0, result.stderr
