import ast
from dataclasses import dataclass, field
from fnmatch import fnmatch
from pathlib import Path
from typing import Any

from .constants import AlertState, Severity
from .output import Output, prepare_output_class
from .patterns import matches_any_tag, matches_excluded_tag
from .rule import Rule, normalize_rule_inputs, prepare_rule_class, resolve_rule_sources
from .source import Source, prepare_source_class


@dataclass(slots=True)
class ValidationReport:
    errors: list[str] = field(default_factory=list)
    warnings: list[str] = field(default_factory=list)

    def extend(self, other: "ValidationReport") -> None:
        self.errors.extend(other.errors)
        self.warnings.extend(other.warnings)

    @property
    def ok(self) -> bool:
        return not self.errors


def validate_rule_class(rule_id: str, rule_cls: type[Any]) -> ValidationReport:
    report = ValidationReport()
    try:
        prepare_rule_class(rule_cls)
    except Exception as exc:
        report.errors.append(str(exc))
        return report

    tags = getattr(rule_cls, "tags", None)
    if tags == []:
        report.warnings.append(f"rule '{rule_id}' has no tags")
    return report


def validate_source_class(source_id: str, source_cls: type[Any]) -> ValidationReport:
    report = ValidationReport()
    try:
        prepare_source_class(source_cls)
    except Exception as exc:
        report.errors.append(str(exc))
    return report


def validate_output_class(output_id: str, output_cls: type[Any]) -> ValidationReport:
    report = ValidationReport()
    try:
        prepare_output_class(output_cls)
    except Exception as exc:
        report.errors.append(str(exc))
    return report


def validate_registries(
    *,
    sources: dict[str, type[Source]],
    rules: dict[str, type[Rule] | type[Any]],
    outputs: dict[str, type[Output]],
    duplicate_rule_ids: dict[str, list[str]] | None = None,
    duplicate_source_ids: dict[str, list[str]] | None = None,
    duplicate_output_ids: dict[str, list[str]] | None = None,
) -> ValidationReport:
    report = ValidationReport()

    for source_id, source_cls in sources.items():
        report.extend(validate_source_class(source_id, source_cls))

    for output_id, output_cls in outputs.items():
        report.extend(validate_output_class(output_id, output_cls))

    for rule_id, rule_cls in rules.items():
        report.extend(validate_rule_class(rule_id, rule_cls))
        inputs = normalize_rule_inputs(
            getattr(rule_cls, "inputs", None),
            source=getattr(rule_cls, "source", None),
        )
        rule_cls.inputs = inputs
        rule_cls.resolved_sources = resolve_rule_sources(inputs, sources.keys())
        report.extend(_validate_rule_inputs(rule_id, inputs, sources.keys()))
        if not rule_cls.resolved_sources:
            report.warnings.append(f"rule '{rule_id}' currently resolves zero sources")

        severity = getattr(rule_cls, "severity", None)
        if severity is not None and not isinstance(severity, Severity):
            report.errors.append(f"rule '{rule_id}' severity must be one of kanary.INFO/WARN/ERROR/CRITICAL")

        report.extend(_validate_rule_settings(rule_id, rule_cls))

        matched_outputs = _matching_outputs(rule_cls, outputs)
        rule_cls.matched_outputs = matched_outputs
        if not matched_outputs:
            report.warnings.append(f"rule '{rule_id}' has no matching output")

    for rule_id, definitions in sorted((duplicate_rule_ids or {}).items()):
        report.errors.append(
            f"duplicate rule_id '{rule_id}' defined by {', '.join(definitions)}"
        )

    for source_id, definitions in sorted((duplicate_source_ids or {}).items()):
        report.errors.append(
            f"duplicate source_id '{source_id}' defined by {', '.join(definitions)}"
        )

    for output_id, definitions in sorted((duplicate_output_ids or {}).items()):
        report.errors.append(
            f"duplicate output_id '{output_id}' defined by {', '.join(definitions)}"
        )

    report.extend(_validate_plugin_id_uniqueness(sources, rules, outputs))

    return report


def _matching_outputs(rule_cls: type[Any], outputs: dict[str, type[Output]]) -> list[str]:
    rule_tags = set(getattr(rule_cls, "tags", []))
    matched: list[str] = []
    possible_states = {state.value for state in AlertState}
    for output_id, output_cls in outputs.items():
        include_tags = list(getattr(output_cls, "include_tags", []))
        exclude_tags = list(getattr(output_cls, "exclude_tags", []))
        exclude_states = set(getattr(output_cls, "exclude_states", []))

        if include_tags and not matches_any_tag(rule_tags, include_tags):
            continue
        if exclude_tags and matches_excluded_tag(rule_tags, exclude_tags):
            continue

        allowed_states = set(possible_states)
        if exclude_states:
            allowed_states -= exclude_states
        if not allowed_states:
            continue

        matched.append(output_id)
    return matched


def _validate_plugin_id_uniqueness(
    sources: dict[str, type[Source]],
    rules: dict[str, type[Rule] | type[Any]],
    outputs: dict[str, type[Output]],
) -> ValidationReport:
    report = ValidationReport()
    owners: dict[str, list[str]] = {}
    for source_id in sources:
        owners.setdefault(source_id, []).append("source")
    for rule_id in rules:
        owners.setdefault(rule_id, []).append("rule")
    for output_id in outputs:
        owners.setdefault(output_id, []).append("output")

    for plugin_id, kinds in sorted(owners.items()):
        if len(kinds) > 1:
            report.errors.append(
                f"plugin id '{plugin_id}' must be unique across rule/source/output (used by {', '.join(kinds)})"
            )
    return report


def _validate_rule_settings(
    rule_id: str,
    rule_cls: type[Any],
) -> ValidationReport:
    report = ValidationReport()
    timeout = getattr(rule_cls, "timeout", None)
    if timeout is not None and not isinstance(timeout, (int, float)):
        report.errors.append(f"rule '{rule_id}' timeout must be a positive number")
        return report
    if isinstance(timeout, (int, float)) and timeout <= 0:
        report.errors.append(f"rule '{rule_id}' timeout must be a positive number")
    return report


def _validate_rule_inputs(
    rule_id: str,
    inputs: list[str],
    available_source_ids: Any,
) -> ValidationReport:
    report = ValidationReport()
    seen: set[str] = set()
    source_ids = list(available_source_ids)
    for selector in inputs:
        if selector in seen:
            report.warnings.append(f"rule '{rule_id}' declares duplicate input selector '{selector}'")
            continue
        seen.add(selector)

        if selector in {"*", "*:*"}:
            report.warnings.append(
                f"rule '{rule_id}' uses very broad input selector '{selector}'; prefer narrower source:input patterns"
            )
            continue

        if ":" not in selector:
            report.warnings.append(
                f"rule '{rule_id}' input selector '{selector}' omits source pattern; prefer fully-qualified 'source:input'"
            )
            continue

        source_pattern, input_pattern = selector.split(":", 1)
        if source_pattern == "*":
            report.warnings.append(
                f"rule '{rule_id}' input selector '{selector}' matches every source; prefer a narrower source pattern"
            )
        if input_pattern == "*":
            matched_sources = [source_id for source_id in source_ids if fnmatch(source_id, source_pattern or "*")]
            if len(matched_sources) > 1 or source_pattern == "*":
                report.warnings.append(
                    f"rule '{rule_id}' input selector '{selector}' matches all inputs from multiple sources"
                )

    return report


def validate_plugin_files(paths: list[Path]) -> ValidationReport:
    report = ValidationReport()
    for path in paths:
        report.extend(_validate_plugin_file(path))
    return report


def _validate_plugin_file(path: Path) -> ValidationReport:
    report = ValidationReport()
    try:
        tree = ast.parse(path.read_text(encoding="utf-8"), filename=str(path))
    except (OSError, SyntaxError):
        return report

    for node in ast.walk(tree):
        if not isinstance(node, ast.Call):
            continue
        if not isinstance(node.func, ast.Name) or node.func.id != "open":
            continue
        if not node.args:
            continue
        first_arg = node.args[0]
        if not isinstance(first_arg, ast.Constant) or not isinstance(first_arg.value, str):
            continue
        file_name = first_arg.value
        if _is_absolute_or_explicit_relative(file_name):
            continue
        report.warnings.append(
            f"file '{path.name}' uses open('{file_name}') with a cwd-relative path; prefer Path(__file__).with_name(...)"
        )

    return report


def _is_absolute_or_explicit_relative(value: str) -> bool:
    pure = Path(value)
    if pure.is_absolute():
        return True
    return value.startswith("./") or value.startswith("../")
