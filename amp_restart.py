"""Amp-specific session discovery helpers for the tq CLI."""

from __future__ import annotations

import json
import re
import shlex
import subprocess
import sys
from collections.abc import Iterable
from dataclasses import dataclass
from pathlib import Path

AMP_CLI_LOG_PATH = Path.home() / ".cache" / "amp" / "logs" / "cli.log"
AMP_THREAD_ID_PATTERN = re.compile(r"T-[0-9a-f-]{36}")
AMP_ENV_ASSIGNMENT_PATTERN = re.compile(r"^[A-Za-z_][A-Za-z0-9_]*=")
AMP_ENV_VALUE_PATTERN_TEMPLATE = r"(?:^|\s){name}=(.*?)(?=\s+[A-Za-z_][A-Za-z0-9_]*=|$)"
AMP_PS_COMMAND_CANDIDATES = [
    ["ps", "eww", "-axo", "pid=,command="],
    ["ps", "eww", "axo", "pid=,command="],
]

# Mirrors the current `amp --help` global options so the generic queue CLI does not own
# Amp-specific argument parsing details.
AMP_GLOBAL_FLAGS_WITH_VALUE = {
    "--visibility",
    "--settings-file",
    "--log-level",
    "--log-file",
    "--mcp-config",
    "-l",
    "--label",
}
AMP_GLOBAL_BOOLEAN_FLAGS = {
    "--notifications",
    "--no-notifications",
    "--color",
    "--no-color",
    "--dangerously-allow-all",
    "--jetbrains",
    "--no-jetbrains",
    "--ide",
    "--no-ide",
    "--stream-json",
    "--stream-json-thinking",
    "--stream-json-input",
    "--archive",
}


@dataclass
class AmpSession:
    pid: int
    cwd: str | None
    thread_id: str | None = None
    agent_session_id: str | None = None
    mode: str | None = None

    @property
    def stop_command(self) -> str:
        return f"kill -TERM {self.pid}"

    @property
    def continue_command(self) -> str | None:
        if not self.cwd or not self.thread_id:
            return None
        return f"(cd {shlex.quote(self.cwd)} && amp threads continue {self.thread_id})"


def add_amp_restart_subparser(subparsers) -> None:
    parser = subparsers.add_parser(
        "amp-restart",
        help="Resolve live interactive Amp sessions to thread IDs and print restart commands",
    )
    parser.add_argument(
        "--pid",
        action="append",
        type=int,
        default=[],
        help="Target a specific live Amp PID. Repeatable. Defaults to all live interactive Amp sessions.",
    )
    parser.add_argument("--json", action="store_true", help="Output in JSON format")
    parser.add_argument(
        "--shell",
        action="store_true",
        help="Print shell commands only (kill + amp threads continue)",
    )


def _extract_env_value(process_line: str, env_name: str) -> str | None:
    pattern = AMP_ENV_VALUE_PATTERN_TEMPLATE.format(name=re.escape(env_name))
    match = re.search(pattern, process_line)
    if not match:
        return None
    value = match.group(1).strip()
    return value or None


def _amp_process_prefix_tokens(process_line: str) -> tuple[int, list[str]] | None:
    line = process_line.strip()
    if not line:
        return None

    try:
        pid_text, command = line.split(None, 1)
        pid = int(pid_text)
    except ValueError:
        return None

    argv = []
    for token in command.split():
        if AMP_ENV_ASSIGNMENT_PATTERN.match(token):
            break
        argv.append(token)

    if not argv:
        return None

    return pid, argv


def _is_interactive_amp_invocation(argv: list[str]) -> tuple[bool, str | None]:
    if not argv or Path(argv[0]).name != "amp":
        return False, None

    remaining: list[str] = []
    mode: str | None = None
    i = 1
    while i < len(argv):
        token = argv[i]
        if token in {"-x", "--execute"} or token.startswith("--execute="):
            return False, mode
        if token in {"-m", "--mode"}:
            if i + 1 < len(argv):
                mode = argv[i + 1]
            i += 2
            continue
        if token.startswith("--mode="):
            mode = token.split("=", 1)[1] or None
            i += 1
            continue
        if token in AMP_GLOBAL_FLAGS_WITH_VALUE:
            i += 2
            continue
        if any(token.startswith(flag + "=") for flag in AMP_GLOBAL_FLAGS_WITH_VALUE if flag.startswith("--")):
            i += 1
            continue
        if token in AMP_GLOBAL_BOOLEAN_FLAGS:
            i += 1
            continue
        remaining = argv[i:]
        break

    interactive = not remaining or (
        len(remaining) >= 2
        and remaining[0] in {"threads", "thread", "t"}
        and remaining[1] in {"continue", "c", "new", "n"}
    )
    return interactive, mode


def parse_amp_sessions_from_ps_output(ps_output: str) -> list[AmpSession]:
    """Parse `ps eww` output and return live interactive Amp sessions."""
    sessions: list[AmpSession] = []
    for line in ps_output.splitlines():
        prefix = _amp_process_prefix_tokens(line)
        if prefix is None:
            continue

        pid, argv = prefix
        interactive, mode = _is_interactive_amp_invocation(argv)
        if not interactive:
            continue

        sessions.append(
            AmpSession(
                pid=pid,
                cwd=_extract_env_value(line, "PWD"),
                agent_session_id=_extract_env_value(line, "AGENT_SESSION_ID"),
                mode=mode,
            )
        )

    return sessions


def _extract_thread_id_from_log_entry(entry: dict, *, allow_session_state: bool = False) -> str | None:
    for key in ("threadId", "threadID", "newThreadID", "currentThreadID"):
        value = entry.get(key)
        if isinstance(value, str) and AMP_THREAD_ID_PATTERN.fullmatch(value):
            return value

    if allow_session_state:
        value = entry.get("lastThreadId")
        if isinstance(value, str) and AMP_THREAD_ID_PATTERN.fullmatch(value):
            return value

    message = entry.get("message")
    if isinstance(message, str) and "Switching to thread:" in message:
        match = AMP_THREAD_ID_PATTERN.search(message)
        if match:
            return match.group(0)

    return None


def parse_amp_thread_ids_from_log(
    log_lines: Iterable[str] | str,
    candidate_pids: set[int] | None = None,
) -> dict[int, str]:
    """Return the latest known Amp thread ID for each live PID in the CLI log."""
    latest_thread_by_pid: dict[int, tuple[str, str]] = {}
    latest_session_start_by_pid: dict[int, str] = {}
    line_iterator = log_lines.splitlines() if isinstance(log_lines, str) else log_lines

    for raw_line in line_iterator:
        line = raw_line.strip()
        if not line:
            continue

        try:
            entry = json.loads(line)
        except json.JSONDecodeError:
            continue

        try:
            pid = int(entry["pid"])
        except (KeyError, TypeError, ValueError):
            continue

        if candidate_pids is not None and pid not in candidate_pids:
            continue

        timestamp = entry.get("timestamp")
        if not isinstance(timestamp, str) or not timestamp:
            continue

        message = entry.get("message")
        if message == "Loaded session state:":
            latest_session_start_by_pid[pid] = timestamp
            thread_id = _extract_thread_id_from_log_entry(entry, allow_session_state=True)
            if thread_id is None:
                latest_thread_by_pid.pop(pid, None)
            else:
                latest_thread_by_pid[pid] = (timestamp, thread_id)
            continue

        session_started_at = latest_session_start_by_pid.get(pid)
        if session_started_at is not None and timestamp < session_started_at:
            continue

        thread_id = _extract_thread_id_from_log_entry(entry)
        if thread_id is None:
            continue

        current = latest_thread_by_pid.get(pid)
        if current is None or timestamp >= current[0]:
            latest_thread_by_pid[pid] = (timestamp, thread_id)

    return {pid: thread_id for pid, (_, thread_id) in latest_thread_by_pid.items()}


def discover_amp_sessions(cli_log_path: Path = AMP_CLI_LOG_PATH) -> list[AmpSession]:
    """Discover live interactive Amp sessions and resolve their current thread IDs."""
    last_error = "Failed to enumerate running processes with ps"
    result = None
    for command in AMP_PS_COMMAND_CANDIDATES:
        try:
            result = subprocess.run(
                command,
                capture_output=True,
                text=True,
                timeout=5,
            )
        except subprocess.TimeoutExpired:
            last_error = f"`{' '.join(command)}` timed out after 5s"
            continue
        except (FileNotFoundError, PermissionError, OSError) as exc:
            last_error = f"`{' '.join(command)}` failed: {exc}"
            continue

        if result.returncode == 0:
            break
        stderr = result.stderr.strip()
        stdout = result.stdout.strip()
        details = stderr or stdout
        if details:
            last_error = details
    else:
        raise RuntimeError(last_error)

    sessions = parse_amp_sessions_from_ps_output(result.stdout)
    if not sessions or not cli_log_path.exists():
        return sessions

    with cli_log_path.open() as cli_log:
        thread_ids = parse_amp_thread_ids_from_log(
            cli_log,
            candidate_pids={session.pid for session in sessions},
        )
    for session in sessions:
        session.thread_id = thread_ids.get(session.pid)

    return sessions


def _amp_session_payload(session: AmpSession) -> dict[str, str | int | None]:
    return {
        "pid": session.pid,
        "mode": session.mode,
        "agent_session_id": session.agent_session_id,
        "cwd": session.cwd,
        "thread_id": session.thread_id,
        "stop_command": session.stop_command,
        "continue_command": session.continue_command,
    }


def cmd_amp_restart(args) -> int:
    """Resolve live interactive Amp sessions to thread IDs and print restart commands."""
    pid_filter = set(args.pid or [])

    try:
        sessions = discover_amp_sessions()
    except Exception as exc:
        print(f"Error: {exc}", file=sys.stderr)
        return 1

    if pid_filter:
        sessions = [session for session in sessions if session.pid in pid_filter]
        found_pids = {session.pid for session in sessions}
        missing_pids = sorted(pid_filter - found_pids)
        if missing_pids:
            joined = ", ".join(str(pid) for pid in missing_pids)
            print(f"Error: No live interactive Amp session found for PID(s): {joined}", file=sys.stderr)
            return 1

    unresolved = [session for session in sessions if not session.cwd or not session.thread_id]

    if getattr(args, "json", False):
        output = {
            "sessions": [_amp_session_payload(session) for session in sessions],
            "summary": {
                "total": len(sessions),
                "resolved": len(sessions) - len(unresolved),
                "unresolved": len(unresolved),
            },
        }
        print(json.dumps(output))
        return 1 if pid_filter and unresolved else 0

    if getattr(args, "shell", False):
        for index, session in enumerate(sessions):
            if index:
                print()
            if session.continue_command:
                print(f"# PID {session.pid} thread={session.thread_id} cwd={session.cwd}")
                print(session.stop_command)
                print(session.continue_command)
            else:
                reason = "missing thread ID" if not session.thread_id else "missing cwd"
                print(f"# PID {session.pid} unresolved ({reason})", file=sys.stderr)
        return 1 if pid_filter and unresolved else 0

    if not sessions:
        print("No live interactive Amp sessions found")
        return 0

    for session in sessions:
        session_label = session.agent_session_id or "-"
        mode_label = session.mode or "-"
        print(f"PID {session.pid}  session={session_label}  mode={mode_label}")
        print(f"  cwd: {session.cwd or '(unresolved)'}")
        print(f"  thread: {session.thread_id or '(unresolved)'}")
        print(f"  stop: {session.stop_command}")
        print(f"  continue: {session.continue_command or '(unresolved)'}")
        print()

    if unresolved:
        print(
            f"Unresolved sessions: {len(unresolved)} (missing cwd or thread ID in {AMP_CLI_LOG_PATH})",
            file=sys.stderr,
        )

    return 1 if pid_filter and unresolved else 0
