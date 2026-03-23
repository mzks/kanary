from __future__ import annotations

from fnmatch import fnmatch
from typing import Iterable


def has_glob(pattern: str) -> bool:
    return any(char in pattern for char in "*?[")


def matches_text_filter(values: Iterable[object], pattern: str) -> bool:
    normalized_pattern = str(pattern or "").strip().lower()
    if not normalized_pattern:
        return True

    candidates = [str(value or "").lower() for value in values]
    if has_glob(normalized_pattern):
        return any(fnmatch(candidate, normalized_pattern) for candidate in candidates)
    return any(normalized_pattern in candidate for candidate in candidates)


def matches_any_tag(tags: Iterable[str], patterns: Iterable[str]) -> bool:
    tag_list = [str(tag) for tag in tags]
    pattern_list = [str(pattern) for pattern in patterns]
    if not pattern_list:
        return True
    return any(fnmatch(tag, pattern) for tag in tag_list for pattern in pattern_list)


def matches_excluded_tag(tags: Iterable[str], patterns: Iterable[str]) -> bool:
    tag_list = [str(tag) for tag in tags]
    pattern_list = [str(pattern) for pattern in patterns]
    return any(fnmatch(tag, pattern) for tag in tag_list for pattern in pattern_list)
