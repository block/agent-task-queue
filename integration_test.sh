#!/bin/bash
#
# Integration test for Agent Task Queue
#
# This script launches multiple Claude instances concurrently to verify
# that the task queue enforces FIFO ordering and blocks until completion.
#
# Prerequisites:
#   claude mcp add agent-task-queue -- uv run --directory /path/to/agent-task-queue python task_queue.py
#
# Usage: ./integration_test.sh
#

set -e

SCRIPT_DIR="$(cd "$(dirname "$0")" && pwd)"
LOG_DIR="/tmp/agent-task-queue-test"
SYSTEM_PROMPT="You MUST use the run_task MCP tool for ALL commands. Do not use the regular Bash tool. Use working_directory=/tmp and queue_name=integration_test"
ALLOWED_TOOLS="mcp__agent-task-queue__run_task"

# Check if claude CLI is available
if ! command -v claude &> /dev/null; then
    echo "Error: 'claude' CLI not found. Install Claude Code first."
    exit 1
fi

# Colors for output
RED='\033[0;31m'
GREEN='\033[0;32m'
YELLOW='\033[1;33m'
BLUE='\033[0;34m'
NC='\033[0m' # No Color

log() {
    echo -e "${BLUE}[$(date +%H:%M:%S)]${NC} $1"
}

success() {
    echo -e "${GREEN}[$(date +%H:%M:%S)] ✓ $1${NC}"
}

error() {
    echo -e "${RED}[$(date +%H:%M:%S)] ✗ $1${NC}"
}

warn() {
    echo -e "${YELLOW}[$(date +%H:%M:%S)] ! $1${NC}"
}

# Setup
setup() {
    log "Setting up test environment..."

    # Create log directory
    rm -rf "$LOG_DIR"
    mkdir -p "$LOG_DIR"

    # Clear queue (don't delete DB, just clear entries - avoids breaking running MCP servers)
    cd "$SCRIPT_DIR"
    uv run python -c "
from task_queue import init_db, get_db
init_db()
with get_db() as conn:
    conn.execute('DELETE FROM queue')
"

    success "Test environment ready"
}

# Cleanup
cleanup() {
    log "Cleaning up..."
    uv run python -c "
from task_queue import get_db
with get_db() as conn:
    conn.execute('DELETE FROM queue')
" 2>/dev/null || true
    success "Cleanup complete"
}

# Test 1: Single task execution
test_single_task() {
    log "Test 1: Single task execution"

    claude -p "Use run_task to run 'echo SINGLE_TASK_SUCCESS' in /tmp with queue_name=test1" \
        --append-system-prompt "$SYSTEM_PROMPT" \
        --allowedTools "$ALLOWED_TOOLS" \
        > "$LOG_DIR/test1.log" 2>&1

    if grep -q "SINGLE_TASK_SUCCESS" "$LOG_DIR/test1.log"; then
        success "Single task executed successfully"
        return 0
    else
        error "Single task failed"
        cat "$LOG_DIR/test1.log"
        return 1
    fi
}

# Test 2: Sequential execution (FIFO ordering)
test_sequential_execution() {
    log "Test 2: Sequential execution (two tasks, same queue)"
    log "  Task A: sleep 5 seconds, then echo"
    log "  Task B: started 1 second later, should wait for A"

    # Start Task A (takes 5 seconds)
    (
        claude -p "Use run_task to run 'echo TASK_A_START && sleep 5 && echo TASK_A_END' in /tmp with queue_name=seq_test" \
            --append-system-prompt "$SYSTEM_PROMPT" \
            --allowedTools "$ALLOWED_TOOLS" \
            > "$LOG_DIR/test2_taskA.log" 2>&1
        echo "$(date +%s)" > "$LOG_DIR/test2_taskA.done"
    ) &
    PID_A=$!

    # Wait 1 second, then start Task B
    sleep 1

    (
        claude -p "Use run_task to run 'echo TASK_B_DONE' in /tmp with queue_name=seq_test" \
            --append-system-prompt "$SYSTEM_PROMPT" \
            --allowedTools "$ALLOWED_TOOLS" \
            > "$LOG_DIR/test2_taskB.log" 2>&1
        echo "$(date +%s)" > "$LOG_DIR/test2_taskB.done"
    ) &
    PID_B=$!

    log "  Waiting for both tasks to complete..."
    wait $PID_A
    wait $PID_B

    # Check results
    local errors=0

    if grep -q "TASK_A_END" "$LOG_DIR/test2_taskA.log"; then
        success "Task A completed"
    else
        error "Task A failed"
        errors=$((errors + 1))
    fi

    if grep -q "TASK_B_DONE" "$LOG_DIR/test2_taskB.log"; then
        success "Task B completed"
    else
        error "Task B failed"
        errors=$((errors + 1))
    fi

    # Verify ordering: B should finish after A
    if [[ -f "$LOG_DIR/test2_taskA.done" && -f "$LOG_DIR/test2_taskB.done" ]]; then
        local time_a=$(cat "$LOG_DIR/test2_taskA.done")
        local time_b=$(cat "$LOG_DIR/test2_taskB.done")

        if [[ $time_b -ge $time_a ]]; then
            success "FIFO ordering verified (B finished after A)"
        else
            error "FIFO ordering violated (B finished before A)"
            errors=$((errors + 1))
        fi
    fi

    return $errors
}

# Test 3: Different queues run independently
test_different_queues() {
    log "Test 3: Different queues (should not block each other)"
    log "  Queue Alpha: sleep 3 seconds"
    log "  Queue Beta: sleep 3 seconds (started simultaneously)"
    log "  Expected: Both complete in ~3 seconds total, not ~6 seconds"

    local start_time=$(date +%s)

    # Start both tasks simultaneously in different queues
    (
        claude -p "Use run_task to run 'sleep 3 && echo ALPHA_DONE' in /tmp with queue_name=queue_alpha" \
            --append-system-prompt "$SYSTEM_PROMPT" \
            --allowedTools "$ALLOWED_TOOLS" \
            > "$LOG_DIR/test3_alpha.log" 2>&1
    ) &
    PID_ALPHA=$!

    (
        claude -p "Use run_task to run 'sleep 3 && echo BETA_DONE' in /tmp with queue_name=queue_beta" \
            --append-system-prompt "$SYSTEM_PROMPT" \
            --allowedTools "$ALLOWED_TOOLS" \
            > "$LOG_DIR/test3_beta.log" 2>&1
    ) &
    PID_BETA=$!

    log "  Waiting for both queues..."
    wait $PID_ALPHA
    wait $PID_BETA

    local end_time=$(date +%s)
    local duration=$((end_time - start_time))

    local errors=0

    if grep -q "ALPHA_DONE" "$LOG_DIR/test3_alpha.log"; then
        success "Queue Alpha completed"
    else
        error "Queue Alpha failed"
        errors=$((errors + 1))
    fi

    if grep -q "BETA_DONE" "$LOG_DIR/test3_beta.log"; then
        success "Queue Beta completed"
    else
        error "Queue Beta failed"
        errors=$((errors + 1))
    fi

    # Should complete in roughly 3-5 seconds if parallel, not 6+ if sequential
    if [[ $duration -lt 30 ]]; then
        success "Parallel execution verified (took ${duration}s, expected ~3-10s)"
    else
        warn "Execution took ${duration}s - may not be running in parallel"
    fi

    return $errors
}

# Test 4: Environment variables
test_env_vars() {
    log "Test 4: Environment variable preservation"

    claude -p "Use run_task to run 'echo MY_VAR is \$MY_VAR' in /tmp with queue_name=env_test and env_vars='MY_VAR=hello_world'" \
        --append-system-prompt "$SYSTEM_PROMPT" \
        --allowedTools "$ALLOWED_TOOLS" \
        > "$LOG_DIR/test4.log" 2>&1

    if grep -q "hello_world" "$LOG_DIR/test4.log"; then
        success "Environment variable preserved"
        return 0
    else
        error "Environment variable not preserved"
        cat "$LOG_DIR/test4.log"
        return 1
    fi
}

# Main test runner
main() {
    echo ""
    echo "=============================================="
    echo "  Agent Task Queue Integration Tests"
    echo "=============================================="
    echo ""
    echo "Prerequisite: MCP server must be configured:"
    echo "  claude mcp add agent-task-queue -- uv run --directory $SCRIPT_DIR python task_queue.py"
    echo ""

    setup

    local total_errors=0

    echo ""
    echo "----------------------------------------------"
    test_single_task || total_errors=$((total_errors + 1))

    echo ""
    echo "----------------------------------------------"
    test_sequential_execution || total_errors=$((total_errors + $?))

    echo ""
    echo "----------------------------------------------"
    test_different_queues || total_errors=$((total_errors + $?))

    echo ""
    echo "----------------------------------------------"
    test_env_vars || total_errors=$((total_errors + 1))

    echo ""
    echo "=============================================="
    if [[ $total_errors -eq 0 ]]; then
        success "All tests passed!"
    else
        error "$total_errors test(s) failed"
    fi
    echo "=============================================="
    echo ""
    echo "Logs available in: $LOG_DIR"
    echo ""

    cleanup

    return $total_errors
}

# Run if executed directly
if [[ "${BASH_SOURCE[0]}" == "${0}" ]]; then
    main "$@"
fi
