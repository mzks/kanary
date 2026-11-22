"""Input selectors: "source:input" names and glob matching shared by rules, engine, and lint."""

from collections.abc import Collection, Iterable
from fnmatch import fnmatch, fnmatchcase
from typing import Any


def selector_label(selector: str | None) -> str | None:
    if selector is None:
        return None
    if ":" in selector and "*" not in selector:
        return selector.split(":", 1)[1]
    return selector


def split_input_selector(selector: str) -> tuple[str | None, str]:
    if ":" not in selector:
        return None, selector
    source_pattern, input_pattern = selector.split(":", 1)
    return source_pattern or None, input_pattern or "*"


def qualify_input_name(source_id: str, input_name: str) -> str:
    return f"{source_id}:{input_name}"


def normalize_runtime_selectors(
    selector: str | Iterable[str] | None,
    declared_inputs: tuple[str, ...],
) -> list[str]:
    if selector is None:
        return list(declared_inputs)
    if isinstance(selector, str):
        return [selector]
    return [str(item) for item in selector]


def is_pattern(text: str) -> bool:
    return any(mark in text for mark in "*?[")


def select_inputs(source_id: str, input_names: Collection[str], selectors: list[str]) -> list[str]:
    """The input names of one source that any selector picks, sorted.

    An exact input name is looked up directly; only glob patterns are matched
    against every input, so rules with plain names stay cheap on large sources.
    """
    picked: set[str] = set()
    for selector in selectors:
        source_pattern, input_pattern = split_input_selector(selector)
        if source_pattern is not None and not fnmatchcase(source_id, source_pattern):
            continue
        if is_pattern(input_pattern):
            picked.update(name for name in input_names if fnmatchcase(name, input_pattern))
        elif input_pattern in input_names:
            picked.add(input_pattern)
    return sorted(picked)


def normalize_rule_inputs(inputs: Any, *, source: str | None = None) -> list[str]:
    if inputs is None or inputs == []:
        if isinstance(source, str) and source:
            return [f"{source}:*"]
        return []
    if isinstance(inputs, str):
        return [inputs]
    if isinstance(inputs, (list, tuple)) and all(isinstance(item, str) for item in inputs):
        return list(inputs)
    raise ValueError("inputs must be a selector or a list of selectors, such as 'daq:hv*' or ['a:v', 'b:v']")


def resolve_rule_sources(inputs: Iterable[str], available_source_ids: Iterable[str]) -> list[str]:
    resolved: set[str] = set()
    source_ids = list(available_source_ids)
    for selector in inputs:
        source_pattern, _ = split_input_selector(selector)
        for source_id in source_ids:
            if source_pattern is None or fnmatch(source_id, source_pattern):
                resolved.add(source_id)
    return sorted(resolved)
