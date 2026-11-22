from collections.abc import Callable
from copy import deepcopy
import difflib
import inspect
from typing import Any

from .constants import ERROR, AlertState, Severity, TransitionKind, coerce_severity
from .output import Output, _DEFAULT_EXCLUDED_STATES
from .rule import Rule
from .schedule import every_seconds, parse_schedule
from .units import parse_duration
from .selectors import normalize_rule_inputs
from .source import Source

_RULES: dict[str, type[Rule]] = {}
_SOURCES: dict[str, type[Source]] = {}
_OUTPUTS: dict[str, type[Output]] = {}
_RULE_DUPLICATES: dict[str, list[str]] = {}
_SOURCE_DUPLICATES: dict[str, list[str]] = {}
_OUTPUT_DUPLICATES: dict[str, list[str]] = {}


def register_rule(cls: type[Rule]) -> type[Rule]:
    prepare_rule_class(cls)
    _annotate_definition_metadata(cls)
    rule_id = getattr(cls, "rule_id")
    if rule_id in _RULES:
        _RULE_DUPLICATES.setdefault(rule_id, [_definition_name(_RULES[rule_id])])
        _RULE_DUPLICATES[rule_id].append(_definition_name(cls))
    _RULES[rule_id] = cls
    return cls


def rule(
    cls: type[Rule] | None = None,
    **attrs: object,
) -> type[Rule] | Callable[[type[Rule]], type[Rule]]:
    if cls is not None:
        _apply_attrs(cls, attrs, Rule)
        return register_rule(cls)

    def decorator(inner_cls: type[Rule]) -> type[Rule]:
        _apply_attrs(inner_cls, attrs, Rule)
        return register_rule(inner_cls)

    return decorator


def register_source(cls: type[Source]) -> type[Source]:
    prepare_source_class(cls)
    _annotate_definition_metadata(cls)
    source_id = getattr(cls, "source_id")
    if source_id in _SOURCES:
        _SOURCE_DUPLICATES.setdefault(source_id, [_definition_name(_SOURCES[source_id])])
        _SOURCE_DUPLICATES[source_id].append(_definition_name(cls))
    _SOURCES[source_id] = cls
    return cls


def source(
    cls: type[Source] | None = None,
    **attrs: object,
) -> type[Source] | Callable[[type[Source]], type[Source]]:
    if cls is not None:
        _apply_attrs(cls, attrs, Source)
        return register_source(cls)

    def decorator(inner_cls: type[Source]) -> type[Source]:
        _apply_attrs(inner_cls, attrs, Source)
        return register_source(inner_cls)

    return decorator


def get_rule_registry() -> dict[str, type[Rule]]:
    return dict(_RULES)


def get_source_registry() -> dict[str, type[Source]]:
    return dict(_SOURCES)


def clear_registries() -> None:
    _RULES.clear()
    _SOURCES.clear()
    _OUTPUTS.clear()
    _RULE_DUPLICATES.clear()
    _SOURCE_DUPLICATES.clear()
    _OUTPUT_DUPLICATES.clear()


def replace_registries(
    *,
    rules: dict[str, type[Rule]],
    sources: dict[str, type[Source]],
    outputs: dict[str, type[Output]] | None = None,
) -> None:
    clear_registries()
    _RULES.update(rules)
    _SOURCES.update(sources)
    if outputs is not None:
        _OUTPUTS.update(outputs)


def register_output(cls: type[Output]) -> type[Output]:
    prepare_output_class(cls)
    _annotate_definition_metadata(cls)
    output_id = getattr(cls, "output_id")
    if output_id in _OUTPUTS:
        _OUTPUT_DUPLICATES.setdefault(output_id, [_definition_name(_OUTPUTS[output_id])])
        _OUTPUT_DUPLICATES[output_id].append(_definition_name(cls))
    _OUTPUTS[output_id] = cls
    return cls


def output(
    cls: type[Output] | None = None,
    **attrs: object,
) -> type[Output] | Callable[[type[Output]], type[Output]]:
    if cls is not None:
        _apply_attrs(cls, attrs, Output)
        return register_output(cls)

    def decorator(inner_cls: type[Output]) -> type[Output]:
        _apply_attrs(inner_cls, attrs, Output)
        return register_output(inner_cls)

    return decorator


def get_output_registry() -> dict[str, type[Output]]:
    return dict(_OUTPUTS)


def get_rule_duplicates() -> dict[str, list[str]]:
    return {rule_id: list(definitions) for rule_id, definitions in _RULE_DUPLICATES.items()}


def get_source_duplicates() -> dict[str, list[str]]:
    return {source_id: list(definitions) for source_id, definitions in _SOURCE_DUPLICATES.items()}


def get_output_duplicates() -> dict[str, list[str]]:
    return {output_id: list(definitions) for output_id, definitions in _OUTPUT_DUPLICATES.items()}


def _definition_name(cls: type[object]) -> str:
    return f"{cls.__module__}.{cls.__qualname__}"


# Attributes Kanary reads even when no base class declares them.
_EXTRA_KNOWN_ATTRS: dict[type[object], frozenset[str]] = {}


def _apply_attrs(cls: type[object], attrs: dict[str, object], base: type[object]) -> None:
    """Copy decorator keyword arguments onto the class.

    A decorator argument and a class attribute are equivalent; when both are
    given, the decorator wins. Conflicting values and unknown attribute names
    are recorded so that lint can warn about them.
    """
    known = _known_attribute_names(cls, base)
    conflicts: list[tuple[str, object, object]] = []
    unknown: list[tuple[str, str | None]] = []
    for attr_name, value in attrs.items():
        # A class-body None is a placeholder meant to be filled in by the decorator.
        class_value = cls.__dict__.get(attr_name)
        if class_value is not None and _values_differ(class_value, value):
            conflicts.append((attr_name, class_value, value))
        if attr_name not in known:
            matches = difflib.get_close_matches(attr_name, sorted(known), n=1)
            unknown.append((attr_name, matches[0] if matches else None))
        setattr(cls, attr_name, deepcopy(value))
    cls.__kanary_decorator_conflicts__ = conflicts
    cls.__kanary_decorator_unknown_attrs__ = unknown


def _known_attribute_names(cls: type[object], base: type[object]) -> set[str]:
    names: set[str] = set(_EXTRA_KNOWN_ATTRS.get(base, frozenset()))
    for klass in (*cls.__mro__, *base.__mro__):
        if klass is object:
            continue
        names.update(name for name in vars(klass) if not name.startswith("__"))
        try:
            names.update(inspect.get_annotations(klass))
        except Exception:
            pass
    return names


def _values_differ(left: Any, right: Any) -> bool:
    try:
        return bool(left != right)
    except Exception:
        return left is not right


def _annotate_definition_metadata(cls: type[object]) -> None:
    # None for a class without a file: typed into python -c, a REPL, or a notebook.
    try:
        cls.__kanary_definition_file__ = inspect.getsourcefile(cls)
    except (OSError, TypeError):
        cls.__kanary_definition_file__ = None


def prepare_rule_class(cls: type[Any]) -> type[Any]:
    rule_id = getattr(cls, "rule_id", None)
    if not isinstance(rule_id, str) or not rule_id:
        raise ValueError(f"rule '{cls.__name__}' must define non-empty string rule_id")

    if getattr(cls, "measurement", None) is not None:
        raise ValueError(
            f"rule '{rule_id}' measurement is no longer supported; use inputs='source_id:input_name'"
        )

    source = getattr(cls, "source", None)
    if source is not None and (not isinstance(source, str) or not source):
        raise ValueError(f"rule '{rule_id}' source must be non-empty string when set")

    try:
        inputs = normalize_rule_inputs(getattr(cls, "inputs", None), source=source)
    except ValueError as exc:
        raise ValueError(f"rule '{rule_id}' {exc}") from exc
    if not inputs:
        raise ValueError(f"rule '{rule_id}' must define source or inputs")

    severity = getattr(cls, "severity", ERROR)  # as for a subclass of kanary.Rule
    try:
        cls.severity = coerce_severity(severity)
    except (TypeError, ValueError) as exc:
        raise ValueError(f"rule '{rule_id}' {exc}") from exc

    if not hasattr(cls, "tags"):
        cls.tags = []  # allowed, as for a subclass of kanary.Rule; lint warns about it
    tags = getattr(cls, "tags")
    if isinstance(tags, str):
        tags = [tags]  # one tag written alone
    elif isinstance(tags, (tuple, set, frozenset)):
        tags = list(tags)
    if not isinstance(tags, list) or not all(isinstance(tag, str) for tag in tags):
        raise ValueError(f"rule '{rule_id}' tags must be a tag or a list of tags, not {tags!r}")
    cls.tags = tags

    evaluate = getattr(cls, "evaluate", None)
    if not callable(evaluate):
        raise ValueError(f"rule '{rule_id}' must implement evaluate(ctx)")
    _check_method("rule", rule_id, evaluate, "evaluate(self, ctx)", arity=1, removed="the payload argument")

    _setdefault(cls, "owner", None)
    _setdefault(cls, "description", None)
    _setdefault(cls, "runbook", None)
    _setdefault(cls, "depends_on", [])
    _setdefault(cls, "suppressed_by", [])
    _setdefault(cls, "matched_outputs", [])
    cls.inputs = inputs
    _setdefault(cls, "resolved_sources", [])

    for attr_name in ("depends_on", "suppressed_by"):
        value = getattr(cls, attr_name)
        if isinstance(value, str):
            value = [value]
        elif isinstance(value, tuple):
            value = list(value)
        if not isinstance(value, list) or not all(isinstance(item, str) for item in value):
            raise ValueError(f"rule '{rule_id}' {attr_name} must be a rule id or a list of rule ids")
        setattr(cls, attr_name, value)

    if isinstance(getattr(cls, "evaluation_interval", None), str):  # "30s", "5m", "1h"
        try:
            cls.evaluation_interval = parse_duration(cls.evaluation_interval)
        except ValueError as exc:
            raise ValueError(f"rule '{rule_id}' evaluation_interval: {exc}") from None
    if getattr(cls, "normalize_evaluation", None) is None:  # a class that does not subclass kanary.Rule
        cls.normalize_evaluation = Rule.normalize_evaluation
    return cls


def prepare_source_class(cls: type[Any]) -> type[Any]:
    _setdefault(cls, "description", None)
    _setdefault(cls, "max_retry", 1)
    _setdefault(cls, "max_reinit", 1)
    if "init" not in cls.__dict__ and getattr(cls, "init", None) in {None, Source.init}:
        cls.init = Source.init
    if "terminate" not in cls.__dict__ and getattr(cls, "terminate", None) in {None, Source.terminate}:
        cls.terminate = Source.terminate

    source_id = getattr(cls, "source_id", None)
    if not isinstance(source_id, str) or not source_id:
        raise ValueError(f"source '{cls.__name__}' must define non-empty string source_id")
    if not callable(getattr(cls, "poll", None)):
        raise ValueError(f"source '{source_id}' must implement poll()")

    for name in ("init", "poll", "terminate"):
        _check_method("source", source_id, getattr(cls, name), f"{name}(self)", arity=0, removed="the ctx argument")

    if isinstance(getattr(cls, "interval", None), str):  # "30s", "5m", "1h"
        try:
            cls.interval = parse_duration(cls.interval)
        except ValueError as exc:
            raise ValueError(f"source '{source_id}' interval: {exc}") from None
    schedule = getattr(cls, "schedule", None)
    try:
        every = every_seconds(schedule)
    except ValueError as exc:
        raise ValueError(f"source '{source_id}' schedule is invalid: {exc}") from None
    if every is not None:
        # schedule="@every 5m" (as in robfig/cron) is another way to write interval = 300.
        if cls.__dict__.get("interval") is not None and cls.__dict__.get("schedule") is not None:
            raise ValueError(f"source '{source_id}' must not define both interval and schedule")
        cls.interval, cls.schedule = every, None
    interval = getattr(cls, "interval", None)
    schedule = getattr(cls, "schedule", None)
    if interval is not None and schedule is not None:
        # The one written on this class (its body or decorator) wins over the other
        # one inherited from a base class, such as a helper's interval = 0.
        own = cls.__dict__
        if own.get("schedule") is not None and own.get("interval") is None:
            cls.interval = interval = None
        elif own.get("interval") is not None and own.get("schedule") is None:
            cls.schedule = schedule = None
    if interval is None and schedule is None:
        interval = 60.0
        cls.interval = interval
    if interval is not None and schedule is not None:
        raise ValueError(f"source '{source_id}' must not define both interval and schedule")
    if interval is not None:
        # 0: call poll() again as soon as it returns, for a poll() that waits for data itself.
        if isinstance(interval, bool) or not isinstance(interval, (int, float)) or not interval >= 0:
            raise ValueError(f"source '{source_id}' interval must be a number >= 0")
        cls._kanary_compiled_schedule = None
    else:
        if not isinstance(schedule, str) or not schedule.strip():
            raise ValueError(f"source '{source_id}' schedule must be a non-empty cron-like string")
        try:
            cls._kanary_compiled_schedule = parse_schedule(schedule)
        except ValueError as exc:
            raise ValueError(f"source '{source_id}' schedule is invalid: {exc}") from exc
    for attr_name in ("max_retry", "max_reinit"):
        value = getattr(cls, attr_name, None)
        if not isinstance(value, int) or value < 0:
            raise ValueError(f"source '{source_id}' {attr_name} must be a non-negative integer")
    return cls


def prepare_output_class(cls: type[Any]) -> type[Any]:
    _setdefault(cls, "description", None)
    _setdefault(cls, "include_tags", [])
    _setdefault(cls, "exclude_tags", [])
    _setdefault(cls, "exclude_states", list(_DEFAULT_EXCLUDED_STATES))
    _setdefault(cls, "exclude_transitions", [])
    _setdefault(cls, "minimum_severity", None)
    _setdefault(cls, "max_retry", 1)
    _setdefault(cls, "max_reinit", 1)
    if "init" not in cls.__dict__ and getattr(cls, "init", None) in {None, Output.init}:
        cls.init = Output.init
    if "terminate" not in cls.__dict__ and getattr(cls, "terminate", None) in {None, Output.terminate}:
        cls.terminate = Output.terminate
    if "matches" not in cls.__dict__ and getattr(cls, "matches", None) in {None, Output.matches}:
        cls.matches = Output.matches

    output_id = getattr(cls, "output_id", None)
    if not isinstance(output_id, str) or not output_id:
        raise ValueError(f"output '{cls.__name__}' must define non-empty string output_id")
    try:
        _minimum_severity(getattr(cls, "minimum_severity", None))
    except Exception as exc:
        raise ValueError(f"output '{output_id}' has invalid minimum_severity: {exc}") from exc
    # One name may be written as a string; state and transition names in any case.
    for attr_name in ("include_tags", "exclude_tags"):
        setattr(cls, attr_name, _name_list(output_id, attr_name, getattr(cls, attr_name)))
    cls.exclude_states = _choice_list(output_id, "exclude_states", cls.exclude_states, AlertState)
    cls.exclude_transitions = _choice_list(output_id, "exclude_transitions", cls.exclude_transitions, TransitionKind)
    for attr_name in ("max_retry", "max_reinit"):
        value = getattr(cls, attr_name, None)
        if not isinstance(value, int) or value < 0:
            raise ValueError(f"output '{output_id}' {attr_name} must be a non-negative integer")
    if not callable(getattr(cls, "emit", None)):
        raise ValueError(f"output '{output_id}' must implement emit(event)")
    _check_method("output", output_id, getattr(cls, "init"), "init(self)", arity=0, removed="the ctx argument")
    _check_method("output", output_id, getattr(cls, "emit"), "emit(self, event)", arity=1, removed="the ctx argument")
    _check_method("output", output_id, getattr(cls, "terminate"), "terminate(self)", arity=0, removed="the ctx argument")
    return cls


_POSITIONAL_KINDS = {inspect.Parameter.POSITIONAL_ONLY, inspect.Parameter.POSITIONAL_OR_KEYWORD}


def _check_method(kind: str, plugin_id: str, method: Any, signature: str, *, arity: int, removed: str) -> None:
    """The method takes exactly `arity` positional arguments after self: poll(self), emit(self, event), ..."""
    parameters = list(inspect.signature(method).parameters.values())
    if parameters and parameters[0].name == "self":
        parameters = parameters[1:]
    if len(parameters) == arity and all(parameter.kind in _POSITIONAL_KINDS for parameter in parameters):
        return
    hint = f" ({removed} was removed in KANARY 0.8.0)" if len(parameters) == arity + 1 else ""
    raise ValueError(f"{kind} '{plugin_id}' {signature.split('(')[0]}() must be {signature}{hint}")


def _setdefault(cls: type[Any], attr_name: str, value: Any) -> None:
    if hasattr(cls, attr_name):
        return
    setattr(cls, attr_name, value)


def _name_list(plugin_id: str, attr_name: str, value: Any) -> list[str]:
    """A list of names: one name may be written as a string ("infra" is ["infra"])."""
    if value is None:
        return []
    if isinstance(value, str):
        return [value]
    if isinstance(value, (list, tuple, set, frozenset)) and all(isinstance(item, str) for item in value):
        return list(value)
    raise ValueError(f"output '{plugin_id}' {attr_name} must be a name or a list of names, not {value!r}")


def _choice_list(plugin_id: str, attr_name: str, value: Any, choices: type[Any]) -> list[str]:
    """State or transition names, upper case; an unknown name is an error (it would never match)."""
    names = [name.strip().upper() for name in _name_list(plugin_id, attr_name, value)]
    known = [choice.value for choice in choices]
    unknown = [name for name in names if name not in known]
    if unknown:
        raise ValueError(f"output '{plugin_id}' {attr_name} has unknown names {unknown}: use {', '.join(known)}")
    return names


def _minimum_severity(value: Severity | str | None) -> Severity | None:
    return None if value is None else coerce_severity(value)
