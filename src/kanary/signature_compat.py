from __future__ import annotations

import inspect
from typing import Any


_POSITIONAL_KINDS = {
    inspect.Parameter.POSITIONAL_ONLY,
    inspect.Parameter.POSITIONAL_OR_KEYWORD,
}


def detect_instance_method_style(
    method: Any,
    *,
    new_arity: int,
    legacy_arity: int,
    new_signature: str,
    legacy_signature: str,
) -> str:
    parameters = list(inspect.signature(method).parameters.values())
    if parameters and parameters[0].name == "self":
        parameters = parameters[1:]

    for parameter in parameters:
        if parameter.kind not in _POSITIONAL_KINDS:
            raise ValueError(
                f"signature must be {new_signature} or legacy {legacy_signature}"
            )

    arity = len(parameters)
    if arity == new_arity:
        return "new"
    if arity == legacy_arity:
        return "legacy"
    raise ValueError(
        f"signature must be {new_signature} or legacy {legacy_signature}"
    )


def invoke_compat(
    method: Any,
    *,
    style: str,
    new_args: tuple[Any, ...],
    legacy_args: tuple[Any, ...],
) -> Any:
    if style == "legacy":
        return method(*legacy_args)
    return method(*new_args)
