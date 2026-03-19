from __future__ import annotations

from dataclasses import dataclass, field
from datetime import datetime
import json
from pathlib import Path
import sqlite3
import threading
from typing import Any

from .constants import AlertState, Severity
from .models import Acknowledgement, AlertEvent, Silence


@dataclass(slots=True)
class RestoredState:
    acknowledgements: dict[str, Acknowledgement] = field(default_factory=dict)
    silences: dict[str, Silence] = field(default_factory=dict)


class NullStore:
    enabled = False

    def initialize(self) -> None:
        return None

    def close(self) -> None:
        return None

    def load_runtime_state(self) -> RestoredState:
        return RestoredState()

    def append_alert_event(self, event: AlertEvent, *, definition_file: str | None, matched_outputs: list[str]) -> None:
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

    def create_silence(self, silence: Silence) -> None:
        return None

    def cancel_silence(self, silence: Silence) -> None:
        return None

    def get_rule_history(self, rule_id: str, rule_tags: list[str] | None = None) -> dict[str, list[dict[str, Any]]]:
        return {
            "enabled": False,
            "alert_events": [],
            "operator_actions": [],
        }


class SQLiteStore:
    enabled = True

    def __init__(self, path: str | Path) -> None:
        self.path = Path(path)
        self._lock = threading.RLock()
        self._conn: sqlite3.Connection | None = None

    def initialize(self) -> None:
        with self._lock:
            if self._conn is not None:
                return
            self.path.parent.mkdir(parents=True, exist_ok=True)
            conn = sqlite3.connect(self.path, check_same_thread=False)
            conn.row_factory = sqlite3.Row
            conn.execute("PRAGMA journal_mode=WAL")
            conn.execute("PRAGMA synchronous=NORMAL")
            conn.executescript(
                """
                CREATE TABLE IF NOT EXISTS alert_events (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    rule_id TEXT NOT NULL,
                    previous_state TEXT,
                    current_state TEXT NOT NULL,
                    severity INTEGER NOT NULL,
                    owner TEXT,
                    message TEXT,
                    payload_json TEXT NOT NULL,
                    tags_json TEXT NOT NULL,
                    occurred_at TEXT NOT NULL,
                    definition_file TEXT,
                    matched_outputs_json TEXT NOT NULL DEFAULT '[]'
                );

                CREATE INDEX IF NOT EXISTS idx_alert_events_rule_time
                    ON alert_events (rule_id, occurred_at DESC, id DESC);

                CREATE INDEX IF NOT EXISTS idx_alert_events_time
                    ON alert_events (occurred_at DESC, id DESC);

                CREATE TABLE IF NOT EXISTS operator_actions (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    action_type TEXT NOT NULL,
                    rule_id TEXT,
                    silence_id TEXT,
                    operator TEXT NOT NULL,
                    reason TEXT,
                    details_json TEXT NOT NULL DEFAULT '{}',
                    created_at TEXT NOT NULL
                );

                CREATE INDEX IF NOT EXISTS idx_operator_actions_rule_time
                    ON operator_actions (rule_id, created_at DESC, id DESC);

                CREATE INDEX IF NOT EXISTS idx_operator_actions_silence_time
                    ON operator_actions (silence_id, created_at DESC, id DESC);

                CREATE INDEX IF NOT EXISTS idx_operator_actions_time
                    ON operator_actions (created_at DESC, id DESC);

                CREATE TABLE IF NOT EXISTS silences (
                    silence_id TEXT PRIMARY KEY,
                    created_by TEXT NOT NULL,
                    reason TEXT,
                    created_at TEXT NOT NULL,
                    start_at TEXT NOT NULL,
                    end_at TEXT NOT NULL,
                    rule_patterns_json TEXT NOT NULL DEFAULT '[]',
                    tags_json TEXT NOT NULL DEFAULT '[]',
                    cancelled_at TEXT,
                    cancelled_by TEXT,
                    cancel_reason TEXT
                );

                CREATE INDEX IF NOT EXISTS idx_silences_window
                    ON silences (start_at, end_at);

                CREATE INDEX IF NOT EXISTS idx_silences_cancelled
                    ON silences (cancelled_at);
                """
            )
            conn.commit()
            self._conn = conn

    def close(self) -> None:
        with self._lock:
            if self._conn is None:
                return
            self._conn.close()
            self._conn = None

    def load_runtime_state(self) -> RestoredState:
        conn = self._require_conn()
        with self._lock:
            acknowledgements: dict[str, Acknowledgement] = {}
            acked_rule_ids = [
                row["rule_id"]
                for row in conn.execute(
                """
                SELECT ae.rule_id
                FROM alert_events AS ae
                JOIN (
                    SELECT rule_id, MAX(id) AS max_id
                    FROM alert_events
                    GROUP BY rule_id
                ) latest ON latest.max_id = ae.id
                WHERE ae.current_state = ?
                """,
                (AlertState.ACKED.value,),
                )
            ]
            for rule_id in acked_rule_ids:
                row = conn.execute(
                    """
                    SELECT operator, reason, created_at
                    FROM operator_actions
                    WHERE action_type = 'ack' AND rule_id = ?
                    ORDER BY id DESC
                    LIMIT 1
                    """,
                    (rule_id,),
                ).fetchone()
                if row is None:
                    continue
                acknowledgements[rule_id] = Acknowledgement(
                    rule_id=rule_id,
                    operator=row["operator"],
                    reason=row["reason"],
                    created_at=_parse_datetime(row["created_at"]),
                )

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
                    cancelled_at=_parse_datetime(row["cancelled_at"]) if row["cancelled_at"] else None,
                    cancelled_by=row["cancelled_by"],
                    cancel_reason=row["cancel_reason"],
                )

            return RestoredState(acknowledgements=acknowledgements, silences=silences)

    def append_alert_event(self, event: AlertEvent, *, definition_file: str | None, matched_outputs: list[str]) -> None:
        conn = self._require_conn()
        with self._lock:
            conn.execute(
                """
                INSERT INTO alert_events (
                    rule_id,
                    previous_state,
                    current_state,
                    severity,
                    owner,
                    message,
                    payload_json,
                    tags_json,
                    occurred_at,
                    definition_file,
                    matched_outputs_json
                ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                """,
                (
                    event.rule_id,
                    event.previous_state.value if event.previous_state is not None else None,
                    event.current_state.value,
                    int(event.alert.severity),
                    event.alert.owner,
                    event.alert.message,
                    json.dumps(event.alert.payload, ensure_ascii=False, default=_json_default),
                    json.dumps(list(event.alert.tags), ensure_ascii=False),
                    event.occurred_at.isoformat(),
                    definition_file,
                    json.dumps(matched_outputs, ensure_ascii=False),
                ),
            )
            conn.commit()

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

    def create_silence(self, silence: Silence) -> None:
        conn = self._require_conn()
        with self._lock:
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
                    cancelled_at,
                    cancelled_by,
                    cancel_reason
                ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
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
                    silence.cancelled_at.isoformat() if silence.cancelled_at else None,
                    silence.cancelled_by,
                    silence.cancel_reason,
                ),
            )
            conn.commit()
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
            },
            created_at=silence.created_at,
        )

    def cancel_silence(self, silence: Silence) -> None:
        conn = self._require_conn()
        with self._lock:
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
            conn.commit()
        self._record_operator_action(
            action_type="cancel_silence",
            rule_id=None,
            silence_id=silence.silence_id,
            operator=silence.cancelled_by or "",
            reason=silence.cancel_reason,
            details={},
            created_at=silence.cancelled_at or datetime.now().astimezone(),
        )

    def get_rule_history(self, rule_id: str, rule_tags: list[str] | None = None) -> dict[str, list[dict[str, Any]]]:
        conn = self._require_conn()
        with self._lock:
            alert_events = [
                {
                    "rule_id": row["rule_id"],
                    "previous_state": row["previous_state"],
                    "current_state": row["current_state"],
                    "severity": row["severity"],
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
                    ORDER BY occurred_at DESC, id DESC
                    """,
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
                ORDER BY created_at DESC, id DESC
                """,
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

            operator_actions.sort(key=lambda row: row["created_at"], reverse=True)
            return {
                "enabled": True,
                "alert_events": alert_events,
                "operator_actions": operator_actions,
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
        conn = self._require_conn()
        with self._lock:
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
            conn.commit()

    def _require_conn(self) -> sqlite3.Connection:
        if self._conn is None:
            raise RuntimeError("store is not initialized")
        return self._conn


def build_store(path: str | Path | None):
    if path is None:
        return NullStore()
    return SQLiteStore(path)


def _parse_datetime(value: str) -> datetime:
    return datetime.fromisoformat(value)


def _json_default(value: object) -> str:
    isoformat = getattr(value, "isoformat", None)
    if callable(isoformat):
        return isoformat()
    raise TypeError(f"Object of type {type(value).__name__} is not JSON serializable")


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


def _silence_targets_rule(
    rule_id: str,
    rule_tags: list[str],
    silence_patterns: list[str],
    silence_tags: list[str],
) -> bool:
    from fnmatch import fnmatch

    return any(fnmatch(rule_id, pattern) for pattern in silence_patterns) or bool(set(rule_tags).intersection(silence_tags))
