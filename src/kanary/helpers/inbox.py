"""Inbox: values that arrive on other threads, waiting for poll()."""

from collections import deque
from datetime import datetime, timezone
import threading
from typing import Any

from ..models import InputValue, PollResult, normalize_input_items
from ..source import skip
from ..values import coerce_datetime
from .buffer import in_test_poll


class Inbox:
    """Values that arrive by themselves, held until poll() takes them.

    A source that is told about values - a webhook, an MQTT message, a callback
    of a device library - creates an Inbox in init(), lets its listener put
    what arrives, and waits for it in poll() (with interval = 0)::

        def init(self):
            self.inbox = kanary.Inbox()
            ...  # start the listener; on its own thread it calls
                 # self.inbox.put(("temperature", 21.5)), the same items as kanary.inputs()

        def poll(self):
            return self.inbox.wait(timeout=1.0)

    put() never blocks and works from any thread. A sample without a time of
    its own is timestamped when it is put, not when poll() takes it. At most
    ``size`` samples wait; beyond that the oldest is dropped and counted in
    ``dropped``. kanaryctl test-poll shows what wait() would return without
    waiting or taking anything.
    """

    def __init__(self, *, size: int = 10_000) -> None:
        if not isinstance(size, int) or isinstance(size, bool) or size < 1:
            raise ValueError(f"Inbox size must be a positive integer, not {size!r}")
        self.size = size
        self.dropped = 0
        self._condition = threading.Condition()
        self._arrived: deque[InputValue] = deque()
        self._latest: dict[str, InputValue] = {}

    def put(self, *items: Any, timestamp: Any = None) -> None:
        """Add samples, written the same way as for kanary.inputs()::

            inbox.put(("temperature", 21.5))
            inbox.put(("temperature", 21.5), ("humidity", 40.0))
            inbox.put(("temperature", 21.5, observed_at))   # with its own time
            inbox.put({"temperature": 21.5, "humidity": 40.0})

        ``timestamp=`` gives a time to the samples without one (default: now).
        """
        when = coerce_datetime(timestamp) if timestamp is not None else datetime.now(timezone.utc)
        samples = normalize_input_items(items, default_timestamp=when)
        with self._condition:
            for sample in samples:
                self._arrived.append(sample)
                if len(self._arrived) > self.size:
                    self._arrived.popleft()
                    self.dropped += 1
            self._condition.notify_all()

    def wait(self, timeout: float = 1.0) -> PollResult:
        """For poll(): wait up to ``timeout`` seconds until something arrives.

        Then return every input with its newest value: inputs that did not
        arrive this time keep their last value, so they do not count as missing.
        A burst is evaluated once, with the newest values. When nothing arrived,
        return kanary.skip(): no new snapshot, rules are not evaluated.
        """
        if in_test_poll():
            latest = self._merged(self.take(0))
            return PollResult(inputs=list(latest.values()), metadata=self._metadata(0)) if latest else skip(reason="nothing arrived yet")
        arrived = self.take(timeout)
        if not arrived:
            return skip(reason="nothing arrived")
        with self._condition:
            latest_values = list(self._latest.values())
        return PollResult(inputs=latest_values, metadata=self._metadata(len(arrived)))

    def take(self, timeout: float = 0) -> list[InputValue]:
        """Every sample that arrived since the last take(), oldest first.

        Waits up to ``timeout`` seconds for the first one. For a poll() that
        must see every value of a burst (a short spike), for example into a
        kanary.Buffer: ``self.buffer.add(*self.inbox.take(timeout=1.0))``.
        Inside a test poll it returns them without taking or waiting.
        """
        with self._condition:
            if in_test_poll():
                return list(self._arrived)
            if not self._arrived and timeout > 0:
                self._condition.wait_for(lambda: bool(self._arrived), timeout)
            samples = list(self._arrived)
            self._arrived.clear()
            for sample in samples:
                self._latest[sample.name] = sample
            return samples

    def latest(self) -> dict[str, InputValue]:
        """{name: newest sample} of everything taken so far."""
        with self._condition:
            return dict(self._latest)

    def __len__(self) -> int:
        """Samples waiting to be taken."""
        with self._condition:
            return len(self._arrived)

    def _merged(self, pending: list[InputValue]) -> dict[str, InputValue]:
        with self._condition:
            latest = dict(self._latest)
        for sample in pending:
            latest[sample.name] = sample
        return latest

    def _metadata(self, arrived: int) -> dict[str, Any]:
        return {"arrived": arrived, "dropped": self.dropped}
