"""Time-of-day helpers for outputs: QuietHours and OnCallSchedule."""

from collections.abc import Callable, Iterable, Mapping
from dataclasses import dataclass
from datetime import datetime, time, timedelta, timezone as dt_timezone
import logging
import threading
from time import monotonic
from typing import Any

from ..constants import CRITICAL, Severity, coerce_severity
from ..dispatch import OutputWorker, current_worker
from ..values import address_list, to_timezone

logger = logging.getLogger("kanary.output")

# What QuietHours sends when a quiet period ends (see its docstring).
RELEASE_MODES = ("all", "latest", "summary")

_WEEKDAYS = ("monday", "tuesday", "wednesday", "thursday", "friday", "saturday", "sunday")


def parse_weekdays(value: Any) -> tuple[int, ...]:
    """Weekdays (Monday = 0) from names, abbreviated or not: ["mon", "Tuesday"], "mon-fri",
    "mon,wed,fri", or "fri-mon" (round the weekend). An unknown name is an error."""
    items = [value] if isinstance(value, str) else list(value or [])
    days: list[int] = []
    for item in items:
        for part in (piece.strip() for piece in str(item).split(",")):
            if not part:
                continue
            start, dash, end = part.partition("-")
            first = _weekday(start)
            if not dash:
                days.append(first)
                continue
            last = _weekday(end)
            span = (last - first) % 7
            days.extend((first + offset) % 7 for offset in range(span + 1))
    return tuple(dict.fromkeys(days))


def _weekday(text: str) -> int:
    word = text.strip().lower()
    if len(word) >= 3:
        for index, name in enumerate(_WEEKDAYS):
            if name.startswith(word):
                return index
    raise ValueError(f"weekdays must be names such as 'mon', 'Tuesday', or 'mon-fri', not {text.strip()!r}")


def parse_clock(value: str | time) -> time:
    """Parse "HH:MM" or "H" (or a datetime.time); "24:00" is the midnight that ends the day."""
    if isinstance(value, time):
        return value
    hours, _, minutes = str(value).strip().partition(":")
    if int(hours) == 24 and int(minutes or 0) == 0:
        return time(0, 0)  # as an end, ("22:00", "24:00") runs to midnight
    return time(int(hours), int(minutes or 0))


def quiet_windows(quiet_hours: Any) -> list[tuple[time, time]]:
    """(start, end) clock times from [("22:00", "07:00"), ...], or one pair written alone."""
    if _is_pair(quiet_hours):
        quiet_hours = [quiet_hours]
    windows = []
    for start, end in quiet_hours:
        start_time, end_time = parse_clock(start), parse_clock(end)
        if start_time == end_time:
            raise ValueError(
                f"quiet hours ({start!r}, {end!r}) cover the whole day; "
                "to hold low-severity events always, use the output's minimum_severity instead"
            )
        windows.append((start_time, end_time))
    return windows


def _is_pair(value: Any) -> bool:
    return (
        isinstance(value, (list, tuple))
        and len(value) == 2
        and all(isinstance(item, (str, time)) for item in value)
    )


def in_daily_window(moment: time, start: time, end: time) -> bool:
    """True when moment is in [start, end); windows may wrap past midnight."""
    if start <= end:
        return start <= moment < end
    return moment >= start or moment < end


def local_time(moment: datetime, timezone: Any) -> datetime:
    """Convert to the time zone (see kanary.to_timezone()), or to the host's local time when None."""
    return moment.astimezone(to_timezone(timezone)) if timezone else moment.astimezone()


def in_quiet_hours(moment: datetime, quiet_hours: Iterable[Any], timezone: str | None = None) -> bool:
    local = local_time(moment, timezone).time()
    return any(in_daily_window(local, start, end) for start, end in quiet_windows(quiet_hours))


def quiet_hours_end(moment: datetime, quiet_hours: Iterable[Any], timezone: str | None = None) -> datetime | None:
    """When the quiet period that contains moment ends (UTC); None when moment is not quiet.

    Overlapping or back-to-back windows are treated as one period.
    """
    windows = quiet_windows(quiet_hours)
    end_at: datetime | None = None
    current = moment
    for _ in range(len(windows) + 1):
        local = local_time(current, timezone)
        ends = []
        for start, end in windows:
            if not in_daily_window(local.time(), start, end):
                continue
            day = local.date()
            if start > end and local.time() >= start:
                day += timedelta(days=1)  # a window wrapping past midnight ends tomorrow
            ends.append(datetime.combine(day, end, tzinfo=local.tzinfo))
        if not ends:
            break
        end_at = max(ends).astimezone(dt_timezone.utc)
        current = end_at
    return end_at


def check_quiet_hours(quiet_hours: Any) -> list[str]:
    problems = []
    if not isinstance(quiet_hours, (list, tuple)):
        return ["quiet hours must be a list of (start, end) pairs such as [('22:00', '07:00')]"]
    for window in [quiet_hours] if _is_pair(quiet_hours) else quiet_hours:
        try:
            quiet_windows([window])
        except ValueError as exc:
            problems.append(str(exc) if "whole day" in str(exc) else f"quiet hours entry {window!r} must be a ('HH:MM', 'HH:MM') pair")
        except TypeError:
            problems.append(f"quiet hours entry {window!r} must be a ('HH:MM', 'HH:MM') pair")
    return problems


def _same_story(held: Any, event: Any) -> bool:
    """Events of the same rule; a test fire's are kept apart from the rule's real events."""
    return held.rule_id == event.rule_id and getattr(held, "synthetic", False) == getattr(event, "synthetic", False)


class QuietHours:
    """Hold alert events during quiet hours and send them when the quiet period ends.

    Use it from an output's emit(), like OutputFollowups:

        def init(self):
            self.quiet = kanary.QuietHours([("22:00", "07:00")], timezone="Asia/Tokyo")

        def terminate(self):
            self.quiet.close()

        def emit(self, event):
            self.quiet.send(event, self.notify)

    send() calls callback(event) right away unless the event falls in a quiet
    window and is below minimum_severity. Such events are held until the quiet
    period ends (back-to-back windows count as one period); ``release`` decides
    what goes out then:

    - "all" (default): every held event, in its original order
    - "latest": only the newest held event of each rule
    - "summary": one call of summary(events) with every held event, for one
      message instead of many (the chat and mail outputs have emit_summary());
      a single held event goes to callback as usual

    Inside KANARY that happens on the output's own thread, between emit() calls;
    used elsewhere, on a timer thread. With defer=False held events are dropped
    instead.

    When an event of a rule is sent right away (for example an escalation to
    CRITICAL at night), that rule's held events go out first with "all", so the
    receiver never sees them out of order; with "latest" and "summary" they are
    dropped, since the event sent now is newer.

    Held events are kept in memory and lost on close() or restart. A callback
    that fails for a held event is logged and not retried; a failure while
    sending right away propagates, so the engine retries emit() as usual.
    """

    def __init__(
        self,
        windows: Iterable[Any],
        *,
        timezone: str | None = None,
        minimum_severity: Severity | str = CRITICAL,
        defer: bool = True,
        release: str = "all",
        summary: Callable[[list[Any]], None] | None = None,
        now_fn: Callable[[], datetime] | None = None,
    ) -> None:
        if release not in RELEASE_MODES:
            raise ValueError(f"release must be one of {', '.join(map(repr, RELEASE_MODES))}, not {release!r}")
        if release == "summary" and summary is None:
            raise ValueError("release='summary' needs summary=, which sends a list of events as one message "
                             "(the chat and mail outputs have emit_summary)")
        self.release = release
        self.summary = summary
        self.windows = [tuple(window) for window in ([windows] if _is_pair(windows) else windows)]
        problems = check_quiet_hours(self.windows)
        if problems:
            raise ValueError("; ".join(problems))
        if timezone:
            to_timezone(timezone)  # raises for an unknown zone
        self.timezone = timezone
        self.minimum_severity = coerce_severity(minimum_severity)  # kanary.ERROR, "error", 30
        self.defer = defer
        self._now = now_fn or (lambda: datetime.now(dt_timezone.utc))
        self._lock = threading.Lock()
        self._held: list[tuple[datetime, Any, Callable[[Any], None]]] = []
        self._timer: threading.Timer | None = None  # only outside an output worker
        self._worker: OutputWorker | None = None
        self._closed = False

    def applies(self, event: Any) -> bool:
        """True when this event falls in quiet hours and is below minimum_severity."""
        return event.effective_severity < self.minimum_severity and in_quiet_hours(
            event.occurred_at, self.windows, self.timezone
        )

    def send(self, event: Any, callback: Callable[[Any], None]) -> bool:
        """Call callback(event) now, or hold it; returns True when it was called now."""
        self._bind()
        with self._lock:
            if self._closed:
                raise RuntimeError("QuietHours is closed")
            if self.applies(event):
                if self.defer:
                    if self.release == "latest":
                        self._held = [item for item in self._held if not _same_story(item[1], event)]
                    release_at = quiet_hours_end(event.occurred_at, self.windows, self.timezone)
                    self._held.append((release_at or self._now(), event, callback))
                    self._schedule()
                return False
            earlier = [item for item in self._held if _same_story(item[1], event)]
            self._held = [item for item in self._held if not _same_story(item[1], event)]
            self._schedule()
        if self.release == "all":
            self._run_held(earlier)  # the newer event sent now replaces them otherwise
        callback(event)
        return True

    def held(self) -> list[tuple[datetime, Any]]:
        """(release_at, event) of every held event, in order."""
        with self._lock:
            return [(release_at, event) for release_at, event, _ in self._held]

    def release_due(self, *, now: datetime | None = None) -> int:
        """Send held events whose quiet period has ended; the timer calls this."""
        current = now or self._now()
        with self._lock:
            due = [item for item in self._held if item[0] <= current]
            self._held = [item for item in self._held if item[0] > current]
            self._schedule()
        self._run_held(due)
        return len(due)

    def close(self) -> None:
        """Stop the timer; events still held are dropped (and logged)."""
        with self._lock:
            self._closed = True
            if self._timer is not None:
                self._timer.cancel()
                self._timer = None
            if self._held:
                logger.warning("dropping %d alert event(s) held for quiet hours", len(self._held))
            self._held = []
            worker = self._worker
        if worker is not None:
            worker.remove_timer(self)

    # Timer source for the output worker (dispatch.TimerSource).

    def next_deadline(self) -> float | None:
        with self._lock:
            if self._closed or not self._held:
                return None
            earliest = min(item[0] for item in self._held)
        return monotonic() + max(0.0, (earliest - self._now()).total_seconds())

    def run_due(self) -> None:
        self.release_due()

    def _bind(self) -> None:
        """Use the output's thread when called from one (normally inside emit())."""
        worker = current_worker()
        if worker is None or self._worker is not None:
            return
        with self._lock:
            if self._worker is not None or self._timer is not None:
                return
            self._worker = worker
        worker.add_timer(self)

    def _run_held(self, items: list[tuple[datetime, Any, Callable[[Any], None]]]) -> None:
        if self.release == "summary" and len(items) > 1 and self.summary is not None:
            try:
                self.summary([event for _release_at, event, _callback in items])
            except Exception:
                logger.exception("sending the summary of %d held events failed", len(items))
            return
        for _release_at, event, callback in items:
            try:
                callback(event)
            except Exception:
                logger.exception("sending a held event failed: rule=%s", event.rule_id)

    def _schedule(self) -> None:
        # Called with the lock held: keep one timer armed for the earliest release.
        if self._worker is not None:
            if current_worker() is not self._worker:
                self._worker.wake()  # it asks next_deadline() again
            return
        if self._timer is not None:
            self._timer.cancel()
            self._timer = None
        if not self._held or self._closed:
            return
        delay = max(0.0, (min(item[0] for item in self._held) - self._now()).total_seconds())
        self._timer = threading.Timer(delay, self._on_timer)
        self._timer.daemon = True
        self._timer.start()

    def _on_timer(self) -> None:
        try:
            self.release_due()
        except Exception:
            logger.exception("releasing held alert events failed")


@dataclass(frozen=True, slots=True)
class Shift:
    recipients: tuple[str, ...]
    start: datetime | None = None  # dated shift: [start, end)
    end: datetime | None = None
    weekdays: tuple[int, ...] = ()  # weekly shift: these days, between start_time and end_time
    start_time: time | None = None
    end_time: time | None = None

    def covers(self, local: datetime) -> bool:
        if self.start is not None and self.end is not None:
            return self.start <= local < self.end
        day, now = local.weekday(), local.time()
        if self.start_time is None or self.end_time is None:
            return not self.weekdays or day in self.weekdays
        if self.start_time > self.end_time and now < self.end_time:
            # The early-morning part of a shift that started the day before.
            return not self.weekdays or (day - 1) % 7 in self.weekdays
        if self.weekdays and day not in self.weekdays:
            return False
        return in_daily_window(now, self.start_time, self.end_time)


class OnCallSchedule:
    """Who to notify at a given time.

    Each shift is a mapping, typically loaded with kanary.load_toml():

        [[shift]]                          # dated: wins over weekly shifts
        start = 2026-12-28T09:00:00
        end   = 2027-01-04T09:00:00
        recipients = ["holiday-oncall@example.org"]

        [[shift]]                          # weekly
        weekdays = ["mon", "tue", "wed", "thu", "fri"]
        from = "09:00"
        to   = "18:00"
        recipients = ["day-shift@example.org"]

    Naive datetimes and clock times are read in ``timezone`` (host local time
    when None). ``default`` is returned when no shift covers the moment.
    """

    def __init__(
        self,
        shifts: Iterable[Mapping[str, Any]],
        *,
        timezone: str | None = None,
        default: Iterable[str] = (),
    ) -> None:
        self.timezone = timezone
        self.default = address_list(default)
        self.shifts = [self._parse_shift(index, raw) for index, raw in enumerate(shifts)]

    def recipients(self, at: datetime | None = None) -> list[str]:
        local = local_time(at or datetime.now().astimezone(), self.timezone)
        dated = [shift for shift in self.shifts if shift.start is not None and shift.covers(local)]
        weekly = [shift for shift in self.shifts if shift.start is None and shift.covers(local)]
        chosen = dated or weekly
        if not chosen:
            return list(self.default)
        recipients: list[str] = []
        for shift in chosen:
            recipients.extend(item for item in shift.recipients if item not in recipients)
        return recipients

    def _parse_shift(self, index: int, raw: Mapping[str, Any]) -> Shift:
        where = f"shift #{index + 1}"
        recipients = address_list(raw.get("recipients"))
        if not recipients:
            raise ValueError(f"{where} must list recipients")
        if "start" in raw or "end" in raw:
            start, end = self._localize(raw.get("start")), self._localize(raw.get("end"))
            if start is None or end is None or start >= end:
                raise ValueError(f"{where} needs start < end")
            return Shift(tuple(recipients), start=start, end=end)
        try:
            days = parse_weekdays(raw.get("weekdays", []))
        except ValueError as exc:
            raise ValueError(f"{where} {exc}") from None
        start_time = parse_clock(raw["from"]) if "from" in raw else None
        end_time = parse_clock(raw["to"]) if "to" in raw else None
        if (start_time is None) != (end_time is None):
            raise ValueError(f"{where} needs both 'from' and 'to'")
        if start_time is not None and start_time == end_time:
            raise ValueError(f"{where} 'from' and 'to' are the same time: for the whole day, leave both out")
        return Shift(tuple(recipients), weekdays=days, start_time=start_time, end_time=end_time)

    def _localize(self, value: Any) -> datetime | None:
        if value is None:
            return None
        moment = value if isinstance(value, datetime) else datetime.fromisoformat(str(value))
        if moment.tzinfo is None:
            zone = to_timezone(self.timezone) if self.timezone else datetime.now().astimezone().tzinfo
            moment = moment.replace(tzinfo=zone)
        return local_time(moment, self.timezone)
