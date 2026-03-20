from __future__ import annotations

import argparse
import json
import os
import sys
from urllib.error import HTTPError, URLError
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
    "RESOLVED": "\033[32m",
}


def main() -> int:
    parser = argparse.ArgumentParser(description="Inspect and control a running KANARY instance")
    parser.add_argument(
        "--base-url",
        default=os.environ.get("KANARY_API_URL", "http://127.0.0.1:8000"),
        help="KANARY API base URL",
    )
    subparsers = parser.add_subparsers(dest="command", required=True)

    subparsers.add_parser("help", help="Show this help message")
    subparsers.add_parser("health", help="Show runtime health")

    alerts_parser = subparsers.add_parser("alerts", help="Show current alerts")
    alerts_parser.add_argument("--json", action="store_true", help="Print raw JSON")

    history_parser = subparsers.add_parser("history", help="Show persisted alert history for a rule")
    history_parser.add_argument("rule_id")
    history_parser.add_argument("--json", action="store_true", help="Print raw JSON")

    plugins_parser = subparsers.add_parser("plugins", help="Show source/rule/output plugin status")
    plugins_parser.add_argument("--json", action="store_true", help="Print raw JSON")

    silences_parser = subparsers.add_parser("silences", help="Show configured silences")
    silences_parser.add_argument("--json", action="store_true", help="Print raw JSON")

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

    subparsers.add_parser("reload", help="Trigger a manual reload")

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
            payload = fetch_json(f"{args.base_url}/alerts")
            if args.json:
                print(json.dumps(payload, ensure_ascii=False, indent=2))
            else:
                print_alerts(payload)
            return 0

        if args.command == "history":
            payload = fetch_json(f"{args.base_url}/history/{args.rule_id}")
            if args.json:
                print(json.dumps(payload, ensure_ascii=False, indent=2))
            else:
                print_history(payload)
            return 0

        if args.command == "plugins":
            payload = fetch_json(f"{args.base_url}/plugins")
            if args.json:
                print(json.dumps(payload, ensure_ascii=False, indent=2))
            else:
                print_plugins(payload)
            return 0

        if args.command == "silences":
            payload = fetch_json(f"{args.base_url}/silences")
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
            payload = fetch_json(f"{args.base_url}/reload", method="POST")
            print(payload.get("status", "unknown"))
            return 0
    except (HTTPError, URLError) as exc:
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


def print_health(payload: dict) -> None:
    print(f"status      {payload.get('status')}")
    print(f"sources     {', '.join(payload.get('sources', [])) or '-'}")
    print(f"rules       {len(payload.get('rules', []))}")
    print(f"alert_count {payload.get('alert_count', 0)}")


def print_alerts(payload: dict) -> None:
    alerts = payload.get("alerts", [])
    if not alerts:
        print("no alerts")
        return

    colored = sys.stdout.isatty()
    severities = [severity_label(alert.get("severity")) for alert in alerts]
    rule_width = max(len("RULE ID"), *(len(alert["rule_id"]) for alert in alerts))
    state_width = max(len("STATE"), *(len(alert["state"]) for alert in alerts))
    severity_width = max(len("SEV"), *(len(label) for label in severities))
    outputs_width = max(len("OUTPUTS"), *(len(", ".join(alert.get("matched_outputs", [])) or "-") for alert in alerts))
    file_width = max(len("FILE"), *(len(alert.get("definition_file") or "-") for alert in alerts))
    ack_width = max(len("ACKED BY"), *(len(alert.get("acked_by") or "-") for alert in alerts))
    silence_width = max(len("SILENCES"), *(len(", ".join(alert.get("active_silence_ids", [])) or "-") for alert in alerts))

    header = (
        f"{'RULE ID':<{rule_width}}  "
        f"{'STATE':<{state_width}}  "
        f"{'SEV':<{severity_width}}  "
        f"{'ACKED BY':<{ack_width}}  "
        f"{'SILENCES':<{silence_width}}  "
        f"{'OUTPUTS':<{outputs_width}}  "
        f"{'FILE':<{file_width}}  "
        f"MESSAGE"
    )
    print(header)
    print("-" * len(header))
    for alert, severity in zip(alerts, severities):
        message = alert.get("message") or ""
        state = alert["state"]
        outputs = ", ".join(alert.get("matched_outputs", [])) or "-"
        acked_by = alert.get("acked_by") or "-"
        silences = ", ".join(alert.get("active_silence_ids", [])) or "-"
        definition_file = alert.get("definition_file") or "-"
        rendered_state = colorize(state, state, colored=colored)
        severity_key = severity if state != "OK" else ""
        rendered_severity = colorize(severity, severity_key, colored=colored)
        print(
            f"{alert['rule_id']:<{rule_width}}  "
            f"{rendered_state:<{state_width + color_padding(rendered_state, state)}}  "
            f"{rendered_severity:<{severity_width + color_padding(rendered_severity, severity)}}  "
            f"{acked_by:<{ack_width}}  "
            f"{silences:<{silence_width}}  "
            f"{outputs:<{outputs_width}}  "
            f"{definition_file:<{file_width}}  "
            f"{message}"
        )


def print_plugins(payload: dict) -> None:
    plugins = payload.get("plugins", [])
    if not plugins:
        print("no plugins")
        return

    colored = sys.stdout.isatty()
    type_width = max(len("TYPE"), *(len(plugin["type"]) for plugin in plugins))
    plugin_width = max(len("PLUGIN"), *(len(plugin["plugin_id"]) for plugin in plugins))
    state_width = max(len("STATE"), *(len(plugin["state"]) for plugin in plugins))
    run_width = max(len("RUNS"), *(len(str(plugin["run_count"])) for plugin in plugins))
    updated_width = max(len("UPDATED"), *(len(plugin.get("last_updated_at") or "-") for plugin in plugins))
    file_width = max(len("FILE"), *(len(plugin.get("definition_file") or "-") for plugin in plugins))

    header = (
        f"{'TYPE':<{type_width}}  "
        f"{'PLUGIN':<{plugin_width}}  "
        f"{'STATE':<{state_width}}  "
        f"{'RUNS':>{run_width}}  "
        f"{'UPDATED':<{updated_width}}  "
        f"{'FILE':<{file_width}}  "
        f"LAST ERROR"
    )
    print(header)
    print("-" * len(header))
    for plugin in plugins:
        state = plugin["state"]
        rendered_state = colorize(state, state.upper(), colored=colored)
        print(
            f"{plugin['type']:<{type_width}}  "
            f"{plugin['plugin_id']:<{plugin_width}}  "
            f"{rendered_state:<{state_width + color_padding(rendered_state, state)}}  "
            f"{plugin['run_count']:>{run_width}}  "
            f"{(plugin.get('last_updated_at') or '-'): <{updated_width}}  "
            f"{(plugin.get('definition_file') or '-'): <{file_width}}  "
            f"{plugin.get('last_error') or ''}"
        )


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
    for row in silences:
        if row["cancelled_at"] is not None:
            status = "cancelled"
        elif row["active"]:
            status = "active"
        else:
            status = "scheduled"
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


def print_history(payload: dict) -> None:
    if not payload.get("enabled", False):
        print("history is disabled")
        return

    events = payload.get("alert_events", [])
    actions = payload.get("operator_actions", [])
    if not events and not actions:
        print("no history")
        return

    if events:
        print("alert events")
        for event in events:
            print(
                f"  {event['occurred_at']}  "
                f"{event['previous_state'] or '-'} -> {event['current_state']}  "
                f"{severity_label(event['severity'])}  "
                f"{event.get('message') or ''}"
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


def color_padding(rendered: str, plain: str) -> int:
    return len(rendered) - len(plain)


if __name__ == "__main__":
    raise SystemExit(main())
