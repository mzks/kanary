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


def coerce_severity(value: "Severity | str") -> Severity:
    """A Severity from a Severity or its name ("warn", "ERROR", ...)."""
    if isinstance(value, Severity):
        return value
    if isinstance(value, str):
        return Severity[value.strip().upper()]
    raise TypeError("severity must be kanary.INFO/WARN/ERROR/CRITICAL or a matching string")
