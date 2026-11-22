"""Reachability sources: PingSource, TCPSource, HTTPStatusSource.

Each checks its targets in parallel once per poll. A target that does not
answer gets an input with no value (None) and the error in its metadata, so a
rule with ``missing_fires = True`` alerts on it.
"""

from collections.abc import Callable, Mapping
from concurrent.futures import ThreadPoolExecutor
import math
import re
import socket
import subprocess
import sys
import time
from typing import Any
from urllib.error import HTTPError
from urllib.parse import urlsplit
from urllib.request import Request, urlopen

from ..models import PollResult
from ..source import Source, inputs

_PING_TIME = re.compile(r"time[=<]([0-9.]+)\s*ms")


class PingSource(Source):
    """``<name>.rtt_ms``: the round-trip time of one ICMP echo to each host.

    Uses the system ping command, so no privileges are needed.
    """

    hosts: dict[str, str] = {}
    timeout: float = 5.0
    max_workers: int = 8

    def poll(self) -> PollResult:
        _require(self.hosts, "PingSource.hosts", "input names to host names or addresses")
        return _check_all(self.hosts, self.ping, ["rtt_ms"], self.max_workers)

    def ping(self, host: str) -> dict[str, float]:
        wait = str(max(1, math.ceil(self.timeout)))
        # macOS: -t is the overall timeout in seconds; Linux: -W is the reply wait.
        argv = ["ping", "-c", "1", "-t" if sys.platform == "darwin" else "-W", wait, host]
        started = time.perf_counter()
        completed = subprocess.run(argv, capture_output=True, text=True, timeout=self.timeout + 2)
        if completed.returncode != 0:
            lines = (completed.stdout + completed.stderr).strip().splitlines()
            raise RuntimeError(lines[-1][-200:] if lines else "no reply")  # e.g. "... 100.0% packet loss"
        match = _PING_TIME.search(completed.stdout)
        return {"rtt_ms": float(match.group(1)) if match else (time.perf_counter() - started) * 1000}


class TCPSource(Source):
    """``<name>.connect_ms``: how long a TCP connect to each "host:port" takes."""

    targets: dict[str, str] = {}
    timeout: float = 5.0
    max_workers: int = 8

    def poll(self) -> PollResult:
        _require(self.targets, "TCPSource.targets", "input names to 'host:port'")
        for name, target in self.targets.items():
            _host_port(name, target)
        return _check_all(self.targets, self.connect, ["connect_ms"], self.max_workers)

    def connect(self, target: str) -> dict[str, float]:
        host, port = _host_port("", target)
        started = time.perf_counter()
        with socket.create_connection((host, port), timeout=self.timeout):
            return {"connect_ms": (time.perf_counter() - started) * 1000}


class HTTPStatusSource(Source):
    """``<name>.status`` (the HTTP status code) and ``<name>.response_ms`` for each URL.

    An error status such as 503 is a value, not a failure: check it with a rule.
    Only a connection that gets no HTTP answer at all leaves the values empty.
    """

    urls: dict[str, str] = {}
    headers: dict[str, str] = {}
    timeout: float = 5.0
    max_workers: int = 8

    def poll(self) -> PollResult:
        _require(self.urls, "HTTPStatusSource.urls", "input names to http(s) URLs")
        for name, url in self.urls.items():
            if urlsplit(str(url)).scheme not in {"http", "https"}:
                raise ValueError(f"url {name!r} must start with http:// or https://")
        return _check_all(self.urls, self.request, ["status", "response_ms"], self.max_workers)

    def request(self, url: str) -> dict[str, float]:
        request = Request(url, headers={"User-Agent": "kanary", **self.headers})
        started = time.perf_counter()
        try:
            with urlopen(request, timeout=self.timeout) as response:
                status = response.status
        except HTTPError as exc:  # the server answered, just not with success
            status = exc.code
        return {"status": status, "response_ms": (time.perf_counter() - started) * 1000}


def _check_all(
    targets: Mapping[str, str],
    check: Callable[[str], dict[str, Any]],
    suffixes: list[str],
    max_workers: int,
) -> PollResult:
    """Run check(target) for every target in parallel; a failure gives None values and the error."""

    def safe(target: str) -> tuple[dict[str, Any], dict[str, Any]]:
        try:
            return check(target), {}
        except Exception as exc:  # unreachable is a result here, not a poll failure
            return {}, {"error": f"{type(exc).__name__}: {exc}"}

    names = list(targets)
    with ThreadPoolExecutor(max_workers=max(1, min(max_workers, len(names)))) as pool:
        results = list(pool.map(safe, (str(targets[name]) for name in names)))
    items = []
    for name, (values, error) in zip(names, results):
        for suffix in suffixes:
            value = values.get(suffix)
            if isinstance(value, float):
                value = round(value, 3)
            items.append((f"{name}.{suffix}", value, None, {"target": targets[name], **error}))
    return inputs(items)


def _require(mapping: Any, setting: str, what: str) -> None:
    if not isinstance(mapping, Mapping) or not mapping:
        raise ValueError(f"{setting} is empty; map {what}")


def _host_port(name: str, target: str) -> tuple[str, int]:
    host, _, port = str(target).rpartition(":")
    host = host.strip("[]")  # [::1]:22
    if not host or not port.isdigit():
        raise ValueError(f"target {name!r} must be 'host:port', not {target!r}")
    return host, int(port)
