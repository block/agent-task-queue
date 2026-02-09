# IntelliJ Plugin

An IntelliJ IDEA plugin that provides real-time visibility into the agent-task-queue system. Shows running/waiting tasks in a status bar widget and tool window, with streaming output, notifications, and task management.

## Features

### Status Bar Widget

Displays current queue state in the IDE status bar with four configurable display modes:

| Mode | Shows |
|------|-------|
| **Hidden** | Nothing — widget invisible |
| **Minimal** | Icon only |
| **Default** | `Task Queue: ./gradlew build (+2)` |
| **Verbose** | `Task Queue: ./gradlew build [2m 13s] (+2 waiting)` |

Click the widget to open the tool window. Configure the display mode in **Settings > Tools > Agent Task Queue**.

### Tool Window

- **Queue** tab — Table of all tasks with ID, status, queue name, command, and relative time. Toolbar actions for refresh, cancel, clear, view output, and settings. Click a running task row to open its output tab.
- **Output** tabs — Per-task closeable tabs with live streaming console output. Automatically opened when a task starts running. Tabs can be closed and reopened by clicking the running task in the queue table.

### Notifications

Balloon notifications for queue events (can be disabled in settings):

| Event | Type | Content |
|-------|------|---------|
| Task starts running | Info balloon | "Running: `./gradlew build`" |
| Task finishes (exit 0) | Info balloon | "Finished: `./gradlew build`" |
| Task fails (exit != 0) | Error balloon (sticky) | "Failed: `./gradlew build`" + View Output action |

Failure detection works by reading the `EXIT CODE` from the formatted task log (`task_<id>.log`) after it disappears from the queue.

## Architecture

### How It Reads Data

The plugin reads the SQLite database directly (read-only via JDBC with WAL mode) rather than going through the MCP server. This avoids coupling to the MCP protocol and lets the plugin work even when no MCP server is running.

```
TaskQueuePoller (1-3s interval)
    └── TaskQueueDatabase.fetchAllTasks()
            └── SELECT * FROM queue ORDER BY queue_name, id
                    └── jdbc:sqlite:/tmp/agent-task-queue/queue.db
```

### Polling Strategy

Two independent polling loops, each active only when needed:

**Database poller** (`TaskQueuePoller`) — Polls the SQLite queue database:
- 1s interval when tasks exist (active)
- 3s interval when queue is empty (idle)
- Supports manual refresh via a conflated coroutine channel
- Detects stale tasks by checking if the server PID is still alive (`kill -0`), and removes them from the DB

**Output file tailer** (`OutputStreamer`) — Tails the running task's output file:
- Only active while a task is running (no coroutine exists otherwise)
- 50ms interval when new data was just read (active streaming)
- 200ms interval when no new data (waiting for output)
- Uses `RandomAccessFile` with byte offset tracking to read only new content
- Prefers `task_<id>.raw.log` (MCP server v0.4.0+) for clean output with no filtering
- Falls back to `task_<id>.log` with header skipping and marker filtering for MCP server v0.3.x and earlier

We chose polling over `java.nio.file.WatchService` because WatchService on macOS falls back to internal polling at 2-10s intervals (no native kqueue support for file modifications in Java), which would actually be slower.

### Data Flow

```
TaskQueuePoller ──poll()──> TaskQueueDatabase ──SQL──> SQLite DB
       │
       └── TaskQueueModel.update(tasks)
                │
                └── messageBus.syncPublisher(TOPIC)
                        │
                        ├── TaskQueueStatusBarWidget.updateLabel()
                        ├── TaskQueuePanel (table + summary)
                        ├── OutputPanel ──start/stopTailing──> OutputStreamer
                        └── TaskQueueNotifier (balloon notifications)
```

All UI components subscribe to `TaskQueueModel.TOPIC` on the IntelliJ message bus and react to changes. The model publishes updates on the EDT via `invokeLater`.

### Process Cancellation

Task cancellation sends SIGTERM to the process group (negative PID), waits 500ms, then sends SIGKILL if still alive. The Python task runner uses `start_new_session=True` when spawning subprocesses, which creates a dedicated process group — this ensures `kill -TERM -<pgid>` cleanly terminates the entire process tree.

The UI is updated optimistically — the task is removed from the model immediately so the table responds instantly, before the background process kill and DB cleanup complete. The poller reconciles with the DB on subsequent polls.

## Database Schema

The plugin reads from the `queue` table:

| Column | Type | Description |
|--------|------|-------------|
| `id` | INTEGER | Auto-incrementing primary key |
| `queue_name` | TEXT | Queue identifier (e.g., "global") |
| `status` | TEXT | "waiting" or "running" |
| `command` | TEXT | Shell command being executed |
| `pid` | INTEGER | MCP server process ID |
| `child_pid` | INTEGER | Subprocess group ID (used for cancellation) |
| `created_at` | TIMESTAMP | When task was queued |
| `updated_at` | TIMESTAMP | Last status change |

Output logs are at `<data_dir>/output/task_<id>.log` (formatted) and `<data_dir>/output/task_<id>.raw.log` (raw output, MCP server v0.4.0+).

## Building

```bash
cd intellij-plugin
./gradlew buildPlugin
```

The built plugin ZIP is at `build/distributions/`.

### Requirements

- JDK 21+
- IntelliJ IDEA 2024.2+ (build 242-252.*)

### Dependencies

- `org.xerial:sqlite-jdbc:3.47.2.0` — SQLite JDBC driver
- Kotlin coroutines — bundled with IntelliJ Platform (do NOT add as a dependency)

## Settings

Persisted in `AgentTaskQueueSettings.xml`:

| Setting | Default | Description |
|---------|---------|-------------|
| `dataDir` | `$TASK_QUEUE_DATA_DIR` or `/tmp/agent-task-queue` | Path to agent-task-queue data directory |
| `displayMode` | `default` | Status bar display: `hidden`, `minimal`, `default`, `verbose` |
| `notificationsEnabled` | `true` | Show balloon notifications for queue events |

## Project Structure

```
src/main/kotlin/com/block/agenttaskqueue/
├── TaskQueueIcons.kt              # Icon loading
├── actions/
│   ├── CancelTaskAction.kt        # Cancel selected task
│   ├── ClearQueueAction.kt        # Clear all tasks
│   ├── OpenOutputLogAction.kt     # Open log file in editor
│   ├── OpenSettingsAction.kt      # Open settings page
│   ├── RefreshQueueAction.kt      # Manual refresh
│   └── TaskQueueDataKeys.kt       # DataKey for selected task
├── data/
│   ├── OutputStreamer.kt           # Coroutine file tailer
│   ├── TaskCanceller.kt           # Process group termination
│   ├── TaskQueueDatabase.kt       # SQLite JDBC access
│   ├── TaskQueueNotifier.kt       # Balloon notifications
│   └── TaskQueuePoller.kt         # Background DB polling
├── model/
│   ├── QueueSummary.kt            # Aggregate counts
│   ├── QueueTask.kt               # Task data class
│   └── TaskQueueModel.kt          # Shared state + message bus topic
├── settings/
│   ├── TaskQueueConfigurable.kt   # Settings UI
│   └── TaskQueueSettings.kt       # Persistent state
└── ui/
    ├── OutputPanel.kt             # Live console output tab
    ├── TaskQueuePanel.kt          # Queue table tab
    ├── TaskQueueStatusBarWidget.kt
    ├── TaskQueueStatusBarWidgetFactory.kt
    ├── TaskQueueTableModel.kt     # Table data model
    └── TaskQueueToolWindowFactory.kt
```
