"""Run external commands: CommandSource, SSHCommandSource, and CommandOutput."""

import json
import os
import shlex
import subprocess
from typing import Any

from ..models import AlertEvent, PollResult
from ..output import Output, event_record
from ..source import Source, inputs, no_data
from ..values import parse_values


_OUTPUT_FORMATS = ("auto", "number", "keyvalue", "json")


class CommandSource(Source):
    """Run a command on every poll and turn its stdout into numeric inputs.

    stdout is parsed with ``output_format`` (see ``kanary.parse_values``):
    one number, ``name=value`` pairs, or JSON. The exit code is reported as
    the ``exit_code`` input so a rule can alert on it. A timeout raises, which
    counts as a poll failure (retry, then reinit).
    """

    command: list[str] | str = []
    timeout: float = 10.0
    output_format: str = "auto"
    value_name: str = "value"
    env: dict[str, str] = {}
    cwd: str | None = None
    report_exit_code: bool = True

    def args(self) -> list[str]:
        """The argv to run. A string command is split like a shell would, without a shell."""
        if not self.command:
            raise ValueError(f"{type(self).__name__}.command is empty")
        if isinstance(self.command, str):
            return shlex.split(self.command)
        return [str(part) for part in self.command]

    def describe(self) -> dict[str, Any]:
        """Result metadata about the command. Only the program name: arguments may
        carry tokens, and metadata is stored and shown in the viewer."""
        args = self.args()
        return {"program": os.path.basename(args[0]) if args else None}

    def run(self) -> subprocess.CompletedProcess[str]:
        return subprocess.run(
            self.args(),
            capture_output=True,
            text=True,
            timeout=self.timeout,
            env={**os.environ, **self.env} if self.env else None,
            cwd=self.cwd,
        )

    def parse(self, stdout: str) -> tuple[dict[str, float], dict[str, str]]:
        """Override to read a custom output format; return (numbers, texts)."""
        if not stdout.strip():
            return {}, {}
        return parse_values(stdout, self.output_format, default_name=self.value_name)

    def poll(self) -> PollResult:
        if self.output_format not in _OUTPUT_FORMATS:
            raise ValueError(f"output_format must be one of {', '.join(_OUTPUT_FORMATS)}, not {self.output_format!r}")
        completed = self.run()
        try:
            numbers, texts = self.parse(completed.stdout)
        except ValueError:
            if completed.returncode == 0:
                raise
            numbers, texts = {}, {}  # a failing command often prints an error instead

        items: list[tuple[str, Any]] = list(numbers.items())
        if self.report_exit_code:
            items.append(("exit_code", completed.returncode))
        metadata = self.describe()
        if texts:
            metadata["text"] = texts
        if completed.returncode != 0 and completed.stderr.strip():
            metadata["stderr"] = completed.stderr.strip()[-500:]
        if not items:
            return no_data(reason="command printed no values", metadata=metadata)
        return inputs(items, metadata=metadata)


_EXIT_MARKER = "__kanary_exit="


class SSHCommandSource(CommandSource):
    """Run ``command`` on a remote host through ``ssh`` (key-based, non-interactive).

    The remote command runs in a subshell and its exit code is printed after it
    as a marker line, so a failure of ssh itself (unreachable host, auth; no
    marker) is raised as a poll failure, while the command's own exit code,
    including 255, is reported as ``exit_code``. This needs a POSIX-like login
    shell on the remote host; set wrap_exit_code = False for other shells, and
    then exit code 255 is always treated as an ssh failure.
    """

    host: str | None = None
    user: str | None = None
    port: int | None = None
    identity_file: str | None = None
    connect_timeout: float = 5.0
    ssh_options: list[str] = []
    wrap_exit_code: bool = True

    def remote_command(self) -> str:
        if not self.command:
            raise ValueError(f"{type(self).__name__}.command is empty")
        remote = self.command if isinstance(self.command, str) else shlex.join(self.command)
        if not self.wrap_exit_code:
            return remote
        # The newline before ")" keeps a trailing comment in the command harmless.
        return f"( {remote}\n); printf '\\n{_EXIT_MARKER}%s\\n' \"$?\""

    def describe(self) -> dict[str, Any]:
        return {"program": "ssh", "host": self.host}

    def args(self) -> list[str]:
        if not self.host:
            raise ValueError(f"{type(self).__name__}.host is not set")
        remote = self.remote_command()
        argv = ["ssh", "-o", "BatchMode=yes", "-o", f"ConnectTimeout={max(1, int(self.connect_timeout))}"]
        if self.port is not None:
            argv += ["-p", str(self.port)]
        if self.identity_file:
            argv += ["-i", os.path.expanduser(self.identity_file)]
        argv += list(self.ssh_options)
        argv += [f"{self.user}@{self.host}" if self.user else str(self.host), remote]
        return argv

    def run(self) -> subprocess.CompletedProcess[str]:
        completed = super().run()
        if not self.wrap_exit_code:
            if completed.returncode == 255:
                raise RuntimeError(f"ssh to {self.host} failed: {completed.stderr.strip()[-300:]}")
            return completed
        body, marker, tail = completed.stdout.rpartition("\n" + _EXIT_MARKER)
        if not marker:
            if completed.returncode == 255:
                raise RuntimeError(f"ssh to {self.host} failed: {completed.stderr.strip()[-300:]}")
            return completed  # e.g. a non-POSIX remote shell; keep ssh's own code
        code = tail.strip()
        completed.stdout = body
        completed.returncode = int(code) if code.lstrip("-").isdigit() else completed.returncode
        return completed


class CommandOutput(Output):
    """Run a command for each event: the event as JSON on stdin, key fields in env.

    Environment: KANARY_RULE_ID, KANARY_STATE, KANARY_PREVIOUS_STATE,
    KANARY_TRANSITION, KANARY_SEVERITY, KANARY_MESSAGE, KANARY_OWNER, KANARY_TAGS.
    A non-zero exit raises, so the engine retries according to max_retry.
    """

    command: list[str] | str = []
    timeout: float = 10.0
    env: dict[str, str] = {}

    def args(self, event: AlertEvent) -> list[str]:
        if not self.command:
            raise ValueError(f"{type(self).__name__}.command is empty")
        if isinstance(self.command, str):
            return shlex.split(self.command)
        return [str(part) for part in self.command]

    def record(self, event: AlertEvent) -> dict[str, Any]:
        """The JSON written to stdin; override to reshape it."""
        return event_record(event)

    def emit(self, event: AlertEvent) -> None:
        record = self.record(event)
        env = {
            **os.environ,
            **self.env,
            "KANARY_RULE_ID": event.rule_id,
            "KANARY_STATE": event.current_state.value,
            "KANARY_PREVIOUS_STATE": event.previous_state.value if event.previous_state else "",
            "KANARY_TRANSITION": event.transition.value if event.transition else "",
            "KANARY_SEVERITY": str(record["severity"]),
            "KANARY_MESSAGE": event.message or "",
            "KANARY_OWNER": event.owner or "",
            "KANARY_TAGS": ",".join(event.tags),
        }
        # A finite timeout is essential: this output's queue waits while it hangs.
        completed = subprocess.run(
            self.args(event),
            input=json.dumps(record, ensure_ascii=False, default=str),
            capture_output=True,
            text=True,
            timeout=self.timeout,
            env=env,
        )
        if completed.returncode != 0:
            raise RuntimeError(
                f"command exited with {completed.returncode}: {completed.stderr.strip()[-300:]}"
            )
