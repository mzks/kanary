"""One thread per output.

Everything an output does - init(), emit(), followup callbacks, terminate() -
runs on that output's own thread, one call at a time. The engine only decides
which outputs an alert event goes to and puts it in their queues, so a slow or
unreachable destination delays that output alone, never rule evaluation, other
outputs, or operator actions.
"""

from collections import deque
from collections.abc import Callable
from concurrent.futures import Future
from dataclasses import dataclass
import logging
import threading
import time
from typing import Any, Protocol

logger = logging.getLogger("kanary.output")

# Events waiting per output; beyond this the oldest is dropped (and recorded as not sent).
MAX_QUEUED_EVENTS = 1000
# On shutdown, how long queued events may still be sent before the rest is dropped.
SHUTDOWN_DRAIN_SECONDS = 5.0

_local = threading.local()


def current_worker() -> "OutputWorker | None":
    """The worker whose thread is running the caller, if any."""
    return getattr(_local, "worker", None)


class TimerSource(Protocol):
    """Something with timed callbacks to run on the worker thread (OutputFollowups).

    A source may set ``sends = False`` when its callbacks send nothing (an init() retry):
    a full restart does not wait for them (see OutputWorker.flush_sending).
    """

    def next_deadline(self) -> float | None: ...

    def run_due(self) -> None: ...


@dataclass(slots=True)
class _Job:
    run: Callable[[Any], Any]  # receives the worker's current output
    future: Future | None = None
    on_drop: Callable[[str], None] | None = None


class OutputWorker:
    """The thread of one output: control jobs first, then events in arrival order."""

    def __init__(
        self,
        output_id: str,
        output: Any,
        *,
        max_queued: int = MAX_QUEUED_EVENTS,
        on_error: Callable[[str, BaseException], None] | None = None,
    ) -> None:
        self.output_id = output_id
        self.output = output  # replaced only on this thread (see the engine's reload)
        self.max_queued = max_queued
        self.dropped = 0
        self._on_error = on_error
        self._condition = threading.Condition()
        self._events: deque[_Job] = deque()
        self._controls: deque[_Job] = deque()
        self._timers: list[TimerSource] = []
        self._timers_changed = False
        self._busy = False
        self._sending = False  # an event, or a timer callback that sends (not an init() retry)
        self._closing = False
        self._drain_deadline: float | None = None
        self._finalize: Callable[[Any], None] | None = None
        self._thread = threading.Thread(target=self._run, name=f"kanary-output-{output_id}", daemon=True)
        self._thread.start()

    # -- used by the engine -------------------------------------------------

    def submit(self, run: Callable[[Any], Any], *, on_drop: Callable[[str], None]) -> None:
        """Queue an event; when the queue is full the oldest event is dropped."""
        dropped: tuple[_Job, str] | None = None
        with self._condition:
            job = _Job(run, on_drop=on_drop)
            if self._closing:
                dropped = (job, "output is stopping")
            else:
                self._events.append(job)
                if len(self._events) > self.max_queued:
                    dropped = (self._events.popleft(), f"more than {self.max_queued} events were waiting")
                self._condition.notify_all()
        if dropped is not None:
            self._drop(*dropped)

    def post(self, run: Callable[[Any], Any]) -> Future:
        """Run a control job (init, reload, test fire) before the queued events."""
        future: Future = Future()
        if threading.current_thread() is self._thread:
            _run_into(future, run, self.output)  # already on this thread: run it now
            return future
        with self._condition:
            if self._closing:
                future.set_exception(RuntimeError(f"output '{self.output_id}' is stopping"))
                return future
            self._controls.append(_Job(run, future=future))
            self._condition.notify_all()
        return future

    @property
    def backlog(self) -> int:
        """Events waiting or being sent."""
        with self._condition:
            return len(self._events) + (1 if self._busy else 0)

    def flush(self, timeout: float) -> bool:
        """Wait until everything queued has run; False on timeout."""
        deadline = time.monotonic() + timeout
        with self._condition:
            while self._events or self._controls or self._busy:
                remaining = deadline - time.monotonic()
                if remaining <= 0 or not self._thread.is_alive():
                    return False
                self._condition.wait(remaining)
        return True

    def flush_sending(self, timeout: float) -> bool:
        """Wait until no event is waiting or being sent, nor a callback that sends; False on
        timeout. An init() retry or a control job may still be running."""
        deadline = time.monotonic() + timeout
        with self._condition:
            while self._events or self._sending:
                remaining = deadline - time.monotonic()
                if remaining <= 0 or not self._thread.is_alive():
                    return False
                self._condition.wait(remaining)
        return True

    @property
    def sending(self) -> bool:
        with self._condition:
            return bool(self._events) or self._sending

    def close(self, *, drain_seconds: float, finalize: Callable[[Any], None] | None = None) -> None:
        """Stop taking events, send the queued ones until the deadline, then finalize and stop."""
        with self._condition:
            if self._closing:
                return
            self._closing = True
            self._drain_deadline = time.monotonic() + max(0.0, drain_seconds)
            self._finalize = finalize
            self._condition.notify_all()

    def join(self, timeout: float | None = None) -> bool:
        self._thread.join(timeout)
        return not self._thread.is_alive()

    # -- used on the worker thread ------------------------------------------

    def sleep(self, seconds: float) -> bool:
        """Wait between retries; True means give up (shutdown deadline reached)."""
        end = time.monotonic() + seconds
        with self._condition:
            while True:
                deadline = self._drain_deadline
                limit = end if deadline is None else min(end, deadline)
                now = time.monotonic()
                if now >= limit:
                    return deadline is not None and now >= deadline
                self._condition.wait(limit - now)

    def add_timer(self, timer: TimerSource) -> None:
        with self._condition:
            if timer not in self._timers:
                self._timers.append(timer)
            self._timers_changed = True
            self._condition.notify_all()

    def remove_timer(self, timer: TimerSource) -> None:
        with self._condition:
            if timer in self._timers:
                self._timers.remove(timer)
            self._timers_changed = True
            self._condition.notify_all()

    def wake(self) -> None:
        """A timer source changed from another thread: recompute the next deadline."""
        with self._condition:
            self._timers_changed = True
            self._condition.notify_all()

    def report_error(self, what: str, exc: BaseException) -> None:
        if self._on_error is not None:
            try:
                self._on_error(what, exc)
            except Exception:
                logger.exception("output '%s' error report failed", self.output_id)

    # -- the thread ---------------------------------------------------------

    def _run(self) -> None:
        _local.worker = self
        while True:
            with self._condition:
                self._timers_changed = False
                timers = list(self._timers)
            # Ask the timer sources outside our lock: they take their own lock.
            deadlines = [deadline for timer in timers if (deadline := timer.next_deadline()) is not None]
            next_due = min(deadlines, default=None)

            with self._condition:
                job, is_event, run_timers, stop = self._take_locked(next_due)
                if job is None and not run_timers and not stop:
                    continue
                self._busy = True
                self._sending = is_event
            try:
                if stop:
                    self._stop()
                    return
                if job is not None:
                    if job.future is not None:
                        _run_into(job.future, job.run, self.output)
                    else:
                        try:
                            job.run(self.output)
                        except Exception as exc:
                            logger.exception("output '%s' job failed", self.output_id)
                            self.report_error("event", exc)
                elif run_timers:
                    for timer in timers:
                        with self._condition:
                            self._sending = getattr(timer, "sends", True)
                        timer.run_due()
            finally:
                with self._condition:
                    self._busy = False
                    self._sending = False
                    self._condition.notify_all()

    def _take_locked(self, next_due: float | None) -> tuple[_Job | None, bool, bool, bool]:
        """Pick the next thing to do, or wait; returns (job, is_event, run_timers, stop)."""
        if self._controls:
            return self._controls.popleft(), False, False, False
        now = time.monotonic()
        if self._closing:
            if self._drain_deadline is not None and now >= self._drain_deadline:
                return None, False, False, True
            if self._events:
                return self._events.popleft(), True, False, False
            return None, False, False, True
        if self._events:
            return self._events.popleft(), True, False, False
        if next_due is not None and next_due <= now:
            return None, False, True, False
        if not self._timers_changed:
            self._condition.wait(None if next_due is None else next_due - now)
        return None, False, False, False

    def _stop(self) -> None:
        with self._condition:
            leftover = list(self._events)
            self._events.clear()
            controls = list(self._controls)
            self._controls.clear()
        for job in leftover:
            self._drop(job, "output stopped before it was sent")
        for job in controls:
            if job.future is not None:
                job.future.set_exception(RuntimeError(f"output '{self.output_id}' stopped"))
        if self._finalize is not None:
            try:
                self._finalize(self.output)
            except Exception:
                logger.exception("output '%s' finalize failed", self.output_id)

    def _drop(self, job: _Job, reason: str) -> None:
        with self._condition:
            self.dropped += 1
        logger.warning("output '%s' dropped an event: %s", self.output_id, reason)
        if job.on_drop is not None:
            try:
                job.on_drop(reason)
            except Exception:
                logger.exception("output '%s' drop handling failed", self.output_id)


def _run_into(future: Future, run: Callable[[Any], Any], output: Any) -> None:
    try:
        result = run(output)
    except BaseException as exc:
        future.set_exception(exc)
    else:
        future.set_result(result)
