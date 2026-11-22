from enum import IntEnum, StrEnum

DEFAULT_VIEWER_TITLE = "KANARY Viewer"


class Severity(IntEnum):
    INFO = 10
    WARN = 20
    ERROR = 30
    CRITICAL = 40


class AlertState(StrEnum):
    OK = "OK"
    FIRING = "FIRING"
    ACKED = "ACKED"
    SILENCED = "SILENCED"
    SUPPRESSED = "SUPPRESSED"


class TransitionKind(StrEnum):
    UNACK = "UNACK"
    ESCALATED = "ESCALATED"
    DEESCALATED = "DEESCALATED"


def severity_label(value: int | Severity) -> str:
    try:
        return Severity(value).name
    except ValueError:
        return str(value)


INFO = Severity.INFO
WARN = Severity.WARN
ERROR = Severity.ERROR
CRITICAL = Severity.CRITICAL

OK = AlertState.OK
FIRING = AlertState.FIRING
ACKED = AlertState.ACKED
SILENCED = AlertState.SILENCED
SUPPRESSED = AlertState.SUPPRESSED

UNACK = TransitionKind.UNACK
ESCALATED = TransitionKind.ESCALATED
DEESCALATED = TransitionKind.DEESCALATED


def coerce_severity(value: "Severity | str | int") -> Severity:
    """A Severity from kanary.ERROR, its name in any case ("ERROR", "Error", "error"), or its number (30)."""
    if isinstance(value, Severity):
        return value
    if isinstance(value, str):
        try:
            return Severity[value.strip().upper()]
        except KeyError:
            raise ValueError(f"unknown severity {value!r}: use INFO, WARN, ERROR, or CRITICAL") from None
    if isinstance(value, int) and not isinstance(value, bool):
        try:
            return Severity(value)
        except ValueError:
            raise ValueError(f"unknown severity {value!r}: the numbers are 10, 20, 30, and 40") from None
    raise TypeError(f"severity must be kanary.INFO/WARN/ERROR/CRITICAL or its name, not {value!r}")


def severity_name(value: "Severity | int | None") -> str | None:
    """The name JSON carries for a severity ("ERROR"); None stays None."""
    return None if value is None else severity_label(value)
