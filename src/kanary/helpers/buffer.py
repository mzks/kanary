"""Buffer and Series: recent history kept by a source.

A Buffer is an ordinary object a source creates in init() and fills in poll()::

    def init(self):
        self.buffer = kanary.Buffer(size=1024, max_age=3 * kanary.hour)

    def poll(self):
        self.buffer.add(("pressure", read_gauge()))   # the same items as kanary.inputs()
        pressure = self.buffer["pressure"].window(10 * kanary.minute)
        return kanary.inputs(("pressure_avg_10m", pressure.mean()))

It holds, per input name, the samples added to it, oldest first; each sample has
.name, .value, .timestamp, and .metadata. ``buffer["name"]`` is a
Series: a read-only copy of those samples that answers the usual questions
(last.value, mean(), max(), slope(), ...). A statistic returns None when there
is no data for it (an empty window, a single sample for a rate). What to publish
while the buffer is still filling is the source's choice: check len(series) or
series.span and, for example, return kanary.skip() until there is enough.

kanaryctl test-poll runs poll() too; what it adds to a Buffer is dropped when
the test poll ends, so the history poll() sees is not changed by it. A Buffer has
no lock: use it from the source's poll() (where test-poll also runs), or lock
it yourself when another thread adds to it.
"""

from collections import deque
from collections.abc import Iterator
from contextlib import contextmanager
from datetime import datetime, timedelta, timezone
from fnmatch import fnmatchcase
import statistics
import threading
from typing import Any

from ..models import InputValue, PollResult, normalize_input_items
from ..units import format_time
from ..values import coerce_datetime, is_number

_scratch = threading.local()


@contextmanager
def discarding_changes() -> Iterator[None]:
    """Changes made on this thread to any Buffer inside are dropped at the end.

    The engine runs test polls in it: a Buffer changed here works on a copy of
    its samples, which this thread reads until the block ends. Other threads keep
    reading and adding to the real samples meanwhile.
    """
    previous = getattr(_scratch, "copies", None)
    _scratch.copies = {}
    try:
        yield
    finally:
        _scratch.copies = previous


def in_test_poll() -> bool:
    """True while a test poll runs on this thread (inside discarding_changes())."""
    return getattr(_scratch, "copies", None) is not None


class Series:
    """The samples of one input, oldest first. Read-only; methods return new Series."""

    __slots__ = ("name", "_samples")

    def __init__(self, name: str, samples: Any = ()) -> None:
        self.name = name
        self._samples: tuple[InputValue, ...] = tuple(samples)

    # -- samples ------------------------------------------------------------

    def __len__(self) -> int:
        return len(self._samples)

    def __iter__(self) -> Iterator[InputValue]:
        return iter(self._samples)

    def __getitem__(self, index: int) -> InputValue:
        return self._samples[index]

    def __repr__(self) -> str:
        return f"Series({self.name!r}, {len(self)} samples)"

    @property
    def first(self) -> InputValue | None:
        """The oldest sample (.value, .timestamp, .metadata), or None."""
        return self._samples[0] if self._samples else None

    @property
    def last(self) -> InputValue | None:
        """The newest sample, or None."""
        return self._samples[-1] if self._samples else None

    @property
    def values(self) -> list[Any]:
        """The values, oldest first: a plain list (numpy.array(series.values) works)."""
        return [sample.value for sample in self._samples]

    @property
    def timestamps(self) -> list[datetime]:
        return [sample.timestamp for sample in self._samples]

    @property
    def span(self) -> float:
        """Seconds from the first to the last sample (0 with fewer than two)."""
        if len(self._samples) < 2:
            return 0.0
        return (self._samples[-1].timestamp - self._samples[0].timestamp).total_seconds()

    # -- selecting ----------------------------------------------------------

    def window(self, duration: float) -> "Series":
        """The samples of the last ``duration`` (``10 * kanary.minute``), counted back from the newest sample."""
        if not self._samples:
            return self
        cutoff = self._samples[-1].timestamp - timedelta(seconds=duration)
        return Series(self.name, (sample for sample in self._samples if sample.timestamp >= cutoff))

    def tail(self, count: int) -> "Series":
        """The newest ``count`` samples."""
        return Series(self.name, self._samples[-count:] if count > 0 else ())

    # -- statistics (None when there is not enough data) ---------------------

    def numbers(self) -> list[float]:
        """The numeric values; None, text, and booleans are left out of statistics."""
        return [sample.value for sample in self._samples if is_number(sample.value)]

    def mean(self) -> float | None:
        values = self.numbers()
        return statistics.fmean(values) if values else None

    def median(self) -> float | None:
        values = self.numbers()
        return statistics.median(values) if values else None

    def min(self) -> float | None:
        values = self.numbers()
        return min(values) if values else None

    def max(self) -> float | None:
        values = self.numbers()
        return max(values) if values else None

    def sum(self) -> float | None:
        values = self.numbers()
        return sum(values) if values else None

    def stddev(self) -> float | None:
        """Sample standard deviation; None with fewer than two values."""
        values = self.numbers()
        return statistics.stdev(values) if len(values) >= 2 else None

    def delta(self) -> float | None:
        """Last value minus first value."""
        points = self._points()
        return points[-1][1] - points[0][1] if len(points) >= 2 else None

    def rate(self) -> float | None:
        """(last - first) / seconds between them: value units per second.

        For another unit, scale it: ``series.rate() * kanary.minute`` is per minute.
        """
        points = self._points()
        if len(points) < 2 or points[-1][0] <= points[0][0]:
            return None
        return (points[-1][1] - points[0][1]) / (points[-1][0] - points[0][0])

    def slope(self) -> float | None:
        """Least-squares trend in value units per second (``* kanary.minute``: per minute).

        Unlike rate(), every sample counts, so one noisy point moves it much less.
        """
        points = self._points()
        if len(points) < 2 or points[-1][0] <= points[0][0]:
            return None
        times, values = zip(*points)
        return statistics.linear_regression(times, values).slope

    def zscore(self) -> float | None:
        """How many standard deviations the last value is from the mean (spikes)."""
        values = self.numbers()
        if len(values) < 3:
            return None
        spread = statistics.stdev(values)
        return (values[-1] - statistics.fmean(values)) / spread if spread else None

    def eta(self, target: float) -> float | None:
        """Seconds until the trend reaches ``target`` from the last value.

        None when the trend is flat or moving away (also when the last value is
        already past target), so check the value itself as well.
        """
        points = self._points()
        slope = self.slope()
        if not points or not slope:
            return None
        seconds = (target - points[-1][1]) / slope
        return seconds if seconds >= 0 else None

    def changes(self, *, tolerance: float = 0.0) -> int:
        """How many times the value changed between consecutive samples (toggling).

        Only sampled values are compared: a value that flips and flips back
        between two samples is not seen.
        """
        values = self.values
        return sum(1 for before, after in zip(values, values[1:]) if not _same(before, after, tolerance))

    def unchanged_seconds(self, *, tolerance: float = 0.0) -> float | None:
        """Seconds the value has stayed within ``tolerance`` of the last value.

        A frozen sensor keeps sending new timestamps with the same value, which a
        stale check cannot see.
        """
        if not self._samples:
            return None
        last = self._samples[-1]
        since = last.timestamp
        for sample in reversed(self._samples):
            if not _same(sample.value, last.value, tolerance):
                break
            since = sample.timestamp
        return (last.timestamp - since).total_seconds()

    def _points(self) -> list[tuple[float, float]]:
        """(seconds from the first sample, value) for the numeric samples."""
        samples = [sample for sample in self._samples if is_number(sample.value)]
        if not samples:
            return []
        origin = samples[0].timestamp
        return [((sample.timestamp - origin).total_seconds(), sample.value) for sample in samples]


class Buffer:
    """Recent samples of every input, by name: ``buffer["pressure"]`` is a Series.

    Each input keeps at most ``size`` samples and, when ``max_age`` is set
    (``3 * kanary.hour``), none older than that; whichever limit is reached first
    drops the oldest samples. The age is counted back from the input's newest
    sample, not from now: samples stay while no newer one arrives.
    ``print(buffer)`` shows what is kept.
    """

    def __init__(self, *, size: int = 1024, max_age: float | None = None) -> None:
        if not isinstance(size, int) or isinstance(size, bool) or size < 1:
            raise ValueError(f"Buffer size must be a positive integer, not {size!r}")
        if max_age is not None and (not is_number(max_age) or max_age <= 0):
            raise ValueError(f"Buffer max_age must be a positive duration or None, not {max_age!r}")
        self.size = size
        self.max_age = max_age
        self._samples: dict[str, deque[InputValue]] = {}

    # -- reading ------------------------------------------------------------

    def __getitem__(self, name: str) -> Series:
        """The samples of one input. KeyError for a name never added (check with ``in``)."""
        samples = self._view().get(name)
        if samples is None:
            known = ", ".join(self._view()) or "nothing yet"
            raise KeyError(f"buffer has no input {name!r}; it has: {known}")
        return Series(name, samples)

    def __contains__(self, name: object) -> bool:
        return name in self._view()

    def __iter__(self) -> Iterator[str]:
        return iter(list(self._view()))

    def __len__(self) -> int:
        return len(self._view())

    def __repr__(self) -> str:
        counts = ", ".join(f"{name}: {len(samples)}" for name, samples in self._view().items())
        return f"Buffer({counts})"

    def __str__(self) -> str:
        limits = f"at most {self.size} samples per input"
        if self.max_age is not None:
            limits += f", none older than {format_time(self.max_age)}"
        lines = [f"Buffer ({limits}): {len(self)} inputs"]
        for name, samples in self._view().items():
            first, last = samples[0], samples[-1]
            lines.append(
                f"  {name}: {len(samples)} samples, "
                f"{first.timestamp.isoformat(timespec='seconds')} .. {last.timestamp.isoformat(timespec='seconds')}, "
                f"last {last.value!r}"
            )
        return "\n".join(lines)

    @property
    def names(self) -> list[str]:
        return list(self._view())

    def select(self, *patterns: str) -> dict[str, Series]:
        """The series whose names match any of the glob patterns, in insertion order.

        ``buffer.select("temp_*")`` gathers every temperature; with no pattern, all.
        """
        return {
            name: Series(name, samples)
            for name, samples in self._view().items()
            if not patterns or any(fnmatchcase(name, pattern) for pattern in patterns)
        }

    def last_values(self, *patterns: str) -> dict[str, Any]:
        """{name: newest value} for the matching series (all with no pattern).

        ``kanary.inputs(buffer.last_values())`` publishes them as they are.
        """
        return {name: series.last.value for name, series in self.select(*patterns).items()}

    # -- changing -----------------------------------------------------------

    def add(self, *items: Any, timestamp: Any = None) -> None:
        """Add samples, written the same way as for kanary.inputs()::

            buffer.add(("pressure", 1.02))                          # observed now
            buffer.add(("pressure", 1.02), ("temperature", 21.5))   # several
            buffer.add(("pressure", 1.02, observed_at))             # with its own time
            buffer.add({"pressure": 1.02, "temperature": 21.5})
            buffer.add(result)  # a whole poll result: kanary.inputs(...), super().poll() of a helper

        A timestamp is a datetime, ISO 8601 text, or unix time; ``timestamp=``
        gives one to the samples without their own (default: now). A result that
        is not ok (no_data(), no_update(), skip()) adds nothing. A sample with the same
        timestamp as the input's newest one replaces it (the same reading again);
        an older one is ignored.
        """
        if len(items) == 1 and (items[0] is None or isinstance(items[0], PollResult)):
            if timestamp is not None:
                raise TypeError("timestamp= is for (name, value) items; a result carries its own")
            result = items[0]
            if result is None or result.status != "ok":
                return
            samples = result.inputs
        else:
            when = coerce_datetime(timestamp) if timestamp is not None else datetime.now(timezone.utc)
            samples = normalize_input_items(items, default_timestamp=when)
        store = self._writable()
        for sample in samples:
            self._append(store, sample)

    def clear(self, name: str | None = None) -> None:
        """Forget one input's samples, or everything."""
        store = self._writable()
        if name is None:
            store.clear()
        else:
            store.pop(name, None)

    def copy(self) -> "Buffer":
        copied = Buffer(size=self.size, max_age=self.max_age)
        copied._samples = _copy_samples(self._view())
        return copied

    # -- internals ----------------------------------------------------------

    def _append(self, store: dict[str, deque[InputValue]], sample: InputValue) -> None:
        samples = store.setdefault(sample.name, deque(maxlen=self.size))
        if samples and sample.timestamp <= samples[-1].timestamp:
            if sample.timestamp == samples[-1].timestamp:
                samples[-1] = sample
            return
        samples.append(sample)
        if self.max_age is not None:
            cutoff = sample.timestamp - timedelta(seconds=self.max_age)
            while samples and samples[0].timestamp < cutoff:
                samples.popleft()

    def _view(self) -> dict[str, deque[InputValue]]:
        """The samples this thread sees: the test poll's copy while one changed it."""
        copies = getattr(_scratch, "copies", None)
        if copies:
            entry = copies.get(id(self))
            if entry is not None:
                return entry[1]
        return self._samples

    def _writable(self) -> dict[str, deque[InputValue]]:
        """The samples to change: inside discarding_changes(), a copy kept until it ends."""
        copies = getattr(_scratch, "copies", None)
        if copies is None:
            return self._samples
        entry = copies.get(id(self))
        if entry is None:
            # The buffer itself is kept in the entry so that its id is not reused meanwhile.
            entry = copies[id(self)] = (self, _copy_samples(self._samples))
        return entry[1]


def _copy_samples(samples: dict[str, deque[InputValue]]) -> dict[str, deque[InputValue]]:
    return {name: deque(kept, maxlen=kept.maxlen) for name, kept in samples.items()}


def _same(before: Any, after: Any, tolerance: float) -> bool:
    if is_number(before) and is_number(after):
        return abs(after - before) <= tolerance
    return before == after
