from __future__ import annotations

from fnmatch import fnmatch
from typing import Iterable


def _names(values: Iterable[str] | str) -> list[str]:
    """One name written as a string is that name, not its characters."""
    return [values] if isinstance(values, str) else [str(value) for value in values]


def matches_any_tag(tags: Iterable[str], patterns: Iterable[str]) -> bool:
    tag_list = _names(tags)
    pattern_list = _names(patterns)
    if not pattern_list:
        return True
    return any(fnmatch(tag, pattern) for tag in tag_list for pattern in pattern_list)


def matches_excluded_tag(tags: Iterable[str], patterns: Iterable[str]) -> bool:
    tag_list = _names(tags)
    pattern_list = _names(patterns)
    return any(fnmatch(tag, pattern) for tag in tag_list for pattern in pattern_list)
