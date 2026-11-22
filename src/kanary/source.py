from collections.abc import Mapping
from datetime import datetime
from typing import Any

from .models import InputValue, PollResult, normalize_input_items


class Source:
    source_id: str
    description: str | None = None
    interval: float | None = None
    schedule: str | None = None
    max_retry: int = 1
    max_reinit: int = 1

    def init(self) -> None:
        return None

    def poll(self) -> PollResult:
        raise NotImplementedError

    def test_poll(self) -> PollResult:
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
) -> PollResult:
    """The values of a poll: kanary.inputs(("temperature", 21.5), ("humidity", 40.0)).

    An item may also be a poll result, such as super().poll() of a helper source;
    its inputs and metadata come first, then the other items, which replace an
    input of the same name::

        return kanary.inputs(super().poll(), ("up", 1))

    A result of kanary.no_update() or kanary.skip() has no new snapshot to add
    to, so it is returned as it is. ``timestamp=`` is for items without a time
    of their own; the inputs of a result keep theirs.
    """
    results = [item for item in items if isinstance(item, PollResult)]
    if not results:
        return PollResult(
            inputs=normalize_input_items(items, default_timestamp=timestamp),
            metadata=dict(metadata or {}),
        )
    for result in results:
        if result.status in ("no_update", "skip"):
            return result
    merged_inputs: list[InputValue] = []
    merged_metadata: dict[str, Any] = {}
    for result in results:
        merged_inputs.extend(result.inputs)
        merged_metadata.update(result.metadata)
    others = tuple(item for item in items if not isinstance(item, PollResult))
    merged_inputs.extend(normalize_input_items(others, default_timestamp=timestamp))
    merged_metadata.update(metadata or {})
    return PollResult(inputs=merged_inputs, metadata=merged_metadata)


def no_data(
    *,
    reason: str | None = None,
    metadata: Mapping[str, Any] | None = None,
) -> PollResult:
    """The poll worked and found nothing: an empty snapshot, rules are evaluated."""
    return PollResult(status="empty", reason=reason, metadata=dict(metadata or {}))


def no_update(
    *,
    reason: str | None = None,
    metadata: Mapping[str, Any] | None = None,
) -> PollResult:
    """The poll worked and nothing is new: the last snapshot is kept, rules are evaluated."""
    return PollResult(status="no_update", reason=reason, metadata=dict(metadata or {}))


def skip(
    *,
    reason: str | None = None,
    metadata: Mapping[str, Any] | None = None,
) -> PollResult:
    """Ignore this poll: the last snapshot is kept, rules are not evaluated."""
    return PollResult(status="skip", reason=reason, metadata=dict(metadata or {}))
