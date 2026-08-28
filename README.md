# Agent Task Queue

[![CI](https://github.com/block/agent-task-queue/actions/workflows/ci.yml/badge.svg?branch=main)](https://github.com/block/agent-task-queue/actions/workflows/ci.yml)
[![PyPI version](https://img.shields.io/pypi/v/agent-task-queue)](https://pypi.org/project/agent-task-queue/)
[![GitHub release](https://img.shields.io/github/v/release/block/agent-task-queue)](https://github.com/block/agent-task-queue/releases)

**Local task queuing for AI agents.** Prevents multiple agents from running expensive operations concurrently and thrashing your machine.

## The Problem

When multiple AI agents work on the same machine, they independently trigger expensive operations. Running these concurrently causes:

- 5-minute builds stretching to 30+ minutes
- Memory thrashing and disk I/O saturation
- Machine unresponsiveness
- Agents unable to coordinate with each other

## How It Works

**Default: Global queue** - All `run_task` calls share one queue.

```
# Agent A runs:
run_task("./gradlew test", working_directory="/project")

# Agent B runs (waits for A to finish, then executes):
run_task("./gradlew build", working_directory="/project")
```

**Custom queues** - Use `queue_name` to isolate workloads:

```
# These run in separate queues (can run in parallel):
run_task("./gradlew build", queue_name="android", ...)
run_task("npm run build", queue_name="web", ...)
```

`run_task` returns the final result for short commands. For longer commands, it returns a task handle
after at most 30 seconds while the command continues in the queue. The agent can show progress inline,
accept steering, and call `task_status` for the next bounded update without rerunning the command.

**Hierarchical queues** - Use `/`-delimited queue names plus `--queue-capacity` when you need
parallelism with a shared cap:

```bash
uvx agent-task-queue@latest \
  --queue-capacity=gradle=2 \
  --queue-capacity=gradle/emu-5557=1 \
  --queue-capacity=gradle/emu-5559=1
```

```python
run_task("./gradlew assembleDebug assembleDebugAndroidTest", queue_name="gradle/build", ...)
run_task("./gradlew connectedDebugAndroidTest -x assembleDebug -x assembleDebugAndroidTest", queue_name="gradle/emu-5557", env_vars="ANDROID_SERIAL=127.0.0.1:5557", ...)
run_task("./gradlew connectedDebugAndroidTest -x assembleDebug -x assembleDebugAndroidTest", queue_name="gradle/emu-5559", env_vars="ANDROID_SERIAL=127.0.0.1:5559", ...)
```

Queue capacities apply to each command for its entire lifetime. For Android-style workflows, that
usually means queueing shared Gradle prep/build first, then fan out emulator-specific commands that
reuse those outputs; see [Android Multi-Emulator Pattern](#android-multi-emulator-pattern).

Configured capacities apply to a scope and all of its descendants. In the example above, the
shared `gradle` scope allows at most two concurrent Gradle-backed tasks, while each emulator leaf
queue remains exclusive. If multiple servers or `tq` CLI invocations share the same data
directory, start them with matching `--queue-capacity` flags; these overrides are process-local and
are not persisted in `queue.db`. If you do not configure any capacities, behavior is unchanged:
each exact `queue_name` is still a FIFO queue with capacity 1.

## Demo: Two Agents, One Build Queue

**Terminal A** - First agent requests an Android build:
```
> Build the Android app

⏺ agent-task-queue - run_task (MCP)
  command: "./gradlew assembleDebug"
  working_directory: "/path/to/android-project"

  ⎿  "RUNNING task_id=1 queue=global elapsed=30.0s process_alive=true ..."

⏺ agent-task-queue - task_status (MCP)
  task_id: 1
  output_offset: 4821

  ... additional bounded task_status calls while the build runs ...

  ⎿  "SUCCESS task_id=1 exit=0 192.6s output=/tmp/agent-task-queue/output/task_1.log"

⏺ Build completed successfully in 192.6s.
```

**Terminal B** - Second agent requests the same build (started 2 seconds after A):
```
> Build the Android app

⏺ agent-task-queue - run_task (MCP)
  command: "./gradlew assembleDebug"
  working_directory: "/path/to/android-project"

  ⎿  "QUEUED task_id=2 queue=global elapsed=30.0s position=1 ..."

⏺ agent-task-queue - task_status (MCP)
  task_id: 2

  ... additional bounded task_status calls while queued and running ...

  ⎿  "SUCCESS task_id=2 exit=0 32.6s output=/tmp/agent-task-queue/output/task_2.log"

⏺ Build completed successfully in 32.6s.
```

**What happened behind the scenes:**

| Time | Agent A | Agent B |
|------|---------|---------|
| 0:00 | Started build | |
| 0:02 | Building... | Entered queue, waiting |
| 3:12 | **Completed** (192.6s) | Started build |
| 3:45 | | **Completed** (32.6s) |

**Why this matters:**

Without the queue, both builds would run simultaneously—fighting for CPU, memory, and disk I/O. Each build might take 5+ minutes, and your machine would be unresponsive.

With the queue:
- **Agent B automatically waited** for Agent A to finish
- **Agent B's build was 6x faster** (32s vs 193s) because Gradle reused cached artifacts
- **Total time: 3:45** instead of 10+ minutes of thrashing
- **Your machine stayed responsive** throughout

## Key Features

- **FIFO Queuing**: Strict first-in-first-out ordering within each exact `queue_name`
- **Bounded Inline Progress**: Long calls yield a task handle within 30 seconds; `task_status` returns on output, a state change, completion, or another 30-second heartbeat
- **No Queue-Wait Timeouts**: Commands can remain queued indefinitely. `timeout_seconds` starts only when execution begins.
- **Explicit Cancellation**: Interrupting `run_task` or `task_status` leaves the command running; only `cancel_task` stops it
- **Environment Variables**: Pass `env_vars="ANDROID_SERIAL=emulator-5560"`
- **Multiple Queues**: Isolate different workloads with `queue_name`
- **Zombie Protection**: Detects dead processes, kills orphans, clears stale locks
- **Auto-Kill**: Tasks running > 120 minutes are terminated

## Desktop Sidecar

The repo also includes an optional Compose Multiplatform desktop app in [desktop-sidecar](desktop-sidecar/README.md) for watching the queue in real time. It is not required for inline agent progress.

It reads the same local SQLite database as `tq` and the IntelliJ plugin, then shows:

- running tasks
- waiting tasks
- exact queues grouped by their root scope (for example `gradle/build` and `gradle/emulator-5554` under `gradle`)

Run it with:

```bash
cd desktop-sidecar
./gradlew run
```

Or point it at a different queue data directory:

```bash
cd desktop-sidecar
./gradlew run --args="--data-dir /path/to/agent-task-queue"
```

The sidecar defaults to `$TASK_QUEUE_DATA_DIR` or `/tmp/agent-task-queue`. It visualizes live occupancy from `queue.db`; configured `--queue-capacity` limits are process-local and are not stored in SQLite, so the UI shows live tasks and queue topology rather than persisted capacity settings.

## Installation

```bash
uvx agent-task-queue@latest
```

That's it. [uvx](https://docs.astral.sh/uv/guides/tools/) runs the package directly from PyPI—no clone, no install, no virtual environment.

## Agent Configuration

Agent Task Queue works with any AI coding tool that supports MCP. Add this config to your MCP client:

```json
{
  "mcpServers": {
    "agent-task-queue": {
      "command": "uvx",
      "args": ["agent-task-queue@latest"]
    }
  }
}
```

### MCP Client Configuration

<details>
<summary>Amp</summary>

Install via CLI:

```bash
amp mcp add agent-task-queue -- uvx agent-task-queue@latest
```

Or add to `.amp/settings.json` (workspace) or global settings. See [Amp Manual](https://ampcode.com/manual) for details.

</details>

<details>
<summary>Claude Code</summary>

Install via CLI (<a href="https://docs.anthropic.com/en/docs/claude-code/mcp">guide</a>):

```bash
claude mcp add agent-task-queue -- uvx agent-task-queue@latest
```

</details>

<details>
<summary>Claude Desktop</summary>

Config file locations:
- **macOS**: `~/Library/Application Support/Claude/claude_desktop_config.json`
- **Linux**: `~/.config/Claude/claude_desktop_config.json`

Use the standard config above.

</details>

<details>
<summary>Cline</summary>

Open the MCP Servers panel > Configure > "Configure MCP Servers" to edit `cline_mcp_settings.json`. Use the standard config above.

See [Cline MCP docs](https://docs.cline.bot/mcp/configuring-mcp-servers) for details.

</details>

<details>
<summary>Codex</summary>

Install via CLI (<a href="https://developers.openai.com/codex/mcp">guide</a>):

```bash
codex mcp add agent-task-queue -- uvx agent-task-queue@latest
```

</details>

<details>
<summary>Copilot / VS Code</summary>

Requires VS Code 1.102+ with GitHub Copilot Chat extension.

Config file locations:
- **Workspace**: `.vscode/mcp.json`
- **Global**: Via Command Palette > "MCP: Open User Configuration"

```json
{
  "servers": {
    "agent-task-queue": {
      "type": "stdio",
      "command": "uvx",
      "args": ["agent-task-queue@latest"]
    }
  }
}
```

See [VS Code MCP docs](https://code.visualstudio.com/docs/copilot/chat/mcp-servers) for details.

</details>

<details>
<summary>Cursor</summary>

Go to `Cursor Settings` > `MCP` > `+ Add new global MCP server`. Use the standard config above.

Config file locations:
- **Global**: `~/.cursor/mcp.json`
- **Project**: `.cursor/mcp.json`

See [Cursor MCP docs](https://docs.cursor.com/context/model-context-protocol) for details.

</details>

<details>
<summary>Firebender</summary>

Add to `firebender.json` in project root, or use Plugin Settings > MCP section. Use the standard config above.

See [Firebender MCP docs](https://docs.firebender.com/context/mcp) for details.

</details>

<details>
<summary>Windsurf</summary>

Config file location: `~/.codeium/windsurf/mcp_config.json`

Or use Windsurf Settings > Cascade > Manage MCPs. Use the standard config above.

See [Windsurf MCP docs](https://docs.windsurf.com/windsurf/cascade/mcp) for details.

</details>

## Usage

Agents use the `run_task` MCP tool for expensive operations:

**Build Tools:** gradle, bazel, make, cmake, mvn, cargo build, go build, npm/yarn/pnpm build

**Container Operations:** docker build, docker-compose, podman, kubectl, helm

**Test Suites:** pytest, jest, mocha, rspec

> **Note:** Some agents automatically prefer MCP tools (Amp, Copilot, Windsurf). Others may need [configuration](#agent-configuration-notes) to prefer `run_task` over built-in shell commands.

### Tool Parameters

| Parameter | Required | Description |
|-----------|----------|-------------|
| `command` | Yes | Shell command to execute |
| `working_directory` | Yes | Absolute path to run from |
| `queue_name` | No | Queue identifier (default: "global") |
| `timeout_seconds` | No | Max **execution** time before kill (default: 1200). Queue wait time doesn't count. |
| `env_vars` | No | Environment variables: `"KEY=val,KEY2=val2"` |
| `wait_seconds` | No | Initial wait before yielding a background handle (0–30, default: 30) |

`queue_name` may be hierarchical, such as `gradle/emu-5557`, when the server is configured with
`--queue-capacity` scopes.

Sibling queues that share a parent scope compete for that parent capacity on a best-effort basis;
FIFO ordering is guaranteed within each exact queue, not across sibling queues.

### Long-Running Tasks and Inline Progress

A command that does not finish during the initial bounded wait returns its state, task ID, queue
position or process liveness, elapsed time, recent output, and `next_output_offset`:

```text
RUNNING task_id=42 queue=gradle/build elapsed=30.0s process_alive=true last_output=2.1s_ago

--- NEW OUTPUT ---
> Task :app:compileDebugKotlin

Task continues in the background. Do not rerun it. Call task_status(task_id=42,
output_offset=1234) for the next update, or cancel_task(task_id=42) to stop it.
```

Call `task_status` with the returned offset. Each status call returns as soon as output or state
changes, when the command finishes, or after at most 30 seconds as a liveness heartbeat. This makes
progress visible in the normal agent TUI or GUI transcript and gives the agent regular boundaries at
which it can absorb steering. MCP progress notifications are also emitted when supported, but the
bounded tool results do not depend on clients rendering those notifications.

Cancelling or steering away from a `run_task`/`task_status` wait does **not** kill the command. Use
`cancel_task(task_id=42)` when termination is intended. Terminal results remain queryable after the
active queue row is released.

### Example

```
run_task(
    command="./gradlew connectedAndroidTest",
    working_directory="/project",
    queue_name="android",
    env_vars="ANDROID_SERIAL=emulator-5560"
)
```

### Android Multi-Emulator Pattern

If your machine can safely run a small number of Gradle-backed device tests in parallel, use a
shared Gradle scope plus one queue per emulator. Because queue capacities only see whole commands,
the practical pattern is to split shared Gradle prep/build from emulator-specific execution.

First, queue the shared Gradle prep/build once:

```python
run_task(
    command="./gradlew assembleDebug assembleDebugAndroidTest",
    working_directory="/project",
    queue_name="gradle/build",
)
```

Then fan out one task per emulator using a command that reuses those prebuilt outputs:

```bash
uvx agent-task-queue@latest \
  --queue-capacity=gradle=2 \
  --queue-capacity=gradle/emu-5557=1 \
  --queue-capacity=gradle/emu-5559=1 \
  --queue-capacity=gradle/emu-5561=1
```

Then pin each task to the matching queue and `ANDROID_SERIAL`:

```python
run_task(
    command="./gradlew connectedDebugAndroidTest -x assembleDebug -x assembleDebugAndroidTest",
    working_directory="/project",
    queue_name="gradle/emu-5557",
    env_vars="ANDROID_SERIAL=127.0.0.1:5557",
)
```

Adapt the exact Gradle tasks and `-x` exclusions to your project. The key is that the second step
must reuse the outputs from the shared prep step instead of rebuilding them in every emulator queue.

If your emulator execution phase no longer needs Gradle at all, queue it outside the shared
`gradle` scope entirely so only the build/prep step consumes shared Gradle capacity.

When multiple entrypoints share this queue database, they must all use the same
`--queue-capacity` configuration for the shared parent caps to mean the same thing.

### Agent Configuration Notes

Some agents need additional configuration to use the queue instead of built-in shell commands.

| Agent | Extra Setup | Notes |
|-------|-------------|-------|
| Amp, Copilot, Windsurf | ❌ None | Works out of the box |
| **Claude Code, Cursor** | ✅ Required | Must remove Bash allowed rules |
| Cline, Firebender | ⚠️ Maybe | Check agent docs |

> [!IMPORTANT]
> **Claude Code users:** If you have allowed rules like `Bash(gradle:*)` or `Bash(./gradlew:*)`, the agent will use Bash directly and **bypass the queue entirely**. You must remove these rules for the queue to work.
>
> Check both `settings.json` and `settings.local.json` (project and global) for rules like:
> - `Bash(gradle:*)`, `Bash(./gradlew:*)`, `Bash(ANDROID_SERIAL=* ./gradlew:*)`
> - `Bash(docker build:*)`, `Bash(pytest:*)`, etc.
>
> See [Claude Code setup guide](examples/claude-code/SETUP.md) for the full fix.

#### Quick Agent Setup

After installing the MCP server, tell your agent:

```
"Configure agent-task-queue - use examples/<agent-name>/SETUP.md if available"
```

**Available setup guides:**
- [Claude Code setup](examples/claude-code/SETUP.md) - 3-step configuration
- [Other agents](examples/) - Contributions welcome!

## Configuration

The server supports the following command-line options:

| Option | Default | Description |
|--------|---------|-------------|
| `--data-dir` | `/tmp/agent-task-queue` | Directory for database and logs |
| `--max-log-size` | `5` | Max metrics log size in MB before rotation |
| `--max-output-files` | `50` | Number of task output files to retain |
| `--tail-lines` | `50` | Lines of output to include on failure |
| `--lock-timeout` | `120` | Minutes before stale locks are cleared |
| `--queue-capacity` | none | Repeatable `scope=capacity` override for hierarchical queue names |

Pass options via the `args` property in your MCP config:

```json
{
  "mcpServers": {
    "agent-task-queue": {
      "command": "uvx",
      "args": [
        "agent-task-queue@latest",
        "--max-output-files=100",
        "--lock-timeout=60",
        "--queue-capacity=gradle=2",
        "--queue-capacity=gradle/emu-5557=1"
      ]
    }
  }
}
```

Run `uvx agent-task-queue@latest --help` to see all options.

## IntelliJ Plugin

An optional [IntelliJ plugin](intellij-plugin/) provides an additional status bar widget, tool window, and notifications. It is separate from the inline `run_task`/`task_status` output available in any MCP agent client. See the [plugin README](intellij-plugin/README.md) for details.

## Architecture

```mermaid
flowchart TD
    A[AI Agent<br/>Claude, Cursor, Windsurf, etc.] -->|MCP Protocol| B[task_queue.py<br/>FastMCP Server]
    B -->|Query/Update| C[(SQLite Queue<br/>/tmp/agent-task-queue/queue.db)]
    B -->|Execute| D[Subprocess<br/>gradle, docker, etc.]

    D -.->|stdout/stderr| B
    B -.->|terminal result or bounded status handle| A
```

### Data Directory

All data is stored in `/tmp/agent-task-queue/` by default:
- `queue.db` - SQLite database for queue state
- `agent-task-queue-logs.json` - JSON metrics log (NDJSON format)
- `output/task_<id>.log` and `.raw.log` - Full and incrementally readable task output

To use a different location, pass `--data-dir=/path/to/data` or set the `TASK_QUEUE_DATA_DIR` environment variable.

### Database Schema

Active queue state is stored in the `queue` table at `/tmp/agent-task-queue/queue.db`:

| Column | Type | Description |
|--------|------|-------------|
| `id` | INTEGER | Auto-incrementing primary key |
| `queue_name` | TEXT | Queue identifier (e.g., "global", "android") |
| `status` | TEXT | Task state: "waiting" or "running" |
| `command` | TEXT | Shell command being executed |
| `pid` | INTEGER | MCP server process ID (for liveness check) |
| `server_id` | TEXT | Server instance UUID (for orphan detection across PID reuse) |
| `child_pid` | INTEGER | Subprocess ID (for orphan cleanup) |
| `created_at` | TIMESTAMP | When task was queued |
| `updated_at` | TIMESTAMP | Last status change |

The `task_results` table stores each task's terminal result as JSON after its active `queue` row is
released. This lets `task_status` report completion without holding a queue slot. Terminal results
are retained with the configured output-file limit.

### Zombie Protection

If an agent crashes while a task is running:
1. The next task detects the dead parent process (via PID check)
2. It kills any orphaned child process (the actual build)
3. It clears the stale lock
4. Execution continues normally

### Metrics Logging

All queue events are logged to `agent-task-queue-logs.json` in NDJSON format (one JSON object per line):

```json
{"event":"task_queued","timestamp":"2025-12-12T16:01:34","task_id":8,"queue_name":"global","pid":23819}
{"event":"task_started","timestamp":"2025-12-12T16:01:34","task_id":8,"queue_name":"global","wait_time_seconds":0.0}
{"event":"task_completed","timestamp":"2025-12-12T16:02:05","task_id":8,"queue_name":"global","command":"./gradlew build","exit_code":0,"duration_seconds":31.2,"stdout_lines":45,"stderr_lines":2}
```

**Events logged:**
- `task_queued` - Task entered the queue
- `task_started` - Task acquired lock and began execution
- `task_completed` - Task finished (includes exit code and duration)
- `task_timeout` - Task killed after timeout
- `task_error` - Task failed with exception
- `zombie_cleared` - Stale lock was cleaned up

The log file rotates when it exceeds 5MB (keeps one backup as `.json.1`).

### Task Output Logs

To reduce token usage, full command output is written to files instead of returned directly:

```
/tmp/agent-task-queue/output/
├── task_1.log         # Formatted log with metadata and section markers
├── task_1.raw.log     # Raw stdout+stderr only (for plugin streaming)
├── task_2.log
├── task_2.raw.log
└── ...
```

Each task produces two output files:
- **`task_<id>.log`** — Formatted log with headers (`COMMAND:`, `WORKING DIR:`), compatibility section markers (`--- STDOUT ---`, `--- STDERR ---`, `--- SUMMARY ---`), and exit code. Stdout and stderr content may be interleaved because both pipes are drained concurrently to prevent subprocess deadlocks.
- **`task_<id>.raw.log`** — Raw interleaved stdout+stderr with no metadata. Used for incremental `task_status` output and optional viewer streaming.

**On success**, the tool returns a single line:
```
SUCCESS task_id=8 exit=0 31.2s command=./gradlew build output=/tmp/agent-task-queue/output/task_8.log
```

**On failure**, the last 50 lines of output are included:
```
FAILED task_id=9 exit=1 12.5s command=./gradlew build output=/tmp/agent-task-queue/output/task_9.log
[error output here]
```

**Automatic cleanup**: Old files are deleted when count exceeds 50 tasks (configurable via `--max-output-files`).

**Manual cleanup**: Use the `clear_task_logs` tool to delete all output files.

## CLI Tool

The `tq` command lets you run commands through the queue and inspect queue status.

### Install CLI

```bash
uv tool install agent-task-queue
```

This installs both the MCP server and the `tq` CLI persistently.

### Running Commands

Run commands through the same queue that agents use:

```bash
tq ./gradlew assembleDebug          # Run a build through the queue
tq npm run build                    # Any command works
tq -q android ./gradlew test        # Use a specific queue
tq -t 600 npm test                  # Custom timeout (seconds)
tq -C /path/to/project make         # Set working directory
```

This prevents resource contention between you and AI agents - when you run a build via `tq`, any agent-initiated builds will wait in the same queue.

### Inspecting the Queue

```bash
tq list              # Show current queue
tq logs              # Show recent activity
tq logs -n 50        # Show last 50 entries
tq clear             # Clear stuck tasks
tq --data-dir PATH   # Use custom data directory
```

Respects `TASK_QUEUE_DATA_DIR` environment variable.

> **Note:** Without installing, you can run one-off commands with:
> ```bash
> uvx --from agent-task-queue tq list
> ```

## Troubleshooting

### Tasks stuck in queue

```bash
tq list    # Check queue status
tq clear   # Clear all tasks
```

### "Database is locked" errors

```bash
ps aux | grep task_queue                  # Check for zombie processes
rm -rf /tmp/agent-task-queue/             # Delete and restart
```

### Server not connecting

1. Ensure `uvx` is in your PATH (install [uv](https://github.com/astral-sh/uv) if needed)
2. Test manually: `uvx agent-task-queue@latest`

## Development

For contributors:

```bash
git clone https://github.com/block/agent-task-queue.git
cd agent-task-queue
uv sync                      # Install dependencies
uv run pytest -v             # Run tests
uv run python task_queue.py  # Run server locally
```

## Platform Support

- macOS
- Linux

## Why MCP Instead of a CLI Tool?

The first attempt at solving this problem was a file-based queue CLI that wrapped commands:

```bash
queue-cli ./gradlew build
```

**The fatal flaw:** AI tools have built-in shell timeouts (30s-120s). If a job waited in queue longer than the timeout, the agent gave up—even though the job would eventually run.

```mermaid
flowchart LR
    subgraph cli [CLI Approach]
        A1[Agent] --> B1[Shell]
        B1 --> C1[CLI]
        C1 --> D1[Queue]
        B1 -.-> |"⏱️ TIMEOUT!"| A1
    end

    subgraph mcp [MCP Approach]
        A2[Agent] --> |MCP Protocol| B2[Server]
        B2 --> C2[Queue]
        B2 -.-> |"✓ bounded inline updates"| A2
    end
```

**Why MCP solves this:**
- The MCP server owns the queued command independently from any one tool request
- Long calls return a handle within 30 seconds instead of depending on a long client timeout
- The agent receives incremental inline output and can accept steering between status calls
- An interrupted status request does not cancel the command; cancellation is explicit

| Aspect | CLI Wrapper | Agent Task Queue |
|--------|-------------|----------------|
| Timeout handling | External workarounds | Solved by design |
| Queue storage | Filesystem | SQLite (WAL mode) |
| Integration | Wrap every command | Automatic tool selection |
| Agent compatibility | Varies by tool | Universal |

## License

Apache 2.0
