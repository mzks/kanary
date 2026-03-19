from collections.abc import Callable
from copy import deepcopy
import inspect

from .output import Output, prepare_output_class
from .rule import Rule, prepare_rule_class
from .source import Source, prepare_source_class

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
        _apply_attrs(cls, attrs)
        return register_rule(cls)

    def decorator(inner_cls: type[Rule]) -> type[Rule]:
        _apply_attrs(inner_cls, attrs)
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
        _apply_attrs(cls, attrs)
        return register_source(cls)

    def decorator(inner_cls: type[Source]) -> type[Source]:
        _apply_attrs(inner_cls, attrs)
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
        _apply_attrs(cls, attrs)
        return register_output(cls)

    def decorator(inner_cls: type[Output]) -> type[Output]:
        _apply_attrs(inner_cls, attrs)
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


def _apply_attrs(cls: type[object], attrs: dict[str, object]) -> None:
    for attr_name, value in attrs.items():
        setattr(cls, attr_name, deepcopy(value))


def _annotate_definition_metadata(cls: type[object]) -> None:
    cls.__kanary_definition_file__ = inspect.getsourcefile(cls)
