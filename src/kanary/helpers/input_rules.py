"""Rules over several inputs: DifferenceRule, KOfNRule, AggregateRule, CountRule.

Each reads the inputs selected by the rule's ``inputs`` (usually a glob such
as "hv:ch*.current") and ignores values that are not numbers.
"""

import statistics
from typing import Any

from ..models import Evaluation
from ..rule import InputView, Rule, RuleContext, firing, ok
from .value_rules import RangeRule


def _numeric(items: list[InputView]) -> list[InputView]:
    return [item for item in items if isinstance(item.value, (int, float)) and not isinstance(item.value, bool)]


class DifferenceRule(Rule):
    """Fire when the spread (highest - lowest) of the inputs exceeds max_difference.

    Typical use: redundant sensors that should agree.
    """

    max_difference: float | None = None
    min_inputs: int = 2

    def evaluate(self, ctx: RuleContext) -> Evaluation:
        if not isinstance(self.max_difference, (int, float)) or self.max_difference < 0:
            raise ValueError("DifferenceRule.max_difference must be a non-negative number")
        if not isinstance(self.min_inputs, int) or self.min_inputs < 2:
            raise ValueError("DifferenceRule.min_inputs must be an integer of at least 2")
        items = _numeric(ctx.inputs())
        if len(items) < self.min_inputs:
            return ok(f"needs {self.min_inputs} inputs with values; have {len(items)}")
        highest = max(items, key=lambda item: item.value)
        lowest = min(items, key=lambda item: item.value)
        spread = highest.value - lowest.value
        extra = {"difference": spread, "highest": highest.name, "lowest": lowest.name}
        detail = f"{highest.name}={highest.value} vs {lowest.name}={lowest.value}"
        if spread > self.max_difference:
            return firing(f"difference {spread:g} exceeds {self.max_difference:g} ({detail})", extra=extra)
        return ok(f"difference {spread:g} within {self.max_difference:g} ({detail})", extra=extra)


class KOfNRule(RangeRule):
    """Fire when at least ``k`` inputs break the range (low / high / fire_when as in RangeRule).

    With count_missing, an input that is present but has no value also counts.
    """

    k: int = 1
    count_missing: bool = False

    def evaluate(self, ctx: RuleContext) -> Evaluation:
        self._check_settings()
        if not isinstance(self.k, int) or self.k < 1:
            raise ValueError("KOfNRule.k must be a positive integer")
        items = ctx.inputs()
        bad = [item.name for item in _numeric(items) if self.breaches(item.value)]
        missing = [item.name for item in items if item.value is None]
        if self.count_missing:
            bad += missing
        extra: dict[str, Any] = {"bad_inputs": bad, "missing_inputs": missing, "total_inputs": len(items)}
        summary = f"{len(bad)} of {len(items)} inputs {self.phrase(True)}"
        if len(bad) >= self.k:
            return firing(f"{summary} (k={self.k}): {', '.join(bad)}", extra=extra)
        return ok(f"{summary} (k={self.k})", extra=extra)


class AggregateRule(RangeRule):
    """Apply RangeRule's check to one aggregate of the inputs.

    aggregate is "sum", "mean", "median", "min", or "max". Hysteresis works as
    in RangeRule.
    """

    aggregate: str = "mean"

    _FUNCTIONS = {
        "sum": sum,
        "mean": statistics.fmean,
        "median": statistics.median,
        "min": min,
        "max": max,
    }

    def compute(self, values: list[float]) -> float | None:
        function = self._FUNCTIONS.get(self.aggregate)
        if function is None:
            raise ValueError(f"AggregateRule.aggregate must be one of {', '.join(self._FUNCTIONS)}, not {self.aggregate!r}")
        return function(values) if values else None

    def evaluate(self, ctx: RuleContext) -> Evaluation:
        self._check_settings()
        values = [item.value for item in _numeric(ctx.inputs())]
        value = self.compute(values)
        if value is None:
            return ok("no input values to aggregate")
        label = f"{self.aggregate} of {len(values)} inputs"
        extra = {"aggregate": value, "count": len(values)}
        fired = self.check(value, ctx)  # RangeRule's check, hysteresis included
        message = f"{label}={value:g} {self.phrase(fired)}"
        return firing(message, extra=extra) if fired else ok(message, extra=extra)


class CountRule(Rule):
    """Fire when the number of inputs that have a value is outside [min_count, max_count].

    Typical use: "64 channels should report; alert when fewer do".
    """

    min_count: int | None = None
    max_count: int | None = None

    def evaluate(self, ctx: RuleContext) -> Evaluation:
        if self.min_count is None and self.max_count is None:
            raise ValueError("CountRule needs min_count, max_count, or both")
        items = ctx.inputs()
        present = [item.name for item in items if item.value is not None]
        missing = [item.name for item in items if item.value is None]
        count = len(present)
        extra = {"count": count, "missing_inputs": missing}
        low = "-" if self.min_count is None else self.min_count
        high = "-" if self.max_count is None else self.max_count
        expected = f"expected [{low}, {high}]"
        if self.min_count is not None and count < self.min_count:
            return firing(f"only {count} inputs report a value, {expected}", extra=extra)
        if self.max_count is not None and count > self.max_count:
            return firing(f"{count} inputs report a value, {expected}", extra=extra)
        return ok(f"{count} inputs report a value, {expected}", extra=extra)
