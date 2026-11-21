from __future__ import annotations

import argparse
import difflib
from fnmatch import fnmatchcase
from importlib import metadata as importlib_metadata
import json
import os
from pathlib import Path
import shutil
import sys
import textwrap
from datetime import datetime, timezone
import tomllib
import unicodedata
from urllib.error import HTTPError, URLError
from urllib.parse import quote
from urllib.request import Request, urlopen

SEVERITY_LABELS = {
    10: "INFO",
    20: "WARN",
    30: "ERROR",
    40: "CRITICAL",
}

ANSI_RESET = "\033[0m"
ANSI_COLORS = {
    "INFO": "\033[36m",
    "WARN": "\033[33m",
    "ERROR": "\033[31m",
    "CRITICAL": "\033[35m",
    "OK": "\033[32m",
    "FIRING": "\033[31m",
    "ACKED": "\033[34m",
    "SILENCED": "\033[36m",
    "SUPPRESSED": "\033[90m",
    "WAITING": "\033[90m",
}

PROJECT_ROOT = Path(__file__).resolve().parents[2]


def _kanary_version() -> str:
    try:
        return importlib_metadata.version("kanary")
    except importlib_metadata.PackageNotFoundError:
        try:
            with (PROJECT_ROOT / "pyproject.toml").open("rb") as handle:
                project = tomllib.load(handle).get("project", {})
        except OSError:
            return "unknown"
        version = project.get("version")
        return str(version) if version else "unknown"


def main() -> int:
    parser = argparse.ArgumentParser(description="Inspect and control a running KANARY instance")
    parser.add_argument(
        "--version",
        action="version",
        version=f"%(prog)s {_kanary_version()}",
    )
    parser.add_argument(
        "--base-url",
        default=os.environ.get("KANARY_API_URL", "http://127.0.0.1:8000"),
        help="KANARY API base URL",
    )
    subparsers = parser.add_subparsers(dest="command", required=True)

    subparsers.add_parser("help", help="Show this help message")
    subparsers.add_parser("health", help="Show runtime health")

    alerts_parser = subparsers.add_parser("alerts", help="Show current alerts, or one in full")
    alerts_parser.add_argument(
        "rule_id", nargs="?", metavar="RULE_ID",
        help="Show this alert in full, with its recent history (a glob such as 'oil.*' shows each match)",
    )
    alerts_parser.add_argument("--json", action="store_true", help="Print raw JSON")
    alerts_parser.add_argument("--filter", help="Filter alerts by text or glob")
    alerts_parser.add_argument("--wide", action="store_true", help="Show every column and cut nothing")

    history_parser = subparsers.add_parser("history", help="Show persisted alert history for a rule")
    history_parser.add_argument("rule_id")
    history_parser.add_argument("--json", action="store_true", help="Print raw JSON")
    history_parser.add_argument("--limit", type=int, help="Show only the most recent N entries in each history section")
    history_parser.add_argument("--since", help="Show only entries at or after this ISO timestamp")

    plugins_parser = subparsers.add_parser("plugins", help="Show source/rule/output plugin status, or one in full")
    plugins_parser.add_argument(
        "plugin_id", nargs="?", metavar="PLUGIN_ID",
        help="Show this plugin in full (a glob such as 'check.*' shows each match)",
    )
    plugins_parser.add_argument("--json", action="store_true", help="Print raw JSON")
    plugins_parser.add_argument("--filter", help="Filter plugins by text or glob")
    plugins_parser.add_argument("--wide", action="store_true", help="Show every column and cut nothing")
    plugins_parser.add_argument("--traceback", action="store_true", help="With PLUGIN_ID: also print where the last error happened")

    silences_parser = subparsers.add_parser("silences", help="Show configured silences")
    silences_parser.add_argument("--json", action="store_true", help="Print raw JSON")
    silences_parser.add_argument("--filter", help="Filter silences by text or glob")
    silences_parser.add_argument("--limit", type=int, help="Show only the most recent N silences")
    silences_parser.add_argument("--since", help="Show only silences at or after this ISO timestamp")

    ack_parser = subparsers.add_parser("ack", help="Acknowledge an alert")
    ack_parser.add_argument("rule_id")
    ack_parser.add_argument("--operator", required=True)
    ack_parser.add_argument("--reason")

    unack_parser = subparsers.add_parser("unack", help="Remove acknowledgement from an alert")
    unack_parser.add_argument("rule_id")
    unack_parser.add_argument("--operator", required=True)
    unack_parser.add_argument("--reason")

    silence_for_parser = subparsers.add_parser("silence-for", help="Create a silence for a duration")
    silence_for_parser.add_argument("--operator", required=True)
    silence_for_parser.add_argument("--minutes", required=True, type=float)
    silence_for_parser.add_argument("--start-at")
    silence_for_parser.add_argument("--rule", action="append", default=[])
    silence_for_parser.add_argument("--tag", action="append", default=[])
    silence_for_parser.add_argument("--reason")

    silence_until_parser = subparsers.add_parser("silence-until", help="Create a silence for a time window")
    silence_until_parser.add_argument("--operator", required=True)
    silence_until_parser.add_argument("--start-at", required=True)
    silence_until_parser.add_argument("--end-at", required=True)
    silence_until_parser.add_argument("--rule", action="append", default=[])
    silence_until_parser.add_argument("--tag", action="append", default=[])
    silence_until_parser.add_argument("--reason")

    unsilence_parser = subparsers.add_parser("unsilence", help="Cancel an existing silence")
    unsilence_parser.add_argument("silence_id")
    unsilence_parser.add_argument("--operator", required=True)
    unsilence_parser.add_argument("--reason")

    reload_parser = subparsers.add_parser("reload", help="Apply discovered plugin changes or restart the engine")
    reload_group = reload_parser.add_mutually_exclusive_group(required=True)
    reload_group.add_argument("--rule")
    reload_group.add_argument("--source")
    reload_group.add_argument("--output")
    reload_group.add_argument("--dirty", action="store_true")
    reload_group.add_argument("--all", action="store_true")
    reload_group.add_argument("--full", action="store_true")

    test_poll_parser = subparsers.add_parser("test-poll", help="Poll one source and print the normalized payload")
    test_poll_parser.add_argument("source_id")
    test_poll_parser.add_argument("--json", action="store_true", help="Print raw JSON")

    test_evaluate_parser = subparsers.add_parser("test-evaluate", help="Dry-run one rule against a payload")
    test_evaluate_parser.add_argument("rule_id")
    payload_group = test_evaluate_parser.add_mutually_exclusive_group(required=False)
    payload_group.add_argument("--payload-file")
    payload_group.add_argument("--payload-json")
    payload_group.add_argument("--payload-stdin", action="store_true")
    payload_group.add_argument(
        "--print-template",
        action="store_true",
        help="Print a canonical inputs-based payload template for this rule and exit",
    )
    test_evaluate_parser.add_argument("--now")
    test_evaluate_parser.add_argument("--json", action="store_true", help="Print raw JSON")

    test_fire_parser = subparsers.add_parser("test-fire", help="Send a synthetic event through the output pipeline")
    test_fire_parser.add_argument("rule_id")
    fire_input = test_fire_parser.add_mutually_exclusive_group(required=True)
    fire_input.add_argument("--state", help="The state of the event: FIRING, OK, ...")
    fire_input.add_argument(
        "--evaluation-file", metavar="PATH",
        help="Send what test-evaluate judged (its output, saved to a file): state, severity, message, values",
    )
    fire_input.add_argument(
        "--evaluation-stdin", action="store_true",
        help="The same from stdin: kanaryctl test-evaluate RULE ... | kanaryctl test-fire RULE --evaluation-stdin",
    )
    test_fire_parser.add_argument("--message")
    test_fire_parser.add_argument("--reason")
    test_fire_parser.add_argument("--now")
    test_fire_parser.add_argument("--json", action="store_true", help="Print raw JSON")

    args = parser.parse_args()

    try:
        if args.command == "help":
            parser.print_help()
            return 0

        if args.command == "health":
            payload = fetch_json(f"{args.base_url}/health")
            print_health(payload)
            return 0

        if args.command == "alerts":
            alerts = fetch_json(f"{args.base_url}/alerts").get("alerts", [])
            plugins = fetch_json(f"{args.base_url}/plugins").get("plugins", [])
            rows = alerts + waiting_rule_rows(alerts, plugins)
            if args.filter:
                rows = [alert for alert in rows if matches_row_filter(
                    [
                        alert.get("rule_id"),
                        alert.get("state"),
                        alert.get("message"),
                        alert.get("acked_by"),
                        alert.get("owner"),
                        " ".join(alert.get("tags", [])),
                        " ".join(alert.get("matched_outputs", [])),
                    ],
                    args.filter,
                )]
            if args.rule_id:
                matches = select_by_id(rows, "rule_id", args.rule_id)
                if not matches:
                    return report_unknown_id("alert", args.rule_id, [row["rule_id"] for row in rows])
                if args.json:
                    print(json.dumps({"alerts": matches}, ensure_ascii=False, indent=2))
                    return 0
                for index, alert in enumerate(matches):
                    if index:
                        print()
                    history = fetch_json(f"{args.base_url}/history/{quote(alert['rule_id'], safe='')}")
                    print_alert_detail(alert, apply_history_filters(history, since=None, limit=5))
                return 0
            if args.json:  # the API's alerts as they are: a rule without one has no row here
                print(json.dumps({"alerts": [row for row in rows if row["state"] != WAITING]}, ensure_ascii=False, indent=2))
            else:
                print_alerts({"alerts": rows}, width=None if args.wide else terminal_width(), wide=args.wide)
            return 0

        if args.command == "history":
            payload = fetch_json(f"{args.base_url}/history/{args.rule_id}")
            payload = apply_history_filters(payload, since=args.since, limit=args.limit)
            if args.json:
                print(json.dumps(payload, ensure_ascii=False, indent=2))
            else:
                print_history(payload)
            return 0

        if args.command == "plugins":
            payload = fetch_json(f"{args.base_url}/plugins")
            if args.filter:
                payload["plugins"] = [plugin for plugin in payload.get("plugins", []) if matches_row_filter(
                    [
                        plugin.get("type"),
                        plugin.get("plugin_id"),
                        plugin.get("state"),
                        plugin.get("definition_file"),
                        plugin.get("last_error"),
                    ],
                    args.filter,
                )]
            if args.plugin_id:
                plugins = payload.get("plugins", [])
                matches = select_by_id(plugins, "plugin_id", args.plugin_id)
                if not matches:
                    return report_unknown_id("plugin", args.plugin_id, [plugin["plugin_id"] for plugin in plugins])
                if args.json:
                    print(json.dumps({"plugins": matches}, ensure_ascii=False, indent=2))
                    return 0
                for index, plugin in enumerate(matches):
                    if index:
                        print()
                    print_plugin_detail(plugin, traceback=args.traceback)
                return 0
            if args.json:
                print(json.dumps(payload, ensure_ascii=False, indent=2))
            else:
                print_plugins(payload, width=None if args.wide else terminal_width(), wide=args.wide)
            return 0

        if args.command == "silences":
            payload = fetch_json(f"{args.base_url}/silences")
            if args.filter:
                payload["silences"] = [silence for silence in payload.get("silences", []) if matches_row_filter(
                    [
                        silence.get("silence_id"),
                        silence.get("created_by"),
                        silence.get("reason"),
                        " ".join(silence.get("rule_patterns", [])),
                        " ".join(silence.get("tags", [])),
                    ],
                    args.filter,
                )]
            payload = apply_silence_filters(payload, since=args.since, limit=args.limit)
            if args.json:
                print(json.dumps(payload, ensure_ascii=False, indent=2))
            else:
                print_silences(payload)
            return 0

        if args.command == "ack":
            payload = fetch_json(
                f"{args.base_url}/alerts/{args.rule_id}/ack",
                method="POST",
                body={"operator": args.operator, "reason": args.reason},
            )
            print(payload.get("status", "unknown"))
            return 0

        if args.command == "unack":
            payload = fetch_json(
                f"{args.base_url}/alerts/{args.rule_id}/unack",
                method="POST",
                body={"operator": args.operator, "reason": args.reason},
            )
            print(payload.get("status", "unknown"))
            return 0

        if args.command == "silence-for":
            payload = fetch_json(
                f"{args.base_url}/silences/duration",
                method="POST",
                body={
                    "operator": args.operator,
                    "reason": args.reason,
                    "duration_minutes": args.minutes,
                    "start_at": args.start_at,
                    "rule_patterns": args.rule,
                    "tags": args.tag,
                },
            )
            for warning in payload.get("warnings", []):
                print(f"warning: {warning}", file=sys.stderr)
            print(payload.get("silence_id", payload.get("status", "unknown")))
            return 0

        if args.command == "silence-until":
            payload = fetch_json(
                f"{args.base_url}/silences/window",
                method="POST",
                body={
                    "operator": args.operator,
                    "reason": args.reason,
                    "start_at": args.start_at,
                    "end_at": args.end_at,
                    "rule_patterns": args.rule,
                    "tags": args.tag,
                },
            )
            for warning in payload.get("warnings", []):
                print(f"warning: {warning}", file=sys.stderr)
            print(payload.get("silence_id", payload.get("status", "unknown")))
            return 0

        if args.command == "unsilence":
            payload = fetch_json(
                f"{args.base_url}/silences/{args.silence_id}/cancel",
                method="POST",
                body={"operator": args.operator, "reason": args.reason},
            )
            print(payload.get("status", "unknown"))
            return 0

        if args.command == "reload":
            payload = fetch_json(
                f"{args.base_url}/reload",
                method="POST",
                body={
                    "rule": args.rule,
                    "source": args.source,
                    "output": args.output,
                    "dirty": args.dirty,
                    "all": args.all,
                    "full": args.full,
                },
            )
            print(payload.get("status", "unknown"))
            return 0

        if args.command == "test-poll":
            payload = fetch_json(
                f"{args.base_url}/test-poll/{args.source_id}",
                method="POST",
            )
            print(json.dumps(payload, ensure_ascii=False, indent=2))
            return 0

        if args.command == "test-evaluate":
            if args.print_template:
                payload = fetch_json(
                    f"{args.base_url}/test-evaluate-template/{args.rule_id}",
                )
                print(json.dumps(payload, ensure_ascii=False, indent=2))
                return 0
            payload = fetch_json(
                f"{args.base_url}/test-evaluate/{args.rule_id}",
                method="POST",
                body={
                    "payload": load_payload_argument(args),
                    "now": args.now,
                },
            )
            print(json.dumps(payload, ensure_ascii=False, indent=2))
            return 0

        if args.command == "test-fire":
            evaluation = None
            if args.evaluation_file:
                evaluation = _parse_json_object(Path(args.evaluation_file).read_text(encoding="utf-8"), source=args.evaluation_file)
            elif args.evaluation_stdin:
                evaluation = _parse_json_object(sys.stdin.read(), source="stdin")
            payload = fetch_json(
                f"{args.base_url}/test-fire/{args.rule_id}",
                method="POST",
                body={
                    "state": args.state,
                    "evaluation": evaluation,
                    "message": args.message,
                    "reason": args.reason,
                    "now": args.now,
                },
            )
            print(json.dumps(payload, ensure_ascii=False, indent=2))
            return 0
    except HTTPError as exc:
        print(f"kanaryctl: {exc}{_error_detail(exc)}")
        return 1
    except URLError as exc:
        print(f"kanaryctl: {exc}")
        return 1
    except ValueError as exc:
        print(f"kanaryctl: {exc}")
        return 1

    return 1


def fetch_json(url: str, method: str = "GET", body: dict | None = None) -> dict:
    data = None if body is None else json.dumps({key: value for key, value in body.items() if value is not None}).encode()
    request = Request(url, method=method, data=data)
    if data is not None:
        request.add_header("Content-Type", "application/json")
    with urlopen(request) as response:
        return json.loads(response.read().decode())


def load_payload_argument(args: argparse.Namespace) -> dict:
    if getattr(args, "payload_json", None):
        return _parse_json_object(args.payload_json, source="--payload-json")
    if getattr(args, "payload_file", None):
        return _parse_json_object(Path(args.payload_file).read_text(encoding="utf-8"), source=args.payload_file)
    if getattr(args, "payload_stdin", False):
        return _parse_json_object(sys.stdin.read(), source="stdin")
    raise ValueError(
        "one payload input is required unless --print-template is used "
        "(choose one of --payload-json, --payload-file, or --payload-stdin)"
    )


def _parse_json_object(raw: str, *, source: str) -> dict:
    try:
        parsed = json.loads(raw)
    except json.JSONDecodeError as exc:
        raise ValueError(f"invalid JSON from {source}: {exc}") from exc
    if not isinstance(parsed, dict):
        raise ValueError(f"payload from {source} must be a JSON object")
    return parsed


def matches_row_filter(values: list[object], pattern: str) -> bool:
    normalized_pattern = str(pattern or "").strip().lower()
    if not normalized_pattern:
        return True
    candidates = [str(value or "").lower() for value in values]
    if any(char in normalized_pattern for char in "*?["):
        from fnmatch import fnmatch

        return any(fnmatch(candidate, normalized_pattern) for candidate in candidates)
    return any(normalized_pattern in candidate for candidate in candidates)


def print_health(payload: dict) -> None:
    print(f"status      {payload.get('status')}")
    print(f"sources     {', '.join(payload.get('sources', [])) or '-'}")
    print(f"rules       {len(payload.get('rules', []))}")
    print(f"alert_count {payload.get('alert_count', 0)}")


WAITING = "WAITING"  # a rule with no alert yet: not evaluated since KANARY started (and none stored)
RESTORED_NOTE = "* restored: the state KANARY stopped with; not evaluated since it started"
# Alerting first, as the viewer lists them.
STATE_ORDER = {"FIRING": 0, "ACKED": 1, "SILENCED": 2, "SUPPRESSED": 3, "OK": 4, WAITING: 5}


def waiting_rule_rows(alerts: list[dict], plugins: list[dict]) -> list[dict]:
    """A row for each rule without an alert, so that every rule is listed."""
    known = {alert["rule_id"] for alert in alerts}
    rows = []
    for plugin in plugins:
        if plugin.get("type") != "rule" or plugin["plugin_id"] in known:
            continue
        failed = plugin.get("state") == "FAILED" and plugin.get("last_error")
        rows.append({
            "rule_id": plugin["plugin_id"],
            "state": WAITING,
            "severity": plugin.get("severity"),
            "owner": plugin.get("owner"),
            "tags": list(plugin.get("tags") or []),
            "message": f"evaluate() failed: {plugin['last_error']}" if failed else "not evaluated yet",
            "matched_outputs": list(plugin.get("matched_outputs") or []),
            "inputs": list(plugin.get("inputs") or []),
            "description": plugin.get("description"),
            "runbook": plugin.get("runbook"),
            "definition_file": plugin.get("definition_file"),
            "acked_by": None,
            "active_silence_ids": [],
            "payload": {},
        })
    return rows


def select_by_id(rows: list[dict], key: str, pattern: str) -> list[dict]:
    """The rows whose id is ``pattern``, or matches it when it is a glob."""
    if any(char in pattern for char in "*?["):
        return [row for row in rows if fnmatchcase(row[key], pattern)]
    return [row for row in rows if row[key] == pattern]


def report_unknown_id(kind: str, pattern: str, known: list[str]) -> int:
    close = difflib.get_close_matches(pattern, known, n=3, cutoff=0.8)
    hint = f"; did you mean {' or '.join(repr(item) for item in close)}?" if close else ""
    print(f"kanaryctl: no {kind} {pattern!r}{hint}")
    return 1


def _error_detail(exc: HTTPError) -> str:
    """The API's own reason for an error answer, when it gave one."""
    try:
        body = json.loads(exc.read().decode() or "{}")
    except (ValueError, OSError):
        return ""
    error = body.get("error") if isinstance(body, dict) else None
    return f": {error}" if error else ""


# -- tables -------------------------------------------------------------------

def terminal_width() -> int | None:
    """Columns to fit in, or None when the output is not a terminal (piped: cut nothing)."""
    if not sys.stdout.isatty():
        return None
    return shutil.get_terminal_size((120, 24)).columns


def text_width(text: str) -> int:
    """Columns the text takes in a terminal: wide (CJK) characters take two."""
    return sum(
        0 if unicodedata.combining(char) else 2 if unicodedata.east_asian_width(char) in "WF" else 1
        for char in text
    )


def cut_text(text: str, width: int) -> str:
    """text in at most ``width`` columns, ending in "…" when cut."""
    if text_width(text) <= width:
        return text
    kept, used = [], 0
    for char in text:
        size = text_width(char)
        if used + size > width - 1:
            break
        kept.append(char)
        used += size
    return "".join(kept) + "…"


def render_table(headers: list[str], rows: list[list[tuple[str, str]]], *, colored: bool, width: int | None) -> list[str]:
    """Lines of a table; each cell is (text, color key). The last column is cut to ``width``."""
    widths = [max(text_width(header), *(text_width(row[index][0]) for row in rows)) for index, header in enumerate(headers[:-1])]
    used = sum(widths) + 2 * len(widths)
    last_width = None if width is None else max(20, width - used)

    def line(cells: list[tuple[str, str]]) -> str:
        parts = []
        for (text, key), column_width in zip(cells[:-1], widths):
            parts.append(colorize(text, key, colored=colored) + " " * (column_width - text_width(text)))
        last = " ".join(cells[-1][0].split()) if last_width is not None else cells[-1][0]
        parts.append(colorize(cut_text(last, last_width) if last_width is not None else last, cells[-1][1], colored=colored))
        return "  ".join(parts).rstrip()

    header_line = line([(header, "") for header in headers])
    rule_width = text_width(header_line) if width is None else min(width, max(text_width(header_line), used + 20))
    return [header_line, "-" * rule_width, *(line(row) for row in rows)]


def short_time(value: str | None, *, now: datetime | None = None) -> str:
    """Local time: "13:45:10" today, "11-20 13:45" another day, "-" when unknown."""
    moment = parse_iso_datetime(value)
    if moment is None:
        return "-"
    local = moment.astimezone()
    today = (now or datetime.now(timezone.utc)).astimezone().date()
    return local.strftime("%H:%M:%S") if local.date() == today else local.strftime("%m-%d %H:%M")


def print_alerts(payload: dict, *, width: int | None = None, wide: bool = False) -> None:
    alerts = payload.get("alerts", [])
    if not alerts:
        print("no alerts")
        return

    headers = ["RULE ID", "STATE", "SEV", "ACKED BY", "SILENCES", *(["OUTPUTS", "FILE"] if wide else []), "MESSAGE"]
    rows = []
    for alert in sorted(alerts, key=lambda row: (STATE_ORDER.get(row["state"], len(STATE_ORDER)), row["rule_id"])):
        state = alert["state"]
        severity = severity_label(alert.get("severity"))
        cells = [
            (alert["rule_id"], ""),
            (state + ("*" if alert.get("restored") else ""), state),
            (severity, severity if state not in ("OK", WAITING) else ""),
            (alert.get("acked_by") or "-", ""),
            (", ".join(alert.get("active_silence_ids", [])) or "-", ""),
        ]
        if wide:
            cells.append((", ".join(alert.get("matched_outputs", [])) or "-", ""))
            cells.append((alert.get("definition_file") or "-", ""))
        cells.append((alert.get("message") or "", ""))
        rows.append(cells)
    for line in render_table(headers, rows, colored=sys.stdout.isatty(), width=width):
        print(line)
    if any(alert.get("restored") for alert in alerts):
        print(RESTORED_NOTE)


def print_plugins(payload: dict, *, width: int | None = None, wide: bool = False) -> None:
    plugins = payload.get("plugins", [])
    if not plugins:
        print("no plugins")
        return

    headers = ["TYPE", "PLUGIN", "STATE", "RUNS", "UPDATED", *(["FILE"] if wide else []), "LAST ERROR"]
    run_width = max(len("RUNS"), *(len(str(plugin["run_count"])) for plugin in plugins))
    rows = []
    for plugin in plugins:
        state = plugin["state"]
        cells = [
            (plugin["type"], ""),
            (plugin["plugin_id"], ""),
            (state, state.upper()),
            (f"{plugin['run_count']:>{run_width}}", ""),
            (short_time(plugin.get("last_updated_at")), ""),
        ]
        if wide:
            cells.append((plugin.get("definition_file") or "-", ""))
        cells.append((plugin.get("last_error") or "", ""))
        rows.append(cells)
    for line in render_table(headers, rows, colored=sys.stdout.isatty(), width=width):
        print(line)


# -- one item in full ------------------------------------------------------------

def _print_fields(fields: list[tuple[str, object]]) -> None:
    """"  label   value" lines; a value that is a list takes a line per item. Long lines wrap."""
    width = terminal_width()
    label_width = max(len(label) for label, _ in fields)
    indent = " " * (2 + label_width + 2)
    for label, value in fields:
        items = [str(item) for item in value] if isinstance(value, list) else str(value).splitlines() or [""]
        for index, item in enumerate(items or ["-"]):
            head = f"  {label:<{label_width}}  " if index == 0 else indent
            if width is None or text_width(head + item) <= width:
                print(head + item)
                continue
            wrapped = textwrap.wrap(item, width=max(20, width - len(indent))) or [item]
            print(head + wrapped[0])
            for rest in wrapped[1:]:
                print(indent + rest)


def _when(value: str | None) -> str:
    """"2026-11-21 13:45:10 (12 min ago)" in local time."""
    moment = parse_iso_datetime(value)
    if moment is None:
        return "-"
    seconds = (datetime.now(timezone.utc) - moment).total_seconds()
    if seconds < 0:
        ago = "from now"
    elif seconds < 60:
        ago = f"{int(seconds)} s ago"
    elif seconds < 3600:
        ago = f"{int(seconds // 60)} min ago"
    elif seconds < 86400:
        ago = f"{seconds / 3600:.1f} h ago"
    else:
        ago = f"{seconds / 86400:.1f} days ago"
    return f"{moment.astimezone().strftime('%Y-%m-%d %H:%M:%S')} ({ago})"


def print_alert_detail(alert: dict, history: dict | None = None) -> None:
    state = alert["state"]
    colored = sys.stdout.isatty()
    severity = severity_label(alert.get("severity"))
    print(f"{alert['rule_id']}  {colorize(state, state, colored=colored)}  "
          f"{colorize(severity, severity if state not in ('OK', WAITING) else '', colored=colored)}")
    fields: list[tuple[str, object]] = [("message", alert.get("message") or "-")]
    if alert.get("restored"):
        fields.append(("restored", "the state KANARY stopped with; not evaluated since it started"))
    if state not in ("OK", WAITING):
        fields.append(("active since", _when(alert.get("active_since"))))
    if alert.get("acked_by"):
        reason = f" ({alert['ack_reason']})" if alert.get("ack_reason") else ""
        fields.append(("acked by", f"{alert['acked_by']} at {short_time(alert.get('acked_at'))}{reason}"))
    silences = alert.get("active_silences") or []
    if silences:
        fields.append(("silences", [
            f"{silence['silence_id']} until {short_time(silence.get('end_at'))} by {silence.get('created_by') or '-'}"
            + (f": {silence['reason']}" if silence.get("reason") else "")
            for silence in silences
        ]))
    values = (alert.get("payload") or {}).get("values")
    if isinstance(values, dict) and values:
        fields.append(("values", [f"{name} = {value}" for name, value in values.items()]))
    if state == WAITING and alert.get("inputs"):
        fields.append(("inputs", ", ".join(alert["inputs"])))
    fields.append(("tags", ", ".join(alert.get("tags", [])) or "-"))
    if alert.get("owner"):
        fields.append(("owner", alert["owner"]))
    fields.append(("outputs", ", ".join(alert.get("matched_outputs", [])) or "-"))
    for key in ("description", "runbook"):
        if alert.get(key):
            fields.append((key, alert[key]))
    fields.append(("file", alert.get("definition_file") or "-"))
    if state != WAITING:
        evaluated = alert.get("last_evaluated_at")
        fields.append(("evaluated", "not since the start" if alert.get("restored") else short_time(evaluated)))
    _print_fields(fields)

    events = (history or {}).get("alert_events") or []
    if events:
        print(f"  history (newest {len(events)})")
        for event in events:
            line = (
                f"    {short_time(event.get('occurred_at'))}  {event.get('previous_state') or '-'} -> "
                f"{event['current_state']}  {severity_label(event.get('current_severity', event.get('severity')))}  "
                f"{event.get('message') or ''}"
            ).rstrip()
            width = terminal_width()
            print(cut_text(line, width) if width else line)


def print_plugin_detail(plugin: dict, *, traceback: bool = False) -> None:
    state = plugin["state"]
    print(f"{plugin['type']} {plugin['plugin_id']}  {colorize(state, state.upper(), colored=sys.stdout.isatty())}")
    fields: list[tuple[str, object]] = []
    if plugin.get("description"):
        fields.append(("description", plugin["description"]))
    fields.append(("runs", (
        f"{plugin['run_count']} (last {short_time(plugin.get('last_run_at'))}, "
        f"success {short_time(plugin.get('last_success_at'))}, failure {short_time(plugin.get('last_failure_at'))})"
    )))
    fields.append(("last error", plugin.get("last_error") or "-"))
    if plugin.get("warning"):
        fields.append(("warning", plugin["warning"]))
    if plugin.get("dirty_reason"):
        fields.append(("changed", plugin["dirty_reason"]))
    if plugin["type"] == "rule":
        fields.append(("severity", severity_label(plugin.get("severity"))))
        fields.append(("inputs", ", ".join(plugin.get("inputs") or []) or "-"))
        fields.append(("sources", ", ".join(plugin.get("resolved_sources") or []) or "-"))
        fields.append(("outputs", ", ".join(plugin.get("matched_outputs") or []) or "-"))
        fields.append(("tags", ", ".join(plugin.get("tags") or []) or "-"))
        for key in ("owner", "runbook"):
            if plugin.get(key):
                fields.append((key, plugin[key]))
    elif plugin["type"] == "output":
        routing = []
        for key, label in (("include_tags", "include tags"), ("exclude_tags", "exclude tags"),
                           ("exclude_states", "exclude states"), ("exclude_transitions", "exclude transitions")):
            if plugin.get(key):
                routing.append(f"{label}: {', '.join(plugin[key])}")
        if plugin.get("minimum_severity") is not None:
            routing.append(f"minimum severity: {plugin['minimum_severity']}")
        fields.append(("routing", routing or ["every alert"]))
        fields.append(("queue", f"{plugin.get('queued', 0)} queued, {plugin.get('dropped', 0)} dropped"))
    fields.append(("file", plugin.get("definition_file") or "-"))
    _print_fields(fields)
    detail = plugin.get("last_error_detail")
    if detail and traceback:
        print("  traceback")
        for line in str(detail).rstrip().splitlines():
            print(f"    {line}")
    elif detail:
        print("  (--traceback shows where the last error happened)")


def print_silences(payload: dict) -> None:
    silences = payload.get("silences", [])
    if not silences:
        print("no silences")
        return

    id_width = max(len("SILENCE ID"), *(len(row["silence_id"]) for row in silences))
    operator_width = max(len("CREATED BY"), *(len(row["created_by"]) for row in silences))
    status_width = len("STATUS")
    start_width = max(len("START"), *(len(row["start_at"]) for row in silences))
    end_width = max(len("END"), *(len(row["end_at"]) for row in silences))
    target_width = max(len("TARGET"), *(len(", ".join(row["rule_patterns"] or row["tags"]) or "-") for row in silences))

    header = (
        f"{'SILENCE ID':<{id_width}}  "
        f"{'CREATED BY':<{operator_width}}  "
        f"{'STATUS':<{status_width}}  "
        f"{'START':<{start_width}}  "
        f"{'END':<{end_width}}  "
        f"{'TARGET':<{target_width}}  "
        f"REASON"
    )
    print(header)
    print("-" * len(header))
    for row in sorted(silences, key=silence_sort_key):
        status = silence_display_status(row).lower()
        target = ", ".join(row["rule_patterns"] or row["tags"]) or "-"
        print(
            f"{row['silence_id']:<{id_width}}  "
            f"{row['created_by']:<{operator_width}}  "
            f"{status:<{status_width}}  "
            f"{row['start_at']:<{start_width}}  "
            f"{row['end_at']:<{end_width}}  "
            f"{target:<{target_width}}  "
            f"{row.get('reason') or ''}"
        )


def apply_silence_filters(payload: dict, *, since: str | None, limit: int | None) -> dict:
    silences = list(payload.get("silences", []))
    since_dt = parse_iso_datetime(since)
    if since_dt is not None:
        filtered: list[dict] = []
        for silence in silences:
            reference = silence_recency_key(silence)
            if reference >= since_dt:
                filtered.append(silence)
        silences = filtered
    silences = sorted(silences, key=silence_recency_key, reverse=True)
    if limit is not None and limit >= 0:
        silences = silences[:limit]
    payload["silences"] = silences
    return payload


def silence_display_status(row: dict) -> str:
    if row.get("cancelled_at") is not None:
        return "CANCELLED"
    if row.get("active"):
        return "ACTIVE"
    now = datetime.now(timezone.utc)
    end_at = parse_iso_datetime(row.get("end_at"))
    start_at = parse_iso_datetime(row.get("start_at"))
    if end_at is not None and end_at <= now:
        return "EXPIRED"
    if start_at is not None and start_at > now:
        return "SCHEDULED"
    return "EXPIRED"


def silence_sort_key(row: dict) -> tuple[int, str]:
    rank = {
        "ACTIVE": 0,
        "SCHEDULED": 1,
        "EXPIRED": 2,
        "CANCELLED": 3,
    }.get(silence_display_status(row), 4)
    return (rank, str(row.get("start_at") or ""))


def parse_iso_datetime(value: str | None) -> datetime | None:
    if not value:
        return None
    normalized = str(value).replace("Z", "+00:00")
    try:
        parsed = datetime.fromisoformat(normalized)
    except ValueError:
        return None
    if parsed.tzinfo is None:
        return parsed.replace(tzinfo=timezone.utc)
    return parsed.astimezone(timezone.utc)


def silence_recency_key(row: dict) -> datetime:
    end_at = parse_iso_datetime(row.get("end_at"))
    start_at = parse_iso_datetime(row.get("start_at"))
    return end_at or start_at or datetime.min.replace(tzinfo=timezone.utc)


def print_history(payload: dict) -> None:
    if not payload.get("enabled", False):
        print("history is disabled")
        return

    events = payload.get("alert_events", [])
    dispatches = payload.get("output_dispatches", [])
    actions = payload.get("operator_actions", [])
    if not events and not dispatches and not actions:
        print("no history")
        return

    if events:
        print("alert events")
        for event in events:
            transition = f"  {event['transition']}" if event.get("transition") else ""
            previous_severity = event.get("previous_severity")
            current_severity = event.get("current_severity", event.get("severity"))
            if previous_severity is None or previous_severity == current_severity:
                severity_part = severity_label(current_severity)
            else:
                severity_part = (
                    f"{severity_label(previous_severity)} -> "
                    f"{severity_label(current_severity)}"
                )
            print(
                f"  {event['occurred_at']}  "
                f"{event['previous_state'] or '-'} -> {event['current_state']}  "
                f"{severity_part}{transition}  "
                f"{event.get('message') or ''}"
            )

    if dispatches:
        print("output dispatches")
        for dispatch in dispatches:
            print(
                f"  {dispatch['occurred_at']}  "
                f"{dispatch['previous_state'] or '-'} -> {dispatch['current_state']}  "
                f"matched={', '.join(dispatch.get('matched_outputs', [])) or '-'}  "
                f"delivered={', '.join(dispatch.get('delivered_outputs', [])) or '-'}  "
                f"emit_skipped={', '.join(dispatch.get('emit_skipped_outputs', [])) or '-'}  "
                f"failed={', '.join(dispatch.get('failed_outputs', [])) or '-'}  "
                f"uninitialized={', '.join(dispatch.get('uninitialized_outputs', [])) or '-'}  "
                f"filtered={', '.join(dispatch.get('filtered_outputs', [])) or '-'}"
            )

    if actions:
        print("operator actions")
        for action in actions:
            print(
                f"  {action['created_at']}  "
                f"{action['action_type']}  "
                f"{action['operator']}  "
                f"{action.get('reason') or ''}"
            )


def apply_history_filters(payload: dict, *, since: str | None, limit: int | None) -> dict:
    since_dt = parse_iso_datetime(since)

    events = list(payload.get("alert_events", []))
    dispatches = list(payload.get("output_dispatches", []))
    actions = list(payload.get("operator_actions", []))

    if since_dt is not None:
        events = [
            event for event in events
            if history_entry_time(event.get("occurred_at")) >= since_dt
        ]
        dispatches = [
            dispatch for dispatch in dispatches
            if history_entry_time(dispatch.get("occurred_at")) >= since_dt
        ]
        actions = [
            action for action in actions
            if history_entry_time(action.get("created_at")) >= since_dt
        ]

    events = sorted(events, key=lambda event: history_entry_time(event.get("occurred_at")), reverse=True)
    dispatches = sorted(dispatches, key=lambda dispatch: history_entry_time(dispatch.get("occurred_at")), reverse=True)
    actions = sorted(actions, key=lambda action: history_entry_time(action.get("created_at")), reverse=True)

    if limit is not None and limit >= 0:
        events = events[:limit]
        dispatches = dispatches[:limit]
        actions = actions[:limit]

    payload["alert_events"] = events
    payload["output_dispatches"] = dispatches
    payload["operator_actions"] = actions
    return payload


def history_entry_time(value: str | None) -> datetime:
    return parse_iso_datetime(value) or datetime.min.replace(tzinfo=timezone.utc)


def severity_label(value: int | None) -> str:
    if value is None:
        return "-"
    return SEVERITY_LABELS.get(value, str(value))


def colorize(text: str, key: str, *, colored: bool) -> str:
    if not colored or not key:
        return text
    color = ANSI_COLORS.get(key)
    if color is None:
        return text
    return f"{color}{text}{ANSI_RESET}"


if __name__ == "__main__":
    raise SystemExit(main())
