"""Ready-made rules that judge each input on its own: ValueRule and its subclasses."""

from collections.abc import Mapping
from difflib import get_close_matches
from typing import Any

from ..constants import Severity, coerce_severity
from ..models import Evaluation
from ..rule import InputView, Rule, RuleContext, firing, ok
from ..selectors import selector_label
from ..units import best_per_time_unit, format_per_time, format_time
from ..values import coerce_datetime, is_number


class ValueRule(Rule):
    """Judge each selected input on its own; the most severe result wins.

    A subclass overrides three small methods:

    - value_of(item, ctx): the number to judge (default: the input's value);
      None when it cannot be judged, which lists the input in missing_inputs.
    - check(value, ctx): True or a Severity to fire, False or None when normal.
    - phrase(fired): the end of the message, e.g. "out of range [0, 100]".

    Messages read "<input>=<value> <phrase>". The payload gets ``values``
    (input name to judged value), ``matched_inputs``, and ``missing_inputs``.
    """

    # True: an input without a value to judge (or no input at all) fires too, for
    # example a probe that got no answer. StaleRule sets it.
    missing_fires = False

    def evaluate(self, ctx: RuleContext) -> Evaluation:
        self._check_settings()
        items = ctx.inputs()
        if not items:
            message = _no_input_message(ctx, self.primary_input_selector())
            return firing(message) if self.missing_fires else ok(message)
        judged = {item.name: self.value_of(item, ctx) for item in items}
        errors = {item.name: item.metadata.get("error") for item in items}
        values = {name: value for name, value in judged.items() if value is not None}
        missing = [name for name, value in judged.items() if value is None]
        found: dict[str, Severity] = {}
        for name, value in values.items():
            result = self.check(value, ctx)
            if result:
                found[name] = self.severity if result is True else coerce_severity(result)
        extra = {
            "values": values,
            "matched_inputs": [{"name": n, "value": values[n], "severity": s.name} for n, s in found.items()],
            "missing_inputs": missing,
        }
        if found:
            parts = [self._pair(name, values[name]) for name in found]
            message = f"{', '.join(parts)} {self.phrase(True)}"
            if self.missing_fires and missing:
                message += "; " + _no_value_text(missing, errors)
            return firing(message, severity=max(found.values()), extra=extra)
        if missing:
            message = _no_value_text(missing, errors)
            return firing(message, extra=extra) if self.missing_fires else ok(message, extra=extra)
        if len(values) == 1:
            name, value = next(iter(values.items()))
            return ok(f"{self._pair(name, value)} {self.phrase(False)}", extra=extra)
        return ok(f"all {len(values)} inputs {self.phrase(False)}", extra=extra)

    def value_of(self, item: InputView, ctx: RuleContext) -> float | None:
        return item.value if is_number(item.value) else None

    def check(self, value: float, ctx: RuleContext) -> Severity | bool | None:
        raise NotImplementedError

    def phrase(self, fired: bool) -> str:
        return "fires" if fired else "ok"

    def format_value(self, value: float) -> str:
        return f"{value:g}"

    def previous_value(self, item: InputView, ctx: RuleContext) -> float | None:
        """The value this rule judged for the input last time (from its previous alert)."""
        if ctx.previous_alert is None:
            return None
        return (ctx.previous_alert.payload.get("values") or {}).get(item.name)

    def _check_settings(self) -> None:
        """Raise ValueError for an invalid setting; called at the start of evaluate()."""

    def _pair(self, name: str, value: float) -> str:
        return f"{name}={self.format_value(value)}"


class ExpectedValueRule(ValueRule):
    """Fire when an input's value is not ``expected`` (or, with fire_when = "equal", when it is).

    expected is one value or a list of values. Numbers compare by value
    (1 == 1.0 == True), so it fits status codes, 0/1 flags, and text states.
    """

    expected: Any = None
    # "different": any other value fires (default). "equal": the expected value fires.
    fire_when: str = "different"

    def value_of(self, item: InputView, ctx: RuleContext) -> Any:
        return item.value

    def check(self, value: Any, ctx: RuleContext) -> bool:
        listed = value in self.expected_values()
        return not listed if self.fire_when == "different" else listed

    def expected_values(self) -> list[Any]:
        if isinstance(self.expected, (list, tuple, set, frozenset)):
            return list(self.expected)
        return [self.expected]

    def phrase(self, fired: bool) -> str:
        wanted = " or ".join(self.format_value(value) for value in self.expected_values())
        if self.fire_when == "different":
            return f"{'is not' if fired else 'is'} the expected {wanted}"
        return f"{'is' if fired else 'is not'} {wanted}"

    def format_value(self, value: Any) -> str:
        return f"{value:g}" if is_number(value) else repr(value)

    def _check_settings(self) -> None:
        if self.fire_when not in {"different", "equal"}:
            raise ValueError(f"fire_when must be 'different' or 'equal', not {self.fire_when!r}")
        if self.expected is None:
            raise ValueError("ExpectedValueRule.expected must be set")


class StaleRule(ValueRule):
    """Fire when an input's timestamp is older than ``timeout`` seconds, or missing."""

    timeout: float
    missing_fires = True

    def value_of(self, item: InputView, ctx: RuleContext) -> float | None:
        if item.timestamp is None:
            return None
        return (ctx.now - coerce_datetime(item.timestamp)).total_seconds()

    def check(self, value: float, ctx: RuleContext) -> bool:
        return value > self.timeout

    def phrase(self, fired: bool) -> str:
        return f"(timeout {format_time(self.timeout)})"

    def format_value(self, value: float) -> str:
        return f"{format_time(value)} old"

    def _check_settings(self) -> None:
        if not is_number(getattr(self, "timeout", None)) or self.timeout <= 0:
            raise ValueError("StaleRule.timeout must be a positive number of seconds")


class RangeRule(ValueRule):
    """Fire when an input leaves [low, high] (or, with fire_when = "inside", enters it).

    While the rule is alerting, the normal range is narrowed by ``hysteresis``
    (the forbidden band widened), so the alert clears only once the value is
    clearly back.
    """

    low: float | None = None
    high: float | None = None
    hysteresis: float = 0.0
    lower_inclusive: bool = True
    upper_inclusive: bool = True
    # "outside": [low, high] is normal and leaving it fires (default).
    # "inside":  [low, high] is forbidden and entering it fires.
    fire_when: str = "outside"

    def check(self, value: float, ctx: RuleContext) -> bool:
        return self.breaches(value, self.hysteresis if ctx.was_alerting() else 0.0)

    def breaches(self, value: float, margin: float = 0.0) -> bool:
        """True when value fires the rule; margin is the hysteresis in effect."""
        if self.fire_when == "inside":
            return self._in_band(value, margin)
        return not self._in_band(value, -margin)

    def phrase(self, fired: bool) -> str:
        if self.fire_when == "inside":
            return f"{'inside' if fired else 'outside'} forbidden range {self.range_text()}"
        return f"{'out of' if fired else 'within'} range {self.range_text()}"

    def range_text(self) -> str:
        left = "[" if self.lower_inclusive else "("
        right = "]" if self.upper_inclusive else ")"
        low = "-inf" if self.low is None else self.format_bound(self.low)
        high = "inf" if self.high is None else self.format_bound(self.high)
        return f"{left}{low}, {high}{right}"

    def format_bound(self, bound: float) -> str:
        return str(bound)

    def _in_band(self, value: float, margin: float = 0.0) -> bool:
        """value in [low - margin, high + margin], honouring the inclusive flags."""
        if self.low is not None:
            low = self.low - margin
            if value < low or (value == low and not self.lower_inclusive):
                return False
        if self.high is not None:
            high = self.high + margin
            if value > high or (value == high and not self.upper_inclusive):
                return False
        return True

    def _check_settings(self) -> None:
        if self.fire_when not in {"outside", "inside"}:
            raise ValueError(f"fire_when must be 'outside' or 'inside', not {self.fire_when!r}")
        if not is_number(self.hysteresis) or self.hysteresis < 0:
            raise ValueError("hysteresis must be a non-negative number")
        if (
            self.fire_when == "outside"
            and self.low is not None
            and self.high is not None
            and self.low + self.hysteresis > self.high - self.hysteresis
        ):
            raise ValueError("hysteresis must be at most half of high - low, or the alert can never clear")


class ThresholdRule(ValueRule):
    """Fire with the severity of the highest threshold an input reaches.

    direction = "high" fires at value >= threshold, "low" at value <= threshold.
    While the rule is alerting, thresholds up to the current severity are moved
    back by ``hysteresis``, so severity drops only once the value is clearly
    back; reaching a higher level is never made easier.
    """

    thresholds: list[tuple[float, Severity]] = []
    direction: str = "high"
    hysteresis: float = 0.0

    def check(self, value: float, ctx: RuleContext) -> Severity | None:
        level = self.level(value)
        if self.hysteresis <= 0 or not ctx.was_alerting() or ctx.previous_severity is None:
            return level
        held = self.level(value, self.hysteresis)
        if held is None:
            return level
        held = min(held, ctx.previous_severity)
        return held if level is None else max(level, held)

    def level(self, value: float, margin: float = 0.0) -> Severity | None:
        """The highest severity whose threshold (moved back by margin) value reaches."""
        if self.direction == "high":
            reached = [severity for threshold, severity in self.thresholds if value >= threshold - margin]
        else:
            reached = [severity for threshold, severity in self.thresholds if value <= threshold + margin]
        return max(reached, default=None)

    def phrase(self, fired: bool) -> str:
        ordered = sorted(self.thresholds, key=lambda item: item[0], reverse=self.direction == "low")
        joined = ", ".join(f"{value:g}->{severity.name}" for value, severity in ordered)
        side = "below" if self.direction == "high" else "above"
        return f"{'reached' if fired else side} {self.direction} thresholds [{joined}]"

    def _check_settings(self) -> None:
        if self.direction not in {"high", "low"}:
            raise ValueError(f"direction must be 'high' or 'low', not {self.direction!r}")


class RateRule(RangeRule):
    """RangeRule on the rate of change between the previous and the current sample.

    The rate is (value - previous value) / seconds between their timestamps:
    value units per second. Write low and high with the time units, for
    example ``high = 1.0 / kanary.minute`` for "1 per minute"; messages show
    the rate in the unit of the larger bound.

    "Previous" is the source's previous poll. When a poll returns the same
    sample again (the timestamp did not advance), the rate judged last time is
    kept. A source can instead return kanary.no_update() when nothing is new.
    """

    def value_of(self, item: InputView, ctx: RuleContext) -> float | None:
        previous = next((p for p in ctx.inputs(previous=True) if p.name == item.name), None)
        if previous is None or not is_number(item.value) or not is_number(previous.value):
            return None
        if item.timestamp is None or previous.timestamp is None:
            return None
        seconds = (coerce_datetime(item.timestamp) - coerce_datetime(previous.timestamp)).total_seconds()
        if seconds == 0:
            return self.previous_value(item, ctx)  # the same sample: its rate is the one from last time
        if seconds < 0:
            return None
        return (item.value - previous.value) / seconds

    def format_value(self, value: float) -> str:
        return format_per_time(value, unit=self._unit())

    def format_bound(self, bound: float) -> str:
        return format_per_time(bound, unit=self._unit())

    def _unit(self) -> tuple[float, str]:
        bounds = [abs(bound) for bound in (self.low, self.high) if bound]
        return best_per_time_unit(max(bounds, default=1.0))


def _no_value_text(names: list[str], errors: Mapping[str, Any]) -> str:
    parts = [f"{name} ({errors[name]})" if errors.get(name) else name for name in names]
    return f"no value: {', '.join(parts)}"


def _no_input_message(ctx: RuleContext, selector: str | None) -> str:
    """Explain an empty selection, with the closest input name when there is one."""
    if selector is None:
        return f"no input matches {', '.join(ctx.declared_inputs) or 'the rule inputs'}"
    message = f"input '{selector_label(selector)}' is missing"
    available = ctx.names("*:*")
    closest = get_close_matches(selector, available, n=1)
    if closest:
        message += f"; closest available input: {closest[0]}"
    if available:
        shown = ", ".join(sorted(available)[:5])
        if len(available) > 5:
            shown += ", ..."
        message += f"; available inputs: {shown}"
    return message


# Return values for evaluate(): kanary.ok(...), kanary.warn_if(cond, ...), ...
