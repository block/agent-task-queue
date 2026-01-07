## Build Queue

For expensive build commands, ALWAYS use the `run_task` MCP tool instead of Bash.

**Commands that MUST use run_task:**
- gradle, bazel, make, cmake, mvn, cargo build, go build
- docker build, docker-compose, kubectl, helm
- npm/yarn/pnpm build, pytest, jest, mocha

**Usage:**
- command: The full shell command
- working_directory: Absolute path to project root
- env_vars: Optional like "KEY=value,KEY2=value2"

NEVER run these via Bash. Always use run_task MCP tool.
