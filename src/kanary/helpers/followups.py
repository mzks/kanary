"""OutputFollowups: in-memory followup callbacks for outputs.

Inside KANARY the callbacks run on the output's own thread (see dispatch.py),
between emit() calls, so an output never has two of its methods running at
once. Used outside KANARY (a test, a script), it runs them on a thread of its own.
"""

from collections.abc import Callable
from dataclasses import dataclass, field
import heapq
import logging
import math
import threading
import time

from ..dispatch import OutputWorker, current_worker
from ..models import AlertEvent

logger = logging.getLogger("kanary.output")


@dataclass(slots=True)
class _FollowupEntry:
    callback: Callable[[AlertEvent], None]
    callback_key: tuple[int, int]


@dataclass(slots=True)
class _FollowupEpisode:
    started_at: float
    event: AlertEvent
    entries: dict[int, _FollowupEntry] = field(default_factory=dict)


class _EventFollowups:
    def __init__(self, owner: "OutputFollowups", event: AlertEvent) -> None:
        self._owner = owner
        self._event = event

    def now(self, callback: Callable[[AlertEvent], None]) -> None:
        """Run callback synchronously with the current event."""
        if not callable(callback):
            raise TypeError("followup callback must be callable")
        callback(self._event)

    def after(
        self,
        delay: float,
        callback: Callable[[AlertEvent], None],
    ) -> None:
        """Run callback after delay from the start of this alert episode."""
        self._owner._schedule(self._event, delay, callback)

    def cancel(
        self,
        callback: Callable[[AlertEvent], None] | None = None,
    ) -> None:
        """Cancel all followups for this event, or only one callback."""
        self._owner._cancel(self._event.rule_id, callback)


class OutputFollowups:
    """Schedule in-memory followups for the latest event of each rule."""

    def __init__(self) -> None:
        self._condition = threading.Condition()
        self._episodes: dict[str, _FollowupEpisode] = {}
        self._heap: list[tuple[float, int, str]] = []
        self._sequence = 0
        self._worker: threading.Thread | None = None  # own thread, only outside an output worker
        self._output_worker: OutputWorker | None = None
        self._closed = False

    def for_event(self, event: AlertEvent) -> _EventFollowups:
        """Select an event and refresh callbacks with its latest state."""
        self._bind()
        with self._condition:
            self._ensure_open()
            episode = self._episodes.get(event.rule_id)
            if episode is not None:
                episode.event = event
        return _EventFollowups(self, event)

    def close(self) -> None:
        """Cancel pending followups and stop running callbacks."""
        with self._condition:
            if self._closed:
                return
            self._closed = True
            self._episodes.clear()
            self._heap.clear()
            worker = self._worker
            output_worker = self._output_worker
            self._condition.notify_all()

        if output_worker is not None:
            output_worker.remove_timer(self)
        if worker is not None and worker is not threading.current_thread():
            worker.join(timeout=2.0)
            if worker.is_alive():
                logger.warning("output followup worker did not stop before timeout")

    # Timer source for the output worker (dispatch.TimerSource).

    def next_deadline(self) -> float | None:
        with self._condition:
            due = self._next_due()
        return due[0] if due is not None else None

    def run_due(self) -> None:
        """Run every callback that is due now."""
        while True:
            with self._condition:
                popped = self._pop_due(time.monotonic())
            if popped is None:
                return
            self._call(*popped)

    def _bind(self) -> None:
        """Use the output's thread when called from one (normally inside emit())."""
        if self._output_worker is not None or self._worker is not None:
            return
        worker = current_worker()
        if worker is None:
            return
        with self._condition:
            if self._output_worker is not None or self._worker is not None:
                return
            self._output_worker = worker
        worker.add_timer(self)

    def _schedule(
        self,
        event: AlertEvent,
        delay: float,
        callback: Callable[[AlertEvent], None],
    ) -> None:
        try:
            delay_seconds = float(delay)
        except (TypeError, ValueError) as exc:
            raise ValueError("followup delay must be a finite non-negative number") from exc
        if not math.isfinite(delay_seconds) or delay_seconds < 0:
            raise ValueError("followup delay must be a finite non-negative number")
        if not callable(callback):
            raise TypeError("followup callback must be callable")

        self._bind()
        with self._condition:
            self._ensure_open()
            episode = self._episodes.get(event.rule_id)
            if episode is None:
                episode = _FollowupEpisode(started_at=time.monotonic(), event=event)
                self._episodes[event.rule_id] = episode
            else:
                episode.event = event

            self._sequence += 1
            sequence = self._sequence
            episode.entries[sequence] = _FollowupEntry(
                callback=callback,
                callback_key=_callback_key(callback),
            )
            heapq.heappush(
                self._heap,
                (episode.started_at + delay_seconds, sequence, event.rule_id),
            )
            if self._output_worker is None:
                self._start_worker()
            self._condition.notify_all()
        self._wake_output_worker()

    def _cancel(
        self,
        rule_id: str,
        callback: Callable[[AlertEvent], None] | None,
    ) -> None:
        with self._condition:
            self._ensure_open()
            episode = self._episodes.get(rule_id)
            if episode is None:
                return
            if callback is None:
                del self._episodes[rule_id]
            else:
                callback_key = _callback_key(callback)
                episode.entries = {
                    sequence: entry
                    for sequence, entry in episode.entries.items()
                    if entry.callback_key != callback_key
                }
                if not episode.entries:
                    del self._episodes[rule_id]
            self._condition.notify_all()
        self._wake_output_worker()

    def _wake_output_worker(self) -> None:
        worker = self._output_worker
        if worker is not None and current_worker() is not worker:
            worker.wake()  # changed from another thread; on the worker it rechecks by itself

    def _start_worker(self) -> None:
        if self._worker is not None:
            return
        self._worker = threading.Thread(
            target=self._run,
            name="kanary-output-followups",
            daemon=True,
        )
        self._worker.start()

    def _run(self) -> None:
        while True:
            with self._condition:
                due = self._next_due()
                while due is None:
                    if self._closed:
                        return
                    self._condition.wait()
                    due = self._next_due()

                remaining = due[0] - time.monotonic()
                if remaining > 0:
                    self._condition.wait(timeout=remaining)
                    continue
                popped = self._pop_due(time.monotonic())
            if popped is not None:
                self._call(*popped)

    def _pop_due(self, now: float) -> tuple[_FollowupEntry, AlertEvent, str] | None:
        """Remove and return the next due callback; call with the condition held."""
        while True:
            due = self._next_due()
            if due is None or due[0] > now:
                return None
            _deadline, sequence, rule_id = heapq.heappop(self._heap)
            episode = self._episodes.get(rule_id)
            if episode is None:
                continue
            entry = episode.entries.pop(sequence, None)
            if entry is None:
                continue
            event = episode.event
            if not episode.entries:
                del self._episodes[rule_id]
            return entry, event, rule_id

    def _call(self, entry: _FollowupEntry, event: AlertEvent, rule_id: str) -> None:
        try:
            entry.callback(event)
        except Exception as exc:
            logger.exception(
                "output followup callback failed: rule=%s callback=%r",
                rule_id,
                entry.callback,
            )
            if self._output_worker is not None:
                self._output_worker.report_error(f"followup callback for {rule_id}", exc)

    def _next_due(self) -> tuple[float, int, str] | None:
        while self._heap:
            due = self._heap[0]
            _deadline, sequence, rule_id = due
            episode = self._episodes.get(rule_id)
            if episode is not None and sequence in episode.entries:
                return due
            heapq.heappop(self._heap)
        return None

    def _ensure_open(self) -> None:
        if self._closed:
            raise RuntimeError("OutputFollowups is closed")


def _callback_key(callback: Callable[[AlertEvent], None]) -> tuple[int, int]:
    instance = getattr(callback, "__self__", None)
    function = getattr(callback, "__func__", callback)
    return id(instance), id(function)
