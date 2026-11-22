"""File helpers: FileSource, LogCountSource, and FileOutput (JSON Lines)."""

from collections.abc import Mapping
from datetime import datetime, timezone
import glob
import json
import os
from pathlib import Path
import re
from typing import Any

from ..models import AlertEvent, PollResult
from ..output import Output, event_record
from ..plugin_files import path_beside_plugin
from ..source import Source, inputs
from ..values import to_number


class FileSource(Source):
    """Watch files and file patterns.

    For a single path:
      <name>.exists        1 or 0
      <name>.age_seconds   seconds since the last modification
      <name>.size_bytes
      <name>.value         the last line as a number (only with read_last_line)

    For a glob pattern ("/data/run_*.root", "logs/**/*.txt") the same inputs
    describe the newest matching file, plus <name>.count (matching files) and
    <name>.total_size_bytes. exists is 1 when anything matches.

    paths is a dict of input name to path or pattern, or a list of them; a
    list entry is named after its file name ("daily.tar.gz") or, for a
    pattern, its fixed part ("*.root" -> "root"). Relative paths are resolved
    next to the plugin file.
    """

    paths: dict[str, str] | list[str] = {}
    read_last_line: bool = False
    encoding: str = "utf-8"

    def poll(self) -> PollResult:
        named = self.named_paths()
        now = datetime.now(timezone.utc)
        items: list[tuple[str, Any, datetime | None, dict[str, Any]]] = []
        for name, raw in named.items():
            path = path_beside_plugin(self, raw)
            if _is_pattern(raw):
                matches = [Path(match) for match in glob.glob(str(path), recursive=True)]
                matches = [match for match in matches if match.is_file()]
                meta = {"pattern": str(path)}
                items.append((f"{name}.count", len(matches), None, meta))
                items.append((f"{name}.total_size_bytes", sum(m.stat().st_size for m in matches), None, meta))
                if not matches:
                    items.append((f"{name}.exists", 0, None, meta))
                    continue
                path = max(matches, key=lambda match: match.stat().st_mtime)
            items.extend(self.describe_file(name, path, now))
        return inputs(items)

    def named_paths(self) -> dict[str, str]:
        if isinstance(self.paths, Mapping):
            named = {str(name): str(path) for name, path in self.paths.items()}
        elif isinstance(self.paths, (list, tuple, set, frozenset)):
            named = {}
            for raw in sorted(str(path) for path in self.paths):
                name = _name_for(raw)
                if name in named:
                    raise ValueError(f"FileSource.paths: {raw!r} and {named[name]!r} both get the name {name!r}; use a dict")
                named[name] = raw
        else:
            raise ValueError("FileSource.paths must be a dict of name -> path or a list of paths")
        if not named:
            raise ValueError("FileSource.paths is empty; list file paths or glob patterns")
        return named

    def describe_file(self, name: str, path: Path, now: datetime) -> list[tuple[str, Any, Any, dict[str, Any]]]:
        meta = {"path": str(path)}
        try:
            stat = path.stat()
        except FileNotFoundError:
            return [(f"{name}.exists", 0, None, meta)]
        modified = datetime.fromtimestamp(stat.st_mtime, tz=timezone.utc)
        items = [
            (f"{name}.exists", 1, None, meta),
            (f"{name}.age_seconds", (now - modified).total_seconds(), None, meta),
            (f"{name}.size_bytes", stat.st_size, None, meta),
        ]
        if self.read_last_line:
            line = last_line(path, encoding=self.encoding)
            number = to_number(line) if line is not None else None
            if number is not None:
                items.append((f"{name}.value", number, modified, {**meta, "line": line}))
        return items


def _is_pattern(path: str) -> bool:
    return any(char in path for char in "*?[")


def _name_for(path: str) -> str:
    """An input name for a listed path: its file name, or the fixed part of a pattern."""
    base = Path(path).name
    if _is_pattern(base):
        base = re.sub(r"[._-]*(?:[*?]|\[[^\]]*\])+[._-]*", ".", base).strip(".") or Path(path).parent.name
    if not base or _is_pattern(base):
        raise ValueError(f"FileSource.paths: cannot name {path!r}; use a dict of name -> path")
    return base


def last_line(path: Path, *, encoding: str = "utf-8", block_size: int = 4096) -> str | None:
    """The last non-empty line of a file, reading only its end."""
    with path.open("rb") as handle:
        handle.seek(0, os.SEEK_END)
        position = handle.tell()
        data = b""
        while position > 0:
            step = min(block_size, position)
            position -= step
            handle.seek(position)
            data = handle.read(step) + data
            lines = [line for line in data.splitlines() if line.strip()]
            if len(lines) > 1 or (lines and position == 0):
                return lines[-1].decode(encoding, errors="replace").strip()
    return None


# Bytes before the read position remembered to detect a truncated-and-regrown file.
_TAIL_BYTES = 256


class LogCountSource(Source):
    """Count new log lines matching each pattern since the previous poll.

    patterns maps an input name to a regular expression. Each poll reports the
    number of matching lines written since the last poll, so ``interval`` sets
    the counting window. Rotation restarts from the beginning: a new file, a
    truncated file, or a truncated file that has already grown past the old
    position (detected by comparing the last bytes read). With start_at_end, lines written before startup are ignored.
    An init() override must set path / patterns first and then call super().init().
    """

    path: str | None = None
    patterns: dict[str, str] = {}
    start_at_end: bool = True
    encoding: str = "utf-8"
    max_bytes_per_poll: int = 10_000_000

    def init(self) -> None:
        if not self.path:
            raise ValueError("LogCountSource.path is not set")
        if not self.patterns:
            raise ValueError("LogCountSource.patterns is empty; map input names to regular expressions")
        self._compiled = {name: re.compile(pattern) for name, pattern in self.patterns.items()}
        self._file = path_beside_plugin(self, self.path)
        self._identity: tuple[int, int] | None = None
        self._offset = 0
        self._partial = b""
        self._tail = b""
        if self.start_at_end and self._file.exists():
            stat = self._file.stat()
            self._identity, self._offset = (stat.st_dev, stat.st_ino), stat.st_size
            with self._file.open("rb") as handle:
                handle.seek(max(0, self._offset - _TAIL_BYTES))
                self._tail = handle.read(_TAIL_BYTES)

    def poll(self) -> PollResult:
        counts = {name: 0 for name in self._compiled}
        last_match: dict[str, str] = {}
        for line in self._new_lines():
            for name, pattern in self._compiled.items():
                if pattern.search(line):
                    counts[name] += 1
                    last_match[name] = line[-300:]
        return inputs(
            [(name, count, None, {"last_line": last_match[name]} if name in last_match else {})
             for name, count in counts.items()],
            metadata={"path": str(self._file)},
        )

    def test_poll(self) -> PollResult:
        """The counts poll() would report now, keeping the read position for the next poll."""
        saved = (self._identity, self._offset, self._partial, self._tail)
        try:
            return self.poll()
        finally:
            self._identity, self._offset, self._partial, self._tail = saved

    def _new_lines(self) -> list[str]:
        try:
            stat = self._file.stat()
        except FileNotFoundError:
            return []  # not written yet, or between rotation steps
        identity = (stat.st_dev, stat.st_ino)
        with self._file.open("rb") as handle:
            if identity != self._identity or stat.st_size < self._offset or not self._same_tail(handle):
                # A new file, or truncated (logrotate copytruncate) even if it has grown again.
                self._identity, self._offset, self._partial, self._tail = identity, 0, b"", b""
            if stat.st_size == self._offset:
                return []
            handle.seek(self._offset)
            data = handle.read(self.max_bytes_per_poll)
        self._offset += len(data)
        self._tail = (self._tail + data)[-_TAIL_BYTES:]
        data = self._partial + data
        *complete, self._partial = data.split(b"\n")  # keep an unfinished last line for next time
        return [line.decode(self.encoding, errors="replace") for line in complete]

    def _same_tail(self, handle: Any) -> bool:
        """Do the bytes just before the read position still match what was read there?"""
        if not self._tail:
            return True
        handle.seek(self._offset - len(self._tail))
        return handle.read(len(self._tail)) == self._tail


class FileOutput(Output):
    """Append each event as one JSON line. A relative path is resolved next to the plugin file.

    Rotate the file with logrotate (copytruncate) or similar. An init() override
    must set path first and then call super().init().
    """

    path: str | None = None
    include_payload: bool = False  # also write the rule's payload (values, extra) as "payload"

    def init(self) -> None:
        if not self.path:
            raise ValueError("FileOutput.path is not set")
        self._file = path_beside_plugin(self, self.path)
        self._file.parent.mkdir(parents=True, exist_ok=True)

    def record(self, event: AlertEvent) -> dict[str, Any]:
        """The JSON object written per event; override to reshape it.

        kanary.event_record(event) is the default object, for example
        ``return {**kanary.event_record(event), "host": "daq01"}``.
        """
        record = event_record(event)
        if self.include_payload:
            record["payload"] = event.payload
        return record

    def emit(self, event: AlertEvent) -> None:
        line = json.dumps(self.record(event), ensure_ascii=False, default=str)
        with self._file.open("a", encoding="utf-8") as handle:
            handle.write(line + "\n")
