import argparse
import logging
import os
from pathlib import Path
import sys

from .loader import RuleDirectoryLoader
from .runtime import DEFAULT_LOG_LEVEL, LOG_LEVEL_CHOICES, EngineRuntime, RuntimeConfig


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description="Run or lint the KANARY engine")
    subparsers = parser.add_subparsers(dest="command")

    run_parser = subparsers.add_parser("run", help="Run the KANARY engine")
    run_parser.add_argument(
        "plugin_directories",
        nargs="+",
        help="One or more directories containing Python Source, Rule, and Output plugins.",
    )
    run_parser.add_argument(
        "--exclude",
        action="append",
        default=[],
        metavar="PLUGIN_GLOB",
        help="Exclude one or more plugin id patterns such as 'sqlite.*.stale' or 'discord'. Can be given multiple times.",
    )
    run_parser.add_argument(
        "--log-level",
        default=DEFAULT_LOG_LEVEL,
        choices=LOG_LEVEL_CHOICES,
        help="Logging level for engine/runtime logs.",
    )
    run_parser.add_argument(
        "--api-host",
        default=os.environ.get("KANARY_API_HOST", "0.0.0.0"),
        help="Host for the local control API and web viewer. Defaults to KANARY_API_HOST or 0.0.0.0.",
    )
    run_parser.add_argument(
        "--api-port",
        default=8000,
        type=int,
        help="Port for the local control API and web viewer. Defaults to 8000.",
    )
    run_parser.add_argument(
        "--disable-default-viewer",
        action="store_true",
        help="Disable the built-in default Web viewer while keeping the HTTP API enabled.",
    )
    run_parser.add_argument(
        "--state-db",
        default=os.environ.get("KANARY_SQLITE_PATH"),
        help="Optional SQLite database path for persisted history and runtime actions. Defaults to KANARY_SQLITE_PATH if set.",
    )
    run_parser.add_argument(
        "--node-id",
        default=os.environ.get("KANARY_NODE_ID"),
        help="Optional node identifier used for peer export/import. Defaults to KANARY_NODE_ID or the local hostname.",
    )

    lint_parser = subparsers.add_parser("lint", help="Lint one or more KANARY plugin directories")
    lint_parser.add_argument(
        "plugin_directories",
        nargs="+",
        help="One or more directories containing Python Source, Rule, and Output plugins.",
    )
    lint_parser.add_argument(
        "--exclude",
        action="append",
        default=[],
        metavar="PLUGIN_GLOB",
        help="Exclude one or more plugin id patterns such as 'sqlite.*.stale' or 'discord'. Can be given multiple times.",
    )
    return parser

def main() -> int:
    parser = build_parser()
    argv = sys.argv[1:]
    if not argv:
        parser.print_help()
        return 2
    if argv[0] not in {"run", "lint", "-h", "--help"}:
        argv = ["run", *argv]
    args = parser.parse_args(argv)

    if args.command is None:
        parser.print_help()
        return 2

    if args.command == "lint":
        loader = RuleDirectoryLoader([Path(path) for path in args.plugin_directories])
        try:
            _, report = loader.inspect(exclude_patterns=args.exclude)
        except Exception as exc:
            print(f"lint failed: {exc}")
            return 1

        for warning in report.warnings:
            print(f"warning: {warning}")
        for error in report.errors:
            print(f"error: {error}")
        if report.ok:
            print("lint ok")
            return 0
        return 1

    logging.basicConfig(
        level=getattr(logging, args.log_level),
        format="%(asctime)s %(levelname)s %(name)s: %(message)s",
    )
    runtime = EngineRuntime(
        RuntimeConfig(
            rule_directories=[Path(path) for path in args.plugin_directories],
            exclude_plugins=args.exclude,
            log_level=args.log_level,
            api_host=args.api_host,
            api_port=args.api_port,
            enable_default_viewer=not args.disable_default_viewer,
            state_db_path=Path(args.state_db) if args.state_db else None,
            node_id=args.node_id,
        )
    )
    runtime.run_forever()
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
