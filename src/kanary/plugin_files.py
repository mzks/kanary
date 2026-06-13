import json
from pathlib import Path
import sys
import tomllib
from typing import Any


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
        raise RuntimeError(f"plugin config file not found: {path}") from exc
    except (OSError, tomllib.TOMLDecodeError) as exc:
        raise RuntimeError(f"failed to load TOML config '{path}': {exc}") from exc
    return _resolve_key(data, key, path)


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
        raise RuntimeError(f"plugin config file not found: {path}") from exc
    except (OSError, json.JSONDecodeError) as exc:
        raise RuntimeError(f"failed to load JSON config '{path}': {exc}") from exc
    return _resolve_key(data, key, path)


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
