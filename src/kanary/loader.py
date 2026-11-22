from __future__ import annotations

from dataclasses import dataclass
import importlib
from importlib.abc import MetaPathFinder
from importlib.machinery import PathFinder, SourceFileLoader
from importlib.util import module_from_spec, spec_from_file_location
from pathlib import Path
import sys
import sysconfig
from types import ModuleType
from typing import Any

from .filtering import apply_excludes
from .plugin_files import tolerating_missing_files
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
from .validation import ValidationReport, validate_plugin_files, validate_registries


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
        signature = []
        for path in self._iter_rule_files():
            try:
                signature.append((str(path), path.stat().st_mtime_ns))
            except FileNotFoundError:
                continue  # removed between the listing and now: it is not there
        return tuple(sorted(signature))

    def load(self, exclude_patterns: list[str] | None = None) -> RegistrySnapshot:
        snapshot, report = self.inspect(exclude_patterns=exclude_patterns)
        if not report.ok:
            raise ValueError(_format_validation_errors(report))
        return snapshot

    def inspect(
        self,
        exclude_patterns: list[str] | None = None,
        *,
        lint: bool = False,
    ) -> tuple[RegistrySnapshot, ValidationReport]:
        """Load the plugin files and check them. With lint=True, a config or secrets
        file that load_json() / load_toml() cannot find is a warning, not an error."""
        if lint:
            with tolerating_missing_files() as notes:
                snapshot, report = self.inspect(exclude_patterns)
            report.warnings.extend(notes)
            return snapshot, report
        missing = [str(path) for path in self.rule_directories if not path.exists()]
        if missing:
            # A typo in the path would otherwise load nothing and report no problem.
            raise ValueError(f"plugin directory not found: {', '.join(missing)}")
        _HELPER_MODULES.prepare(self.rule_directories)
        previous_sources = get_source_registry()
        previous_rules = get_rule_registry()
        previous_outputs = get_output_registry()
        clear_registries()
        self._generation += 1
        files = self._iter_rule_files()
        try:
            for index, path in enumerate(files):
                package_module = self._package_module_name(path)
                if package_module is not None:
                    self._import_package_file(package_module, path)
                else:
                    self._load_file(f"_kanary_rules_{self._generation}_{index}", path)

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
            report.extend(validate_plugin_files(files))
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
            if rule_directory.is_file():
                if rule_directory.suffix == ".py":
                    files.append(rule_directory)
                continue
            files.extend(
                path for path in rule_directory.rglob("*.py") if path.is_file()
            )
        return sorted(files)

    def _package_module_name(self, path: Path) -> str | None:
        """``pkg.limits`` for pkg/limits.py in a package (a directory with __init__.py) under a
        plugin directory: such a file is a module of its package, whose relative imports
        (``from .values import LIMIT``) work. None for any other plugin file."""
        for root in self.rule_directories:
            if not root.is_dir():
                continue
            try:
                parts = path.relative_to(root).with_suffix("").parts
            except ValueError:
                continue
            if len(parts) < 2 or not (path.parent / "__init__.py").is_file():
                return None
            return ".".join(parts[:-1] if parts[-1] == "__init__" else parts)
        return None

    def _import_package_file(self, module_name: str, path: Path) -> ModuleType:
        """Import it by its name, as a plugin that imports it does: run once per load."""
        try:
            module = importlib.import_module(module_name)
        except Exception as exc:
            raise RuntimeError(
                f"failed to load plugin file '{path}': {type(exc).__name__}: {exc}"
            ) from exc
        module_file = getattr(module, "__file__", None)
        if not module_file or Path(module_file).resolve() != path.resolve():
            raise RuntimeError(
                f"failed to load plugin file '{path}': the name '{module_name}' is taken by "
                f"{module_file or 'another module'}; rename the package directory"
            )
        return module

    def _load_file(self, module_name: str, path: Path) -> ModuleType:
        spec = spec_from_file_location(module_name, path, loader=_FreshSourceLoader(module_name, str(path)))
        if spec is None or spec.loader is None:
            raise ImportError(f"failed to load rule file: {path}")

        module = module_from_spec(spec)
        sys.modules[module_name] = module
        try:
            spec.loader.exec_module(module)
        except Exception as exc:
            raise RuntimeError(
                f"failed to load plugin file '{path}': {type(exc).__name__}: {exc}"
            ) from exc
        return module


class _FreshSourceLoader(SourceFileLoader):
    """Compile a plugin file from its text on disk every time, without __pycache__.

    The bytecode cache is checked by the file's mtime in whole seconds and its size,
    so an edit within the same second that keeps the size would run the old code.
    """

    def get_code(self, fullname: str):
        return self.source_to_code(self.get_data(self.path), self.path)


class _HelperModuleFinder(MetaPathFinder):
    """Modules that plugins import from their own directories (``from shared import LIMIT``).

    The plugin directories are on the import path (after everything else, so a helper
    never hides a module of Python or of an installed package). Such a module is read
    from its file every time the plugins load, like the plugin files: an edit to a
    shared helper reaches the plugins on the next reload, not only on a restart.
    """

    def __init__(self) -> None:
        self.roots: set[Path] = set()
        self.loaded: set[str] = set()

    def prepare(self, directories: list[Path]) -> None:
        for directory in directories:
            if not directory.exists():
                continue
            root = (directory if directory.is_dir() else directory.parent).resolve()
            self.roots.add(root)
            if str(root) not in sys.path:
                sys.path.append(str(root))
        if self not in sys.meta_path:
            sys.meta_path.insert(0, self)
        for name in self.loaded:  # import them again from their files
            sys.modules.pop(name, None)
        self.loaded.clear()

    def find_spec(self, fullname: str, path: Any = None, target: Any = None) -> Any:
        if fullname == "kanary" or fullname.startswith("kanary.") or not self.roots:
            return None
        spec = PathFinder.find_spec(fullname, path)
        if spec is None or not spec.origin or not spec.origin.endswith(".py"):
            return None
        origin = Path(spec.origin).resolve()
        if not any(origin.is_relative_to(root) for root in self.roots) or _installed(origin):
            return None  # not a helper of the plugins: imported as usual
        spec.loader = _FreshSourceLoader(fullname, spec.origin)
        self.loaded.add(fullname)
        return spec


_HELPER_MODULES = _HelperModuleFinder()


def _python_locations() -> list[Path]:
    """Where Python and installed packages live: never helpers, even under a plugin directory."""
    paths = sysconfig.get_paths()
    places = {paths.get(key) for key in ("stdlib", "platstdlib", "purelib", "platlib")}
    return [Path(place).resolve() for place in places if place]


_PYTHON_LOCATIONS = _python_locations()


def installed_file(path: str | Path) -> bool:
    """A file of Python or of an installed package, not plugin code."""
    return _installed(Path(path).resolve())


def _installed(origin: Path) -> bool:
    if {"site-packages", "dist-packages"} & set(origin.parts):
        return True
    return any(origin.is_relative_to(place) for place in _PYTHON_LOCATIONS)


def _format_validation_errors(report: ValidationReport) -> str:
    return "\n".join(report.errors)
