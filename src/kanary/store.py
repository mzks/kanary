from __future__ import annotations

from collections.abc import Iterator
from contextlib import contextmanager
from dataclasses import dataclass, field
from datetime import datetime
import json
from pathlib import Path
import sqlite3
import threading
from typing import Any

from .constants import AlertState, Severity, severity_name
from .models import Acknowledgement, AlertEvent, Silence
from .values import coerce_datetime, json_default, json_ready


@dataclass(slots=True)
class RestoredRuleState:
    """A rule's alert as its last stored event left it: the state KANARY stopped with."""

    state: AlertState
    severity: Severity
    message: str | None
    payload: dict[str, Any]
    occurred_at: datetime  # of that last event
    active_since: datetime | None  # first event since the rule was last OK; None when OK


@dataclass(slots=True)
class RestoredJudgment:
    """A rule's own last judgment (FIRING, OK), whatever a silence or a parent showed of it."""

    state: AlertState
    severity: Severity
    message: str | None
    payload: dict[str, Any]
    evaluated_at: datetime
    active_since: datetime | None


@dataclass(slots=True)
class RestoredState:
    acknowledgements: dict[str, Acknowledgement] = field(default_factory=dict)
    silences: dict[str, Silence] = field(default_factory=dict)
    latest_rule_states: dict[str, RestoredRuleState] = field(default_factory=dict)
    judgments: dict[str, RestoredJudgment] = field(default_factory=dict)
    # Per output, the alerts it was sent and not yet the OK of, with the severity sent last.
    # None: not kept by this database yet (one from before), so not known.
    output_notices: dict[str, dict[str, Severity]] | None = None


# Each rule's own last judgment, kept apart from the alert events: those record what was
# shown (SILENCED, SUPPRESSED), and a judgment can change while that does not. Added to
# databases of schema version 1 when missing; older KANARY ignores it.
_JUDGMENTS_TABLE = """
CREATE TABLE IF NOT EXISTS rule_judgments (
    rule_id TEXT PRIMARY KEY,
    state TEXT NOT NULL,
    severity INTEGER NOT NULL,
    message TEXT,
    payload_json TEXT NOT NULL DEFAULT '{}',
    evaluated_at TEXT NOT NULL,
    active_since TEXT
)
"""

# What each output was told: an OK goes where it closes an alert, with that alert's severity.
_OUTPUT_NOTICES_TABLE = """
CREATE TABLE IF NOT EXISTS output_notices (
    output_id TEXT NOT NULL,
    rule_id TEXT NOT NULL,
    severity INTEGER NOT NULL,
    PRIMARY KEY (output_id, rule_id)
)
"""


class NullStore:
    enabled = False
    schema_version = 0
    target_schema_version = 1

    def initialize(self) -> None:
        return None

    def close(self) -> None:
        return None

    def load_runtime_state(self) -> RestoredState:
        return RestoredState()

    def append_alert_event(self, event: AlertEvent, *, definition_file: str | None, matched_outputs: list[str]) -> None:
        return None

    def append_output_dispatch(
        self,
        *,
        event: AlertEvent,
        matched_outputs: list[str],
        delivered_outputs: list[str],
        filtered_outputs: list[str],
        uninitialized_outputs: list[str],
        failed_outputs: list[str],
    ) -> None:
        return None

    def record_acknowledgement(self, acknowledgement: Acknowledgement) -> None:
        return None

    def record_unacknowledgement(
        self,
        *,
        rule_id: str,
        operator: str,
        reason: str | None,
        created_at: datetime,
    ) -> None:
        return None

    def record_rule_removed(
        self,
        *,
        rule_id: str,
        definition_file: str | None,
        previous_state: str,
        previous_severity: str | None,
        operator: str,
        reason: str,
        created_at: datetime,
        had_ack: bool,
        active_silence_ids: list[str],
    ) -> None:
        return None

    def create_silence(self, silence: Silence) -> None:
        return None

    def cancel_silence(self, silence: Silence) -> None:
        return None

    def record_judgment(self, rule_id: str, judgment: RestoredJudgment) -> None:
        return None

    def forget_judgment(self, rule_id: str) -> None:
        return None

    def record_output_notice(self, output_id: str, rule_id: str, severity: Severity | None) -> None:
        return None

    def forget_output_notices(self, *, output_id: str | None = None, rule_id: str | None = None) -> None:
        return None

    def get_rule_history(
        self, rule_id: str, rule_tags: list[str] | None = None, *, limit: int | None = None
    ) -> dict[str, list[dict[str, Any]]]:
        return {
            "enabled": False,
            "alert_events": [],
            "output_dispatches": [],
            "operator_actions": [],
        }

    def get_alert_history(
        self,
        *,
        limit: int = 100,
        after_alert_id: int | None = None,
        after_action_id: int | None = None,
    ) -> dict[str, Any]:
        return {
            "enabled": False,
            "entries": [],
            "latest_alert_id": after_alert_id or 0,
            "latest_action_id": after_action_id or 0,
            "alert_has_more": False,
            "action_has_more": False,
            "initial_truncated": False,
        }


class SQLiteStore:
    enabled = True
    SCHEMA_VERSION = 1

    def __init__(self, path: str | Path) -> None:
        self.path = Path(path)
        self._lock = threading.RLock()
        self._conn: sqlite3.Connection | None = None
        self.schema_version = 0
        self.target_schema_version = self.SCHEMA_VERSION
        self._notices_known = False

    def initialize(self) -> None:
        with self._lock:
            if self._conn is not None:
                return
            self.path.parent.mkdir(parents=True, exist_ok=True)
            conn = sqlite3.connect(self.path, check_same_thread=False)
            try:
                conn.row_factory = sqlite3.Row
                conn.execute("PRAGMA journal_mode=WAL")
                conn.execute("PRAGMA synchronous=NORMAL")
                self._ensure_schema(conn)
                conn.execute(_JUDGMENTS_TABLE)
                # A database from before kept no notices: what outputs were told is not known.
                self._notices_known = _has_table(conn, "output_notices")
                conn.execute(_OUTPUT_NOTICES_TABLE)
                conn.commit()
            except BaseException:
                conn.close()  # a database KANARY cannot use is not left open
                raise
            self._conn = conn

    def _ensure_schema(self, conn: sqlite3.Connection) -> None:
        version = int(conn.execute("PRAGMA user_version").fetchone()[0])
        if version == 0:
            if self._has_user_tables(conn):
                raise RuntimeError(
                    "unsupported legacy state DB schema (user_version=0); create a fresh state DB for this Kanary version"
                )
            self._create_schema_v1(conn)
            conn.execute(f"PRAGMA user_version = {self.SCHEMA_VERSION}")
            self.schema_version = self.SCHEMA_VERSION
            return
        if version != self.SCHEMA_VERSION:
            raise RuntimeError(
                f"unsupported state DB schema version {version}; expected {self.SCHEMA_VERSION}"
            )
        self._assert_schema_v1_tables(conn)
        self.schema_version = version

    def _has_user_tables(self, conn: sqlite3.Connection) -> bool:
        row = conn.execute(
            """
            SELECT COUNT(*)
            FROM sqlite_master
            WHERE type = 'table' AND name NOT LIKE 'sqlite_%'
            """
        ).fetchone()
        return bool(row[0])

    def _assert_schema_v1_tables(self, conn: sqlite3.Connection) -> None:
        required = {
            "alert_events",
            "output_dispatches",
            "operator_actions",
            "silences",
        }
        found = {
            row["name"]
            for row in conn.execute(
                """
                SELECT name
                FROM sqlite_master
                WHERE type = 'table'
                """
            )
        }
        missing = sorted(required - found)
        if missing:
            raise RuntimeError(
                f"state DB schema version {self.SCHEMA_VERSION} is incomplete; missing tables: {', '.join(missing)}"
            )

    def _create_schema_v1(self, conn: sqlite3.Connection) -> None:
        conn.executescript(
            """
            CREATE TABLE alert_events (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                rule_id TEXT NOT NULL,
                previous_state TEXT,
                current_state TEXT NOT NULL,
                previous_severity INTEGER,
                current_severity INTEGER,
                transition TEXT,
                severity INTEGER NOT NULL,
                owner TEXT,
                message TEXT,
                payload_json TEXT NOT NULL,
                tags_json TEXT NOT NULL,
                occurred_at TEXT NOT NULL,
                definition_file TEXT,
                matched_outputs_json TEXT NOT NULL DEFAULT '[]'
            );

            CREATE INDEX idx_alert_events_rule_time
                ON alert_events (rule_id, occurred_at DESC, id DESC);

            CREATE INDEX idx_alert_events_time
                ON alert_events (occurred_at DESC, id DESC);

            CREATE TABLE output_dispatches (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                rule_id TEXT NOT NULL,
                previous_state TEXT,
                current_state TEXT NOT NULL,
                occurred_at TEXT NOT NULL,
                matched_outputs_json TEXT NOT NULL DEFAULT '[]',
                delivered_outputs_json TEXT NOT NULL DEFAULT '[]',
                filtered_outputs_json TEXT NOT NULL DEFAULT '[]',
                uninitialized_outputs_json TEXT NOT NULL DEFAULT '[]',
                failed_outputs_json TEXT NOT NULL DEFAULT '[]'
            );

            CREATE INDEX idx_output_dispatches_rule_time
                ON output_dispatches (rule_id, occurred_at DESC, id DESC);

            CREATE INDEX idx_output_dispatches_time
                ON output_dispatches (occurred_at DESC, id DESC);

            CREATE TABLE operator_actions (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                action_type TEXT NOT NULL,
                rule_id TEXT,
                silence_id TEXT,
                operator TEXT NOT NULL,
                reason TEXT,
                details_json TEXT NOT NULL DEFAULT '{}',
                created_at TEXT NOT NULL
            );

            CREATE INDEX idx_operator_actions_rule_time
                ON operator_actions (rule_id, created_at DESC, id DESC);

            CREATE INDEX idx_operator_actions_silence_time
                ON operator_actions (silence_id, created_at DESC, id DESC);

            CREATE INDEX idx_operator_actions_time
                ON operator_actions (created_at DESC, id DESC);

            CREATE TABLE silences (
                silence_id TEXT PRIMARY KEY,
                created_by TEXT NOT NULL,
                reason TEXT,
                created_at TEXT NOT NULL,
                start_at TEXT NOT NULL,
                end_at TEXT NOT NULL,
                rule_patterns_json TEXT NOT NULL DEFAULT '[]',
                tags_json TEXT NOT NULL DEFAULT '[]',
                remote_silence_refs_json TEXT NOT NULL DEFAULT '[]',
                cancelled_at TEXT,
                cancelled_by TEXT,
                cancel_reason TEXT
            );

            CREATE INDEX idx_silences_window
                ON silences (start_at, end_at);

            CREATE INDEX idx_silences_cancelled
                ON silences (cancelled_at);
            """
        )

    def close(self) -> None:
        with self._lock:
            if self._conn is None:
                return
            self._conn.close()
            self._conn = None

    def load_runtime_state(self) -> RestoredState:
        conn = self._require_conn()
        with self._lock:
            acknowledgements = self._load_acknowledgements(conn)

            silences: dict[str, Silence] = {}
            for row in conn.execute("SELECT * FROM silences ORDER BY created_at, silence_id"):
                silences[row["silence_id"]] = Silence(
                    silence_id=row["silence_id"],
                    created_by=row["created_by"],
                    reason=row["reason"],
                    created_at=_parse_datetime(row["created_at"]),
                    start_at=_parse_datetime(row["start_at"]),
                    end_at=_parse_datetime(row["end_at"]),
                    rule_patterns=tuple(json.loads(row["rule_patterns_json"])),
                    tags=tuple(json.loads(row["tags_json"])),
                    remote_silence_refs=tuple(json.loads(row["remote_silence_refs_json"])),
                    cancelled_at=_parse_datetime(row["cancelled_at"]) if row["cancelled_at"] else None,
                    cancelled_by=row["cancelled_by"],
                    cancel_reason=row["cancel_reason"],
                )

            latest_rule_states = self._load_latest_rule_states(conn)

            return RestoredState(
                acknowledgements=acknowledgements,
                silences=silences,
                latest_rule_states=latest_rule_states,
                judgments=self._load_judgments(conn),
                output_notices=self._load_output_notices(conn) if self._notices_known else None,
            )

    def _load_output_notices(self, conn: sqlite3.Connection) -> dict[str, dict[str, Severity]]:
        notices: dict[str, dict[str, Severity]] = {}
        for row in conn.execute("SELECT output_id, rule_id, severity FROM output_notices"):
            try:
                notices.setdefault(row["output_id"], {})[row["rule_id"]] = Severity(int(row["severity"]))
            except ValueError:
                continue
        return notices

    def record_output_notice(self, output_id: str, rule_id: str, severity: Severity | None) -> None:
        """The output was sent an alert of the rule (severity), or its OK (None)."""
        with self._transaction() as conn:
            if severity is None:
                conn.execute("DELETE FROM output_notices WHERE output_id = ? AND rule_id = ?", (output_id, rule_id))
            else:
                conn.execute(
                    "INSERT OR REPLACE INTO output_notices (output_id, rule_id, severity) VALUES (?, ?, ?)",
                    (output_id, rule_id, int(severity)),
                )

    def forget_output_notices(self, *, output_id: str | None = None, rule_id: str | None = None) -> None:
        """A removed output, or a removed rule: nothing of it is open any more."""
        with self._transaction() as conn:
            if output_id is not None:
                conn.execute("DELETE FROM output_notices WHERE output_id = ?", (output_id,))
            if rule_id is not None:
                conn.execute("DELETE FROM output_notices WHERE rule_id = ?", (rule_id,))

    def _load_judgments(self, conn: sqlite3.Connection) -> dict[str, RestoredJudgment]:
        judgments: dict[str, RestoredJudgment] = {}
        for row in conn.execute("SELECT * FROM rule_judgments"):
            try:
                payload = json.loads(row["payload_json"])
                judgments[row["rule_id"]] = RestoredJudgment(
                    state=AlertState(row["state"]),
                    severity=Severity(int(row["severity"])),
                    message=row["message"],
                    payload=payload if isinstance(payload, dict) else {},
                    evaluated_at=_parse_datetime(row["evaluated_at"]),
                    active_since=_parse_datetime(row["active_since"]) if row["active_since"] else None,
                )
            except (TypeError, ValueError):
                continue  # a row KANARY cannot read: the rule's next evaluation judges again
        return judgments

    def record_judgment(self, rule_id: str, judgment: RestoredJudgment) -> None:
        with self._transaction() as conn:
            conn.execute(
                """
                INSERT OR REPLACE INTO rule_judgments
                    (rule_id, state, severity, message, payload_json, evaluated_at, active_since)
                VALUES (?, ?, ?, ?, ?, ?, ?)
                """,
                (
                    rule_id,
                    judgment.state.value,
                    int(judgment.severity),
                    judgment.message,
                    json.dumps(json_ready(judgment.payload), ensure_ascii=False, default=json_default),
                    judgment.evaluated_at.isoformat(),
                    judgment.active_since.isoformat() if judgment.active_since else None,
                ),
            )

    def forget_judgment(self, rule_id: str) -> None:
        with self._transaction() as conn:
            conn.execute("DELETE FROM rule_judgments WHERE rule_id = ?", (rule_id,))

    def _load_acknowledgements(self, conn: sqlite3.Connection) -> dict[str, Acknowledgement]:
        """The acks that still hold: each one for the alert the rule is in now.

        An ack holds when it is the rule's last ack / unack / rule_removed action (the
        engine records an unack by "system" when an alert ends while silenced or
        suppressed), no OK came after it, and the rule's last state is one an ack goes
        on through (ACKED, or hidden by a silence or a parent).
        """
        latest_states = {
            row["rule_id"]: row["current_state"]
            for row in conn.execute(
                """
                SELECT ae.rule_id, ae.current_state
                FROM alert_events AS ae
                JOIN (SELECT rule_id, MAX(id) AS max_id FROM alert_events GROUP BY rule_id) latest
                  ON latest.max_id = ae.id
                """
            )
        }
        last_ok = {
            row["rule_id"]: _parse_datetime(row["occurred_at"])
            for row in conn.execute(
                """
                SELECT ae.rule_id, ae.occurred_at
                FROM alert_events AS ae
                JOIN (SELECT rule_id, MAX(id) AS ok_id FROM alert_events WHERE current_state = ? GROUP BY rule_id) ok
                  ON ok.ok_id = ae.id
                """,
                (AlertState.OK.value,),
            )
        }
        holding = {AlertState.ACKED.value, AlertState.SILENCED.value, AlertState.SUPPRESSED.value}
        acknowledgements: dict[str, Acknowledgement] = {}
        for row in conn.execute(
            """
            SELECT rule_id, action_type, operator, reason, created_at
            FROM operator_actions
            WHERE id IN (
                SELECT MAX(id) FROM operator_actions
                WHERE action_type IN ('ack', 'unack', 'rule_removed')
                GROUP BY rule_id
            )
            """
        ):
            rule_id = row["rule_id"]
            if row["action_type"] != "ack" or latest_states.get(rule_id) not in holding:
                continue
            created_at = _parse_datetime(row["created_at"])
            ok_at = last_ok.get(rule_id)
            if ok_at is not None and not created_at > ok_at:
                continue  # the alert it acknowledged has ended (a database written before 0.8.0)
            acknowledgements[rule_id] = Acknowledgement(
                rule_id=rule_id,
                operator=row["operator"],
                reason=row["reason"],
                created_at=created_at,
            )
        return acknowledgements

    def _load_latest_rule_states(self, conn: sqlite3.Connection) -> dict[str, RestoredRuleState]:
        active_since = {
            row["rule_id"]: _parse_datetime(row["occurred_at"])
            for row in conn.execute(
                """
                WITH last_ok AS (
                    SELECT rule_id, MAX(id) AS ok_id
                    FROM alert_events
                    WHERE current_state = ?
                    GROUP BY rule_id
                ),
                first_active AS (
                    SELECT ae.rule_id, MIN(ae.id) AS first_id
                    FROM alert_events AS ae
                    LEFT JOIN last_ok ON last_ok.rule_id = ae.rule_id
                    WHERE ae.id > COALESCE(last_ok.ok_id, 0)
                    GROUP BY ae.rule_id
                )
                SELECT fa.rule_id, ae.occurred_at
                FROM first_active AS fa
                JOIN alert_events AS ae ON ae.id = fa.first_id
                """,
                (AlertState.OK.value,),
            )
        }
        removed_at = {
            row["rule_id"]: _parse_datetime(row["created_at"])
            for row in conn.execute(
                """
                SELECT rule_id, created_at
                FROM operator_actions
                WHERE id IN (
                    SELECT MAX(id) FROM operator_actions WHERE action_type = 'rule_removed' GROUP BY rule_id
                )
                """
            )
        }
        states: dict[str, RestoredRuleState] = {}
        for row in conn.execute(
            """
            SELECT ae.rule_id, ae.current_state, ae.current_severity, ae.message, ae.payload_json, ae.occurred_at
            FROM alert_events AS ae
            JOIN (
                SELECT rule_id, MAX(id) AS max_id
                FROM alert_events
                GROUP BY rule_id
            ) latest ON latest.max_id = ae.id
            """
        ):
            rule_id = row["rule_id"]
            state = AlertState(row["current_state"])
            occurred_at = _parse_datetime(row["occurred_at"])
            removed = removed_at.get(rule_id)
            if removed is not None and _not_before(removed, occurred_at):
                continue  # removed in a reload after its last event: that state is no longer its own
            try:
                payload = json.loads(row["payload_json"])
            except ValueError:
                payload = {}
            states[rule_id] = RestoredRuleState(
                state=state,
                severity=Severity(int(row["current_severity"])),
                message=row["message"],
                payload=payload if isinstance(payload, dict) else {},
                occurred_at=occurred_at,
                active_since=active_since.get(rule_id) if state != AlertState.OK else None,
            )
        return states

    def append_alert_event(self, event: AlertEvent, *, definition_file: str | None, matched_outputs: list[str]) -> None:
        with self._transaction() as conn:
            conn.execute(
                """
                INSERT INTO alert_events (
                    rule_id,
                    previous_state,
                    current_state,
                    previous_severity,
                    current_severity,
                    transition,
                    severity,
                    owner,
                    message,
                    payload_json,
                    tags_json,
                    occurred_at,
                    definition_file,
                    matched_outputs_json
                ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                """,
                (
                    event.rule_id,
                    event.previous_state.value if event.previous_state is not None else None,
                    event.current_state.value,
                    int(event.previous_severity) if event.previous_severity is not None else None,
                    int(event.current_severity),
                    event.transition.value if event.transition is not None else None,
                    int(event.alert.severity),
                    event.alert.owner,
                    event.alert.message,
                    json.dumps(json_ready(event.alert.payload), ensure_ascii=False, default=json_default),
                    json.dumps(list(event.alert.tags), ensure_ascii=False),
                    event.occurred_at.isoformat(),
                    definition_file,
                    json.dumps(matched_outputs, ensure_ascii=False),
                ),
            )

    def append_output_dispatch(
        self,
        *,
        event: AlertEvent,
        matched_outputs: list[str],
        delivered_outputs: list[str],
        filtered_outputs: list[str],
        uninitialized_outputs: list[str],
        failed_outputs: list[str],
    ) -> None:
        with self._transaction() as conn:
            conn.execute(
                """
                INSERT INTO output_dispatches (
                    rule_id,
                    previous_state,
                    current_state,
                    occurred_at,
                    matched_outputs_json,
                    delivered_outputs_json,
                    filtered_outputs_json,
                    uninitialized_outputs_json,
                    failed_outputs_json
                ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)
                """,
                (
                    event.rule_id,
                    event.previous_state.value if event.previous_state is not None else None,
                    event.current_state.value,
                    event.occurred_at.isoformat(),
                    json.dumps(matched_outputs, ensure_ascii=False),
                    json.dumps(delivered_outputs, ensure_ascii=False),
                    json.dumps(filtered_outputs, ensure_ascii=False),
                    json.dumps(uninitialized_outputs, ensure_ascii=False),
                    json.dumps(failed_outputs, ensure_ascii=False),
                ),
            )

    def record_acknowledgement(self, acknowledgement: Acknowledgement) -> None:
        self._record_operator_action(
            action_type="ack",
            rule_id=acknowledgement.rule_id,
            silence_id=None,
            operator=acknowledgement.operator,
            reason=acknowledgement.reason,
            details={},
            created_at=acknowledgement.created_at,
        )

    def record_unacknowledgement(
        self,
        *,
        rule_id: str,
        operator: str,
        reason: str | None,
        created_at: datetime,
    ) -> None:
        self._record_operator_action(
            action_type="unack",
            rule_id=rule_id,
            silence_id=None,
            operator=operator,
            reason=reason,
            details={},
            created_at=created_at,
        )

    def record_rule_removed(
        self,
        *,
        rule_id: str,
        definition_file: str | None,
        previous_state: str,
        previous_severity: str | None,
        operator: str,
        reason: str,
        created_at: datetime,
        had_ack: bool,
        active_silence_ids: list[str],
    ) -> None:
        self._record_operator_action(
            action_type="rule_removed",
            rule_id=rule_id,
            silence_id=None,
            operator=operator,
            reason=reason,
            details={
                "definition_file": definition_file,
                "previous_state": previous_state,
                "previous_severity": previous_severity,
                "had_ack": had_ack,
                "active_silence_ids": list(active_silence_ids),
            },
            created_at=created_at,
        )

    def create_silence(self, silence: Silence) -> None:
        with self._transaction() as conn:
            conn.execute(
                """
                INSERT OR REPLACE INTO silences (
                    silence_id,
                    created_by,
                    reason,
                    created_at,
                    start_at,
                    end_at,
                    rule_patterns_json,
                    tags_json,
                    remote_silence_refs_json,
                    cancelled_at,
                    cancelled_by,
                    cancel_reason
                ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                """,
                (
                    silence.silence_id,
                    silence.created_by,
                    silence.reason,
                    silence.created_at.isoformat(),
                    silence.start_at.isoformat(),
                    silence.end_at.isoformat(),
                    json.dumps(list(silence.rule_patterns), ensure_ascii=False),
                    json.dumps(list(silence.tags), ensure_ascii=False),
                    json.dumps(list(silence.remote_silence_refs), ensure_ascii=False),
                    silence.cancelled_at.isoformat() if silence.cancelled_at else None,
                    silence.cancelled_by,
                    silence.cancel_reason,
                ),
            )
        self._record_operator_action(
            action_type="create_silence",
            rule_id=None,
            silence_id=silence.silence_id,
            operator=silence.created_by,
            reason=silence.reason,
            details={
                "start_at": silence.start_at.isoformat(),
                "end_at": silence.end_at.isoformat(),
                "rule_patterns": list(silence.rule_patterns),
                "tags": list(silence.tags),
                "remote_silence_refs": list(silence.remote_silence_refs),
            },
            created_at=silence.created_at,
        )

    def cancel_silence(self, silence: Silence) -> None:
        with self._transaction() as conn:
            conn.execute(
                """
                UPDATE silences
                SET cancelled_at = ?, cancelled_by = ?, cancel_reason = ?
                WHERE silence_id = ?
                """,
                (
                    silence.cancelled_at.isoformat() if silence.cancelled_at else None,
                    silence.cancelled_by,
                    silence.cancel_reason,
                    silence.silence_id,
                ),
            )
        self._record_operator_action(
            action_type="cancel_silence",
            rule_id=None,
            silence_id=silence.silence_id,
            operator=silence.cancelled_by or "",
            reason=silence.cancel_reason,
            details={
                "start_at": silence.start_at.isoformat(),
                "end_at": silence.end_at.isoformat(),
                "rule_patterns": list(silence.rule_patterns),
                "tags": list(silence.tags),
            },
            created_at=silence.cancelled_at or datetime.now().astimezone(),
        )

    def get_rule_history(
        self, rule_id: str, rule_tags: list[str] | None = None, *, limit: int | None = None
    ) -> dict[str, list[dict[str, Any]]]:
        """A rule's events, output dispatches, and operator actions, newest first; with
        limit, the newest ``limit`` of each (a rule that flaps for years has very many).

        Newest by time, not by the order the rows were written: a dispatch is written once
        every output is done, so a slow output's FIRING may be written after the ACKED."""
        cap = f" LIMIT {int(limit)}" if limit is not None else ""
        with self._reading() as conn:
            alert_events = [
                {
                    "rule_id": row["rule_id"],
                    "previous_state": row["previous_state"],
                    "current_state": row["current_state"],
                    "previous_severity": severity_name(row["previous_severity"]),
                    "current_severity": severity_name(row["current_severity"] if row["current_severity"] is not None else row["severity"]),
                    "transition": row["transition"],
                    "severity": severity_name(row["severity"]),
                    "owner": row["owner"],
                    "message": row["message"],
                    "payload": json.loads(row["payload_json"]),
                    "tags": json.loads(row["tags_json"]),
                    "occurred_at": row["occurred_at"],
                    "definition_file": row["definition_file"],
                    "matched_outputs": json.loads(row["matched_outputs_json"]),
                }
                for row in conn.execute(
                    """
                    SELECT *
                    FROM alert_events
                    WHERE rule_id = ?
                    ORDER BY julianday(occurred_at) DESC, id DESC
                    """ + cap,
                    (rule_id,),
                )
            ]
            output_dispatches = [
                _output_dispatch_row(row)
                for row in conn.execute(
                    """
                    SELECT *
                    FROM output_dispatches
                    WHERE rule_id = ?
                    ORDER BY julianday(occurred_at) DESC, id DESC
                    """ + cap,
                    (rule_id,),
                )
            ]
            operator_actions: list[dict[str, Any]] = []
            seen_action_ids: set[int] = set()
            for row in conn.execute(
                """
                SELECT *
                FROM operator_actions
                WHERE rule_id = ?
                ORDER BY julianday(created_at) DESC, id DESC
                """ + cap,
                (rule_id,),
            ):
                operator_actions.append(_operator_action_row(row))
                seen_action_ids.add(row["id"])

            for row in conn.execute(
                """
                SELECT oa.*, s.rule_patterns_json, s.tags_json
                FROM operator_actions AS oa
                JOIN silences AS s ON s.silence_id = oa.silence_id
                WHERE oa.silence_id IS NOT NULL
                ORDER BY oa.created_at DESC, oa.id DESC
                """
            ):
                if row["id"] in seen_action_ids:
                    continue
                silence_patterns = json.loads(row["rule_patterns_json"])
                silence_tags = json.loads(row["tags_json"])
                if not _silence_targets_rule(rule_id, rule_tags or [], silence_patterns, silence_tags):
                    continue
                operator_actions.append(_operator_action_row(row))
                seen_action_ids.add(row["id"])

            # Newest first by time, not by text: times are written with their own offsets
            # (an evaluation in local time, an operator action in UTC).
            alert_events.sort(key=lambda row: _parse_datetime(row["occurred_at"]), reverse=True)
            output_dispatches.sort(key=lambda row: _parse_datetime(row["occurred_at"]), reverse=True)
            operator_actions.sort(key=lambda row: _parse_datetime(row["created_at"]), reverse=True)
            if limit is not None:
                operator_actions = operator_actions[: int(limit)]  # with the silences that cover the rule
            return {
                "enabled": True,
                "alert_events": alert_events,
                "output_dispatches": output_dispatches,
                "operator_actions": operator_actions,
            }

    def get_alert_history(
        self,
        *,
        limit: int = 100,
        after_alert_id: int | None = None,
        after_action_id: int | None = None,
    ) -> dict[str, Any]:
        incremental = after_alert_id is not None or after_action_id is not None
        with self._reading() as conn:
            alert_where: list[str] = [
                "NOT (ae.previous_state IS NULL AND ae.current_state = 'OK')"
            ]
            alert_params: list[object] = []
            if incremental:
                alert_where.append("ae.id > ?")
                alert_params.append(after_alert_id or 0)
            alert_order = "ASC" if incremental else "DESC"
            alert_rows = list(
                conn.execute(
                    f"""
                SELECT
                    ae.id,
                    ae.rule_id,
                    ae.previous_state,
                    ae.current_state,
                    ae.previous_severity,
                    ae.current_severity,
                    ae.transition,
                    ae.severity,
                    ae.owner,
                    ae.message,
                    ae.tags_json,
                    ae.occurred_at,
                    ae.definition_file,
                    ae.matched_outputs_json
                FROM alert_events AS ae
                WHERE {' AND '.join(alert_where)}
                ORDER BY ae.id {alert_order}
                LIMIT ?
                """,
                    (*alert_params, limit + 1),
                )
            )

            action_where: list[str] = []
            action_params: list[object] = []
            if incremental:
                action_where.append("oa.id > ?")
                action_params.append(after_action_id or 0)
            action_order = "ASC" if incremental else "DESC"
            action_where_sql = f"WHERE {' AND '.join(action_where)}" if action_where else ""
            action_rows = list(
                conn.execute(
                    f"""
                SELECT oa.*
                FROM operator_actions AS oa
                {action_where_sql}
                ORDER BY oa.id {action_order}
                LIMIT ?
                """,
                    (*action_params, limit + 1),
                )
            )

            alert_has_more = len(alert_rows) > limit
            action_has_more = len(action_rows) > limit
            alert_rows = alert_rows[:limit]
            action_rows = action_rows[:limit]

            max_alert_id = int(
                conn.execute("SELECT COALESCE(MAX(id), 0) FROM alert_events").fetchone()[0]
            )
            max_action_id = int(
                conn.execute("SELECT COALESCE(MAX(id), 0) FROM operator_actions").fetchone()[0]
            )

            if incremental:
                latest_alert_id = max((row["id"] for row in alert_rows), default=after_alert_id or 0)
                latest_action_id = max((row["id"] for row in action_rows), default=after_action_id or 0)
                if not alert_has_more:
                    latest_alert_id = max(latest_alert_id, max_alert_id)
                if not action_has_more:
                    latest_action_id = max(latest_action_id, max_action_id)
            else:
                latest_alert_id = max_alert_id
                latest_action_id = max_action_id

        alert_entries = [
            {
                "kind": "alert_event",
                "id": row["id"],
                "occurred_at": row["occurred_at"],
                "rule_id": row["rule_id"],
                "previous_state": row["previous_state"],
                "current_state": row["current_state"],
                "transition": row["transition"],
                "severity": severity_name(row["severity"]),
                "previous_severity": severity_name(row["previous_severity"]),
                "current_severity": severity_name(row["current_severity"] if row["current_severity"] is not None else row["severity"]),
                "owner": row["owner"],
                "message": row["message"],
                "tags": json.loads(row["tags_json"]),
                "matched_outputs": json.loads(row["matched_outputs_json"]),
                "definition_file": row["definition_file"],
                "action_type": None,
                "operator": None,
                "reason": None,
            }
            for row in alert_rows
        ]
        action_entries = [
            {
                "kind": "operator_action",
                "id": row["id"],
                "occurred_at": row["created_at"],
                "action_type": row["action_type"],
                "rule_id": row["rule_id"],
                "silence_id": row["silence_id"],
                "operator": row["operator"],
                "reason": row["reason"],
                "details": json.loads(row["details_json"]),
            }
            for row in action_rows
        ]

        merged = sorted(
            alert_entries + action_entries,
            key=lambda entry: (_parse_datetime(entry["occurred_at"]), entry["id"]),
            reverse=True,
        )
        return {
            "enabled": True,
            "entries": merged,
            "latest_alert_id": latest_alert_id,
            "latest_action_id": latest_action_id,
            "alert_has_more": alert_has_more if incremental else False,
            "action_has_more": action_has_more if incremental else False,
            "initial_truncated": not incremental and (alert_has_more or action_has_more),
        }

    def _record_operator_action(
        self,
        *,
        action_type: str,
        rule_id: str | None,
        silence_id: str | None,
        operator: str,
        reason: str | None,
        details: dict[str, Any],
        created_at: datetime,
    ) -> None:
        with self._transaction() as conn:
            conn.execute(
                """
                INSERT INTO operator_actions (
                    action_type,
                    rule_id,
                    silence_id,
                    operator,
                    reason,
                    details_json,
                    created_at
                ) VALUES (?, ?, ?, ?, ?, ?, ?)
                """,
                (
                    action_type,
                    rule_id,
                    silence_id,
                    operator,
                    reason,
                    json.dumps(details, ensure_ascii=False),
                    created_at.isoformat(),
                ),
            )

    @contextmanager
    def _reading(self) -> Iterator[sqlite3.Connection]:
        """A connection of its own for a read: with WAL, its snapshot does not wait for the
        writes going on (nor for the store lock they hold). An in-memory database has only
        the one connection."""
        primary = self._require_conn()
        if str(self.path) == ":memory:":
            with self._lock:
                yield primary
            return
        conn = sqlite3.connect(self.path, check_same_thread=False)
        try:
            conn.row_factory = sqlite3.Row
            conn.execute("PRAGMA query_only=ON")
            conn.execute("BEGIN")  # one snapshot for every query of the read
            yield conn
        finally:
            conn.close()

    @contextmanager
    def _transaction(self) -> Iterator[sqlite3.Connection]:
        """One write: committed, or rolled back when it fails (the database locked by
        another process past the busy timeout, a full disk), so the next write starts clean."""
        conn = self._require_conn()
        with self._lock:
            try:
                yield conn
                conn.commit()
            except BaseException:
                conn.rollback()
                raise

    def _require_conn(self) -> sqlite3.Connection:
        if self._conn is None:
            raise RuntimeError("store is not initialized")
        return self._conn


def _has_table(conn: sqlite3.Connection, name: str) -> bool:
    return conn.execute("SELECT 1 FROM sqlite_master WHERE type = 'table' AND name = ?", (name,)).fetchone() is not None


def build_store(path: str | Path | None):
    if path is None:
        return NullStore()
    return SQLiteStore(path)


def _parse_datetime(value: str) -> datetime:
    return coerce_datetime(value)  # rows written before 0.8.0 may lack a time zone: local time


def _not_before(moment: datetime, other: datetime) -> bool:
    try:
        return moment >= other
    except TypeError:  # one with a zone, one without (an old database)
        return moment.replace(tzinfo=None) >= other.replace(tzinfo=None)


def _operator_action_row(row: sqlite3.Row) -> dict[str, Any]:
    return {
        "action_type": row["action_type"],
        "rule_id": row["rule_id"],
        "silence_id": row["silence_id"],
        "operator": row["operator"],
        "reason": row["reason"],
        "details": json.loads(row["details_json"]),
        "created_at": row["created_at"],
    }


def _output_dispatch_row(row: sqlite3.Row) -> dict[str, Any]:
    matched_outputs = json.loads(row["matched_outputs_json"])
    delivered_outputs = json.loads(row["delivered_outputs_json"])
    uninitialized_outputs = json.loads(row["uninitialized_outputs_json"])
    failed_outputs = json.loads(row["failed_outputs_json"])
    completed_outputs = set(delivered_outputs) | set(uninitialized_outputs) | set(failed_outputs)
    return {
        "rule_id": row["rule_id"],
        "previous_state": row["previous_state"],
        "current_state": row["current_state"],
        "occurred_at": row["occurred_at"],
        "matched_outputs": matched_outputs,
        "delivered_outputs": delivered_outputs,
        "emit_skipped_outputs": [
            output_id
            for output_id in matched_outputs
            if output_id not in completed_outputs
        ],
        "filtered_outputs": json.loads(row["filtered_outputs_json"]),
        "uninitialized_outputs": uninitialized_outputs,
        "failed_outputs": failed_outputs,
    }


def _silence_targets_rule(
    rule_id: str,
    rule_tags: list[str],
    silence_patterns: list[str],
    silence_tags: list[str],
) -> bool:
    from fnmatch import fnmatch

    return any(fnmatch(rule_id, pattern) for pattern in silence_patterns) or bool(set(rule_tags).intersection(silence_tags))
