# Setup Guide: Claude Code

This guide configures Claude Code to use the task queue for expensive operations.

## 1. Install MCP Server

```bash
claude mcp add agent-task-queue -- uvx agent-task-queue@latest
```

## 2. Update settings files

Check **both** `settings.json` and `settings.local.json` (global `~/.claude/` and project `.claude/`).

Add permission to read task logs:

```json
{
  "permissions": {
    "allow": [
      "Read(/tmp/agent-task-queue/*)"
    ]
  }
}
```

**Remove these if present** (they bypass the queue):
- `Bash(gradle:*)`, `Bash(gradlew:*)`, `Bash(./gradlew:*)`
- `Bash(ANDROID_SERIAL=* ./gradlew:*)` or similar prefixed commands
- `Bash(docker build:*)`, `Bash(docker-compose:*)`
- `Bash(npm run build:*)`, `Bash(pytest:*)`, `Bash(jest:*)`
- `Bash(mvn:*)`, `Bash(bazel:*)`, `Bash(make:*)`

## 3. Update ~/.claude/CLAUDE.md

Add instructions to use the queue:

```markdown
## Build Queue

For expensive operations, ALWAYS use the `run_task` MCP tool instead of Bash.

**Commands that MUST use run_task:**
- gradle, bazel, make, cmake, mvn, cargo build, go build
- docker build, docker-compose, kubectl, helm
- npm/yarn/pnpm build, pytest, jest, mocha

**Usage:**
- command: The full shell command
- working_directory: Absolute path to project root
- env_vars: Optional like "KEY=value,KEY2=value2"

NEVER run these via Bash. Always use run_task MCP tool.
```

## Done

Restart Claude Code for changes to take effect.
