# Agent Task Queue Desktop Sidecar

Minimal Compose Multiplatform desktop app for watching the local `agent-task-queue` database in real time.

The sidecar reads the existing SQLite queue DB directly and shows:

- running tasks
- waiting tasks
- exact queues grouped by root scope so hierarchical queue activity is easier to understand

It is intentionally read-only. There is no new MCP protocol or server surface.

## Run

```bash
./gradlew run
```

By default the app reads `$TASK_QUEUE_DATA_DIR` or `/tmp/agent-task-queue`.

Use a specific queue directory with:

```bash
./gradlew run --args="--data-dir /path/to/agent-task-queue"
```

## Package

```bash
./gradlew packageDistributionForCurrentOS
```

## Notes

- `./gradlew` in this directory delegates to the checked-in Gradle wrapper under `../intellij-plugin/` so the sidecar stays lightweight.
- Queue capacities configured with `--queue-capacity` are process-local and are not persisted in `queue.db`, so the app visualizes live tasks and queue layout rather than stored capacity numbers.
