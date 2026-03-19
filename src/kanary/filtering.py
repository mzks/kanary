from __future__ import annotations

from fnmatch import fnmatch


def apply_excludes(snapshot, patterns: list[str] | None):
    patterns = patterns or []
    if not patterns:
        return snapshot

    filtered_sources = {
        source_id: source_cls
        for source_id, source_cls in snapshot.sources.items()
        if not _is_excluded(source_id, patterns)
    }
    filtered_outputs = {
        output_id: output_cls
        for output_id, output_cls in snapshot.outputs.items()
        if not _is_excluded(output_id, patterns)
    }
    filtered_rules = {
        rule_id: rule_cls
        for rule_id, rule_cls in snapshot.rules.items()
        if not _is_excluded(rule_id, patterns) and getattr(rule_cls, "source", None) in filtered_sources
    }
    return type(snapshot)(
        sources=filtered_sources,
        rules=filtered_rules,
        outputs=filtered_outputs,
    )


def _is_excluded(plugin_id: str, patterns: list[str]) -> bool:
    return any(fnmatch(plugin_id, pattern) for pattern in patterns)
