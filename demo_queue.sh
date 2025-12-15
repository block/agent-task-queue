#!/bin/bash
#
# Demo: Watch the task queue in action
#
# This script launches 3 Claude instances that all try to use the queue.
# Open another terminal and watch the queue with:
#   watch -n1 'sqlite3 /tmp/agent-task-queue/queue.db "SELECT * FROM queue;"'
#
# Prerequisites:
#   claude mcp add agent-task-queue -- uv run --directory /path/to/agent-task-queue python task_queue.py
#
# Usage: ./demo_queue.sh
#

set -e

SCRIPT_DIR="$(cd "$(dirname "$0")" && pwd)"
SYSTEM_PROMPT="You MUST use the run_task MCP tool. Use working_directory=/tmp and queue_name=demo"
ALLOWED_TOOLS="mcp__agent-task-queue__run_task"

# Check if claude CLI is available
if ! command -v claude &> /dev/null; then
    echo "Error: 'claude' CLI not found. Install Claude Code first."
    exit 1
fi

echo "=============================================="
echo "  Agent Task Queue Demo"
echo "=============================================="
echo ""
echo "Prerequisite: MCP server must be configured:"
echo "  claude mcp add agent-task-queue -- uv run --directory $SCRIPT_DIR python task_queue.py"
echo ""
echo "This will launch 3 Claude instances:"
echo "  - Task A: sleeps 10 seconds"
echo "  - Task B: sleeps 5 seconds (starts 2s after A)"
echo "  - Task C: echoes immediately (starts 4s after A)"
echo ""
echo "Expected behavior:"
echo "  - A runs first (10s)"
echo "  - B waits, then runs (5s)"
echo "  - C waits, then runs (instant)"
echo "  - Total time: ~15-20 seconds"
echo ""
echo "Watch the queue in another terminal:"
echo "  watch -n1 'sqlite3 /tmp/agent-task-queue/queue.db \"SELECT id, status, created_at FROM queue;\"'"
echo ""
read -p "Press Enter to start..."

# Clear queue (don't delete DB, just clear entries - avoids breaking running MCP servers)
cd "$SCRIPT_DIR"
uv run python -c "
from task_queue import init_db, get_db
init_db()
with get_db() as conn:
    conn.execute('DELETE FROM queue')
"

echo ""
echo "[$(date +%H:%M:%S)] Starting Task A (10 second sleep)..."
(
    claude -p "Use run_task to run 'echo TASK_A_START && sleep 10 && echo TASK_A_END' in /tmp with queue_name=demo" \
        --append-system-prompt "$SYSTEM_PROMPT" \
        --allowedTools "$ALLOWED_TOOLS"
    echo "[$(date +%H:%M:%S)] Task A finished"
) &

sleep 2

echo "[$(date +%H:%M:%S)] Starting Task B (5 second sleep)..."
(
    claude -p "Use run_task to run 'echo TASK_B_START && sleep 5 && echo TASK_B_END' in /tmp with queue_name=demo" \
        --append-system-prompt "$SYSTEM_PROMPT" \
        --allowedTools "$ALLOWED_TOOLS"
    echo "[$(date +%H:%M:%S)] Task B finished"
) &

sleep 2

echo "[$(date +%H:%M:%S)] Starting Task C (immediate echo)..."
(
    claude -p "Use run_task to run 'echo TASK_C_DONE' in /tmp with queue_name=demo" \
        --append-system-prompt "$SYSTEM_PROMPT" \
        --allowedTools "$ALLOWED_TOOLS"
    echo "[$(date +%H:%M:%S)] Task C finished"
) &

echo ""
echo "[$(date +%H:%M:%S)] All tasks submitted. Waiting for completion..."
echo ""

wait

echo ""
echo "=============================================="
echo "  Demo Complete"
echo "=============================================="
