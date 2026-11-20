from collections.abc import Iterator
from contextlib import contextmanager
import json
from pathlib import Path
import sys
import threading
import tomllib
from typing import Any

_lint = threading.local()


@contextmanager
def tolerating_missing_files() -> Iterator[list[str]]:
    """While linting: load_json() / load_toml() give None for a missing file or key.

    Lint loads the plugin files to check them, but uses none of their settings, so
    a secrets file that exists only on the server must not stop it. Each miss is
    noted in the yielded list. Outside this block a miss raises as usual.
    """
    previous = getattr(_lint, "notes", None)
    _lint.notes = notes = []
    try:
        yield notes
    finally:
        _lint.notes = previous


def plugin_dir() -> Path:
    caller_file = _caller_file()
    return caller_file.parent


def load_toml(
    key: str | None = None,
    *,
    filename: str | Path = "config.toml",
) -> Any:
    path = _resolve_plugin_path(filename)
    try:
        with path.open("rb") as handle:
            data = tomllib.load(handle)
    except FileNotFoundError as exc:
        return _missing(f"plugin config file not found: {path}", "load_toml", exc)
    except (OSError, tomllib.TOMLDecodeError) as exc:
        raise RuntimeError(f"failed to load TOML config '{path}': {exc}") from exc
    return _resolve_key_or_missing(data, key, path, "load_toml")


def load_json(
    key: str | None = None,
    *,
    filename: str | Path = "config.json",
) -> Any:
    path = _resolve_plugin_path(filename)
    try:
        with path.open("r", encoding="utf-8") as handle:
            data = json.load(handle)
    except FileNotFoundError as exc:
        return _missing(f"plugin config file not found: {path}", "load_json", exc)
    except (OSError, json.JSONDecodeError) as exc:
        raise RuntimeError(f"failed to load JSON config '{path}': {exc}") from exc
    return _resolve_key_or_missing(data, key, path, "load_json")


def _resolve_key_or_missing(data: Any, key: str | None, path: Path, function: str) -> Any:
    try:
        return _resolve_key(data, key, path)
    except RuntimeError as exc:
        return _missing(str(exc), function, exc)


def _missing(problem: str, function: str, exc: Exception) -> Any:
    notes = getattr(_lint, "notes", None)
    if notes is None:
        raise RuntimeError(problem) from exc
    try:
        where = str(_caller_file())
    except RuntimeError:
        where = "a plugin file"
    notes.append(f"{where}: {problem}; {function}() gave None while linting")
    return None


def _resolve_plugin_path(filename: str | Path) -> Path:
    path = Path(filename)
    if path.is_absolute():
        return path
    return plugin_dir() / path


def _caller_file() -> Path:
    frame = sys._getframe(1)
    while frame is not None and frame.f_globals.get("__name__") == __name__:
        frame = frame.f_back
    if frame is None:
        raise RuntimeError("could not determine plugin file path")
    caller = frame.f_globals.get("__file__")
    if not caller:
        raise RuntimeError("could not determine plugin file path")
    return Path(caller).resolve()


def _resolve_key(data: Any, key: str | None, path: Path) -> Any:
    if key is None:
        return data
    current = data
    for part in key.split("."):
        if not isinstance(current, dict) or part not in current:
            raise RuntimeError(f"config key '{key}' not found in {path}")
        current = current[part]
    return current


def path_beside_plugin(plugin: Any, path: str | Path) -> Path:
    """Resolve a relative path against the directory of the plugin's file."""
    path = Path(path).expanduser()
    if path.is_absolute():
        return path
    definition_file = getattr(type(plugin), "__kanary_definition_file__", None)
    base = Path(definition_file).parent if definition_file else Path.cwd()
    return base / path

