# Agent Setup Guides

Some AI coding agents need configuration to prefer MCP tools over built-in shell commands. This directory contains agent-specific setup guides.

## Available Guides

### [Claude Code](claude-code/)
- **SETUP.md** - 3-step configuration guide
- **CLAUDE.md** - Example config snippet

**Why needed:** Claude Code defaults to built-in Bash tool. Configuration ensures it uses `run_task` for expensive operations.

## Agent Compatibility

| Agent | MCP Support | Extra Config Needed? | Status |
|-------|-------------|---------------------|---------|
| Amp | ✅ Yes | ❌ No | Works out of the box |
| Claude Code | ✅ Yes | ✅ Yes | [Setup guide available](claude-code/) |
| Cline | ✅ Yes | ⚠️ Maybe | Needs testing |
| Copilot (VS Code) | ✅ Yes | ❌ No | Works out of the box |
| Cursor | ✅ Yes | ✅ Yes | Setup guide needed |
| Firebender | ✅ Yes | ⚠️ Maybe | Needs testing |
| Windsurf | ✅ Yes | ❌ No | Works out of the box |

## Contributing

To add a setup guide for another agent:

1. **Create directory:** `mkdir examples/<agent-name>`
2. **Add SETUP.md** with clear, agent-readable instructions (keep it under 60 lines)
3. **Include example configs** if needed
4. **Update this README** with agent compatibility info

**Goal:** Agents should be able to follow SETUP.md automatically without human intervention.
