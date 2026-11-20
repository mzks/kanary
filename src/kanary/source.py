from collections.abc import Mapping
from datetime import datetime
from typing import Any

from .models import SourceResult, normalize_input_items


class Source:
    source_id: str
    description: str | None = None
    interval: float | None = None
    schedule: str | None = None
    max_retry: int = 1
    max_reinit: int = 1

    def init(self) -> None:
        return None

    def poll(self) -> SourceResult:
        raise NotImplementedError

    def test_poll(self) -> SourceResult:
        """What poll() would return now, without changing this source (kanaryctl test-poll).

        The default is poll() itself, which is right when poll() only reads. What it
        adds to a kanary.Buffer is dropped afterwards, so keeping history needs
        nothing here. Override it only when poll() takes something that is then gone -
        values from a queue, a read position in a log - and return the same kind of
        result without taking it. It runs on the source's own thread, between polls,
        and its result is only shown, never evaluated.
        """
        return self.poll()

    def terminate(self) -> None:
        return None


def inputs(
    *items: Any,
    timestamp: datetime | None = None,
    metadata: Mapping[str, Any] | None = None,
) -> SourceResult:
    normalized_measurements = normalize_input_items(items, default_timestamp=timestamp)
    return SourceResult(
        measurements=normalized_measurements,
        metadata=dict(metadata or {}),
    )


def no_data(
    *,
    reason: str | None = None,
    metadata: Mapping[str, Any] | None = None,
) -> SourceResult:
    return SourceResult(
        measurements=[],
        status="empty",
        reason=reason,
        metadata=dict(metadata or {}),
    )


def no_update(
    *,
    reason: str | None = None,
    metadata: Mapping[str, Any] | None = None,
) -> SourceResult:
    return SourceResult(
        measurements=[],
        status="no_update",
        reason=reason,
        metadata=dict(metadata or {}),
    )


def skip(
    *,
    reason: str | None = None,
    metadata: Mapping[str, Any] | None = None,
) -> SourceResult:
    return SourceResult(
        measurements=[],
        status="skip",
        reason=reason,
        metadata=dict(metadata or {}),
    )
