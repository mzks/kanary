from __future__ import annotations

from fnmatch import fnmatch
from typing import Iterable


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
