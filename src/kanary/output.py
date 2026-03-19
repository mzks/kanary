from typing import Any

from .models import AlertEvent


class Output:
    output_id: str
    include_tags: list[str] = []
    exclude_tags: list[str] = []
    include_states: list[str] = []
    exclude_states: list[str] = []

    def init(self, ctx: dict[str, Any]) -> None:
        return None

    def emit(self, event: AlertEvent, ctx: dict[str, Any]) -> None:
        raise NotImplementedError

    def terminate(self, ctx: dict[str, Any]) -> None:
        return None

    def matches(self, event: AlertEvent) -> bool:
        alert_tags = set(event.alert.tags)
        if self.include_tags and not alert_tags.intersection(self.include_tags):
            return False
        if self.exclude_tags and alert_tags.intersection(self.exclude_tags):
            return False
        state = event.current_state.value
        if self.include_states and state not in self.include_states:
            return False
        if self.exclude_states and state in self.exclude_states:
            return False
        return True


def prepare_output_class(cls: type[Any]) -> type[Any]:
    _setdefault(cls, "include_tags", [])
    _setdefault(cls, "exclude_tags", [])
    _setdefault(cls, "include_states", [])
    _setdefault(cls, "exclude_states", [])
    if "init" not in cls.__dict__ and getattr(cls, "init", None) in {None, Output.init}:
        cls.init = Output.init
    if "terminate" not in cls.__dict__ and getattr(cls, "terminate", None) in {None, Output.terminate}:
        cls.terminate = Output.terminate
    if "matches" not in cls.__dict__ and getattr(cls, "matches", None) in {None, Output.matches}:
        cls.matches = Output.matches

    output_id = getattr(cls, "output_id", None)
    if not isinstance(output_id, str) or not output_id:
        raise ValueError(f"output '{cls.__name__}' must define non-empty string output_id")
    if not callable(getattr(cls, "emit", None)):
        raise ValueError(f"output '{output_id}' must implement emit(event, ctx)")
    return cls


def _setdefault(cls: type[Any], attr_name: str, value: Any) -> None:
    if hasattr(cls, attr_name):
        return
    setattr(cls, attr_name, value)
