import ast
import difflib
from dataclasses import dataclass, field
from fnmatch import fnmatch
import math
from pathlib import Path
from typing import Any

from .constants import AlertState, Severity
from .output import Output
from .patterns import matches_any_tag, matches_excluded_tag
from .registry import _minimum_severity, prepare_output_class, prepare_rule_class, prepare_source_class
from .helpers.value_rules import RangeRule, RateRule, StaleRule, ThresholdRule
from .rule import Rule
from .selectors import normalize_rule_inputs, resolve_rule_sources
from .source import Source


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
    report.warnings.extend(_decorator_warnings("rule", rule_id, rule_cls))

    tags = getattr(rule_cls, "tags", None)
    if tags == []:
        report.warnings.append(
            f"rule '{rule_id}' has no tags: outputs with include_tags, silences by tag, "
            "and remote imports filtered by tag do not see it"
        )
    return report


def validate_source_class(source_id: str, source_cls: type[Any]) -> ValidationReport:
    report = ValidationReport()
    try:
        prepare_source_class(source_cls)
    except Exception as exc:
        report.errors.append(str(exc))
        return report
    report.warnings.extend(_decorator_warnings("source", source_id, source_cls))
    return report


def validate_output_class(output_id: str, output_cls: type[Any]) -> ValidationReport:
    report = ValidationReport()
    try:
        prepare_output_class(output_cls)
    except Exception as exc:
        report.errors.append(str(exc))
        return report
    report.warnings.extend(_decorator_warnings("output", output_id, output_cls))
    return report


def _decorator_warnings(kind: str, plugin_id: str, cls: type[Any]) -> list[str]:
    warnings: list[str] = []
    for attr_name, class_value, decorator_value in getattr(cls, "__kanary_decorator_conflicts__", []):
        warnings.append(
            f"{kind} '{plugin_id}' sets '{attr_name}' in both the decorator and the class body "
            f"with different values ({_short_repr(decorator_value)} vs {_short_repr(class_value)}); "
            "the decorator value is used"
        )
    for attr_name, suggestion in getattr(cls, "__kanary_decorator_unknown_attrs__", []):
        hint = f"did you mean '{suggestion}'?" if suggestion else "check for a typo"
        warnings.append(f"{kind} '{plugin_id}' decorator sets unknown attribute '{attr_name}'; {hint}")
    return warnings


def _short_repr(value: Any, limit: int = 40) -> str:
    text = repr(value)
    return text if len(text) <= limit else text[: limit - 3] + "..."


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

        report.extend(_validate_rule_settings(rule_id, rule_cls))

        rule_cls.matched_outputs = matching_outputs(rule_cls, outputs)  # the engine works it out again
        if not rule_cls.matched_outputs:
            report.warnings.append(f"rule '{rule_id}' has no matching output")

    report.extend(_validate_dependencies(rules))

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


def _validate_dependencies(rules: dict[str, type[Any]]) -> ValidationReport:
    """suppressed_by / depends_on: unknown rule ids, and cycles.

    In a cycle the rules hold each other back: once one fires, none of them
    is ever notified again.
    """
    report = ValidationReport()
    parents: dict[str, list[str]] = {}
    for rule_id, rule_cls in rules.items():
        listed: list[str] = []
        for attr_name in ("suppressed_by", "depends_on"):
            for parent_id in getattr(rule_cls, attr_name, None) or []:
                if parent_id == rule_id:
                    report.errors.append(f"rule '{rule_id}' lists itself in {attr_name}")
                elif parent_id not in rules:
                    close = difflib.get_close_matches(parent_id, list(rules), n=1)
                    hint = f"; did you mean '{close[0]}'?" if close else ""
                    report.warnings.append(
                        f"rule '{rule_id}' {attr_name} '{parent_id}' is not a loaded rule (a typo, or excluded), "
                        f"so it never holds this rule back{hint}"
                    )
                else:
                    listed.append(parent_id)
        parents[rule_id] = listed

    reported: set[frozenset[str]] = set()
    state: dict[str, int] = {}  # 1: on the current path, 2: done
    path: list[str] = []

    def visit(rule_id: str) -> None:
        state[rule_id] = 1
        path.append(rule_id)
        for parent_id in parents.get(rule_id, []):
            if state.get(parent_id) == 1:
                cycle = path[path.index(parent_id):]
                if frozenset(cycle) not in reported:
                    reported.add(frozenset(cycle))
                    chain = " -> ".join(f"'{item}'" for item in [*cycle, parent_id])
                    report.errors.append(
                        f"rules {chain} hold each other back (suppressed_by / depends_on form a cycle)"
                    )
            elif parent_id not in state:
                visit(parent_id)
        path.pop()
        state[rule_id] = 2

    for rule_id in rules:
        if rule_id not in state:
            visit(rule_id)
    return report


def matching_outputs(rule_cls: type[Any] | Any, outputs: dict[str, type[Output] | Output]) -> list[str]:
    """The outputs a rule's alerts can reach (by tags, severity, and states); rule and outputs may be instances."""
    rule_tags = set(getattr(rule_cls, "tags", []))
    rule_severity = getattr(rule_cls, "severity", None)
    matched: list[str] = []
    possible_states = {state.value for state in AlertState}
    for output_id, output_cls in outputs.items():
        include_tags = list(getattr(output_cls, "include_tags", []))
        exclude_tags = list(getattr(output_cls, "exclude_tags", []))
        exclude_states = set(getattr(output_cls, "exclude_states", []))
        minimum_severity = _minimum_severity(getattr(output_cls, "minimum_severity", None))

        if include_tags and not matches_any_tag(rule_tags, include_tags):
            continue
        if exclude_tags and matches_excluded_tag(rule_tags, exclude_tags):
            continue
        if (
            minimum_severity is not None
            and isinstance(rule_severity, Severity)
            and rule_severity < minimum_severity
        ):
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
    evaluation_interval = getattr(rule_cls, "evaluation_interval", None)
    if evaluation_interval is not None and (
        isinstance(evaluation_interval, bool)
        or not isinstance(evaluation_interval, (int, float))
        or not math.isfinite(evaluation_interval)
        or evaluation_interval <= 0
    ):
        report.errors.append(
            f"rule '{rule_id}' evaluation_interval must be a positive finite number"
        )
    if (
        isinstance(rule_cls, type)
        and issubclass(rule_cls, RangeRule)
        and getattr(rule_cls, "low", None) is None
        and getattr(rule_cls, "high", None) is None
    ):
        if getattr(rule_cls, "fire_when", "outside") == "inside":
            report.warnings.append(
                f"rule '{rule_id}' sets neither low nor high with fire_when='inside', so it always fires; set at least one bound"
            )
        else:
            report.warnings.append(
                f"rule '{rule_id}' sets neither low nor high, so it never fires; set at least one bound"
            )
    if isinstance(rule_cls, type) and issubclass(rule_cls, (RangeRule, ThresholdRule, StaleRule)):
        removed = [
            name
            for name in _REMOVED_RULE_SETTINGS
            if any(name in vars(klass) for klass in rule_cls.__mro__ if klass not in _BUILTIN_RULES)
        ]
        if removed:
            report.warnings.append(
                f"rule '{rule_id}' sets {', '.join(removed)}, which built-in rules no longer read; "
                "select the value with inputs='source:input' instead"
            )
    return report


_REMOVED_RULE_SETTINGS = ("field", "timestamp_field", "previous_field", "previous_timestamp_field")
_BUILTIN_RULES = (RangeRule, RateRule, ThresholdRule, StaleRule, Rule, object)


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
            f"file '{path.name}' uses open('{file_name}') with a cwd-relative path; prefer kanary.plugin_dir() / ... or Path(__file__).with_name(...)"
        )

    report.warnings.extend(_evaluate_warnings(path, tree))
    return report


# kanary.<name>(...) that make an evaluation; for the *_if ones the message is the second argument.
_FIRING_HELPERS = {"firing", "fire", "info", "warn", "error", "critical"}
_IF_HELPERS = {"fire_if", "firing_if", "info_if", "warn_if", "error_if", "critical_if"}
_EVALUATION_HELPERS = _FIRING_HELPERS | _IF_HELPERS | {"ok", "ok_if"}


def _evaluate_warnings(path: Path, tree: ast.AST) -> list[str]:
    """Two easy mistakes in a rule's evaluate().

    - kanary.firing(...) as a statement: its result is dropped and the rule stays OK.
      (Falling off the end without a return is fine: that is OK on purpose.)
    - a FIRING without a message (kanary.error(), return True): the notification
      then shows only the rule id.
    """
    warnings: list[str] = []
    for cls in (node for node in ast.walk(tree) if isinstance(node, ast.ClassDef)):
        for method in cls.body:
            if not isinstance(method, (ast.FunctionDef, ast.AsyncFunctionDef)) or method.name != "evaluate":
                continue
            where = f"file '{path.name}' line {{line}}: {cls.name}.evaluate()"
            for node in sorted(_walk_own_body(method), key=lambda item: getattr(item, "lineno", 0)):
                if isinstance(node, ast.Expr) and (name := _helper_name(node.value)) in _EVALUATION_HELPERS:
                    warnings.append(
                        where.format(line=node.lineno)
                        + f" calls kanary.{name}(...) without returning it, so the rule stays OK; write return kanary.{name}(...)"
                    )
                elif isinstance(node, ast.Call) and _fires_without_message(node):
                    warnings.append(
                        where.format(line=node.lineno)
                        + f" fires with kanary.{_helper_name(node)}() without a message; the notification shows only the rule id"
                    )
                elif isinstance(node, ast.Return) and isinstance(node.value, ast.Constant) and node.value.value is True:
                    warnings.append(
                        where.format(line=node.lineno)
                        + " returns True, a FIRING without a message; the notification shows only the rule id"
                    )
    return warnings


def _walk_own_body(function: ast.AST):
    """The nodes of a function, without those of functions and classes defined inside it."""
    pending = list(ast.iter_child_nodes(function))
    while pending:
        node = pending.pop()
        if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef, ast.Lambda, ast.ClassDef)):
            continue
        yield node
        pending.extend(ast.iter_child_nodes(node))


def _helper_name(node: ast.AST) -> str | None:
    """'firing' for a call kanary.firing(...), else None."""
    if (
        isinstance(node, ast.Call)
        and isinstance(node.func, ast.Attribute)
        and isinstance(node.func.value, ast.Name)
        and node.func.value.id == "kanary"
    ):
        return node.func.attr
    return None


def _fires_without_message(call: ast.Call) -> bool:
    name = _helper_name(call)
    if any(keyword.arg == "message" for keyword in call.keywords) or any(isinstance(arg, ast.Starred) for arg in call.args):
        return False
    if name in _FIRING_HELPERS:
        return not call.args
    if name in _IF_HELPERS:
        return len(call.args) < 2
    return False


def _is_absolute_or_explicit_relative(value: str) -> bool:
    pure = Path(value)
    if pure.is_absolute():
        return True
    return value.startswith("./") or value.startswith("../")
