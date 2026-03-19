from enum import IntEnum, StrEnum


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
    RESOLVED = "RESOLVED"


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
RESOLVED = AlertState.RESOLVED
