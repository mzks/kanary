from __future__ import annotations

from dataclasses import dataclass
from importlib.util import module_from_spec, spec_from_file_location
from pathlib import Path
import sys
from types import ModuleType

from .filtering import apply_excludes
from .registry import (
    clear_registries,
    get_output_registry,
    get_output_duplicates,
    get_rule_duplicates,
    get_rule_registry,
    get_source_duplicates,
    get_source_registry,
    replace_registries,
)
from .validation import ValidationReport, validate_registries


@dataclass(slots=True)
class RegistrySnapshot:
    sources: dict[str, type]
    rules: dict[str, type]
    outputs: dict[str, type]


class RuleDirectoryLoader:
    def __init__(self, rule_directory: str | Path | list[str | Path]) -> None:
        if isinstance(rule_directory, (str, Path)):
            self.rule_directories = [Path(rule_directory)]
        else:
            self.rule_directories = [Path(path) for path in rule_directory]
        self._generation = 0

    def snapshot_signature(self) -> tuple[tuple[str, int], ...]:
        files = self._iter_rule_files()
        return tuple(
            sorted((str(path), path.stat().st_mtime_ns) for path in files)
        )

    def load(self, exclude_patterns: list[str] | None = None) -> RegistrySnapshot:
        snapshot, report = self.inspect(exclude_patterns=exclude_patterns)
        if not report.ok:
            raise ValueError(_format_validation_errors(report))
        return snapshot

    def inspect(self, exclude_patterns: list[str] | None = None) -> tuple[RegistrySnapshot, ValidationReport]:
        previous_sources = get_source_registry()
        previous_rules = get_rule_registry()
        previous_outputs = get_output_registry()
        clear_registries()
        self._generation += 1

        try:
            for index, path in enumerate(self._iter_rule_files()):
                module_name = f"_kanary_rules_{self._generation}_{index}"
                self._load_file(module_name, path)

            sources = get_source_registry()
            rules = get_rule_registry()
            outputs = get_output_registry()
            snapshot = apply_excludes(
                RegistrySnapshot(sources=sources, rules=rules, outputs=outputs),
                exclude_patterns,
            )
            report = validate_registries(
                sources=snapshot.sources,
                rules=snapshot.rules,
                outputs=snapshot.outputs,
                duplicate_rule_ids=get_rule_duplicates(),
                duplicate_source_ids=get_source_duplicates(),
                duplicate_output_ids=get_output_duplicates(),
            )
            return snapshot, report
        except Exception:
            replace_registries(
                rules=previous_rules,
                sources=previous_sources,
                outputs=previous_outputs,
            )
            raise

    def _iter_rule_files(self) -> list[Path]:
        files: list[Path] = []
        for rule_directory in self.rule_directories:
            if not rule_directory.exists():
                continue
            files.extend(
                path for path in rule_directory.rglob("*.py") if path.is_file()
            )
        return sorted(files)

    def _load_file(self, module_name: str, path: Path) -> ModuleType:
        spec = spec_from_file_location(module_name, path)
        if spec is None or spec.loader is None:
            raise ImportError(f"failed to load rule file: {path}")

        module = module_from_spec(spec)
        sys.modules[module_name] = module
        spec.loader.exec_module(module)
        return module


def _format_validation_errors(report: ValidationReport) -> str:
    return "\n".join(report.errors)
