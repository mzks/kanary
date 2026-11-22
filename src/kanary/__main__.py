import argparse
import difflib
from importlib import metadata as importlib_metadata
import logging
import os
from pathlib import Path
import sys
import tomllib

from . import scaffold
from .constants import DEFAULT_VIEWER_TITLE
from .loader import RuleDirectoryLoader
from .runtime import AUTO_RELOAD_CHOICES, DEFAULT_LOG_LEVEL, LOG_LEVEL_CHOICES, EngineRuntime, RuntimeConfig

PROJECT_ROOT = Path(__file__).resolve().parents[2]
NEW_USAGE = "kanary new -t TEMPLATE PLUGIN_ID [-o PATH] [-a | --force | --stdout]"


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


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        description="Run or lint the KANARY engine",
        epilog=(
            "Run is the default command, so you can omit it:\n"
            "  kanary ./plugins\n"
            "  python -m kanary ./plugins\n"
            "\n"
            "Common run options:\n"
            "  --api-host HOST        Bind the local API and Web viewer host\n"
            "  --api-port PORT        Bind the local API and Web viewer port\n"
            "  --state-db PATH        Persist history and actions in SQLite\n"
            "  --no-output-emit       Route outputs without calling emit()\n"
            "  --auto-reload MODE     Apply discovered changes automatically (off, dirty, all)\n"
            "  --exclude GLOB         Exclude matching plugin ids from the runtime\n"
            "  For the full run option list, use:\n"
            "  kanary run --help\n"
            "Lint must be explicit:\n"
            "  kanary lint ./plugins\n"
            "Generate a plugin from a template:\n"
            "  kanary new --list\n"
            "  kanary new -t range my.rule"
        ),
        formatter_class=argparse.RawDescriptionHelpFormatter,
    )
    parser.add_argument(
        "--version",
        action="version",
        version=f"%(prog)s {_kanary_version()}",
    )
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
        type=lambda value: value.strip().upper(),  # info, INFO, Info
        choices=LOG_LEVEL_CHOICES,
        help="Logging level for engine/runtime logs.",
    )
    run_parser.add_argument(
        "--auto-reload",
        default="off",
        type=lambda value: value.strip().lower(),  # dirty, Dirty, DIRTY
        choices=AUTO_RELOAD_CHOICES,
        help="Automatically apply discovered changes: off, dirty, or all. Defaults to off.",
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
        "--viewer-title",
        default=os.environ.get("KANARY_VIEWER_TITLE", DEFAULT_VIEWER_TITLE),
        help=f"Title shown in the built-in Web viewer header and browser tab. Defaults to KANARY_VIEWER_TITLE or '{DEFAULT_VIEWER_TITLE}'.",
    )
    run_parser.add_argument(
        "--no-output-emit",
        action="store_true",
        help="Load, initialize, and route Output plugins without calling emit().",
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

    new_parser = subparsers.add_parser(
        "new",
        help="Generate a plugin file from a template",
        usage=(
            f"{NEW_USAGE}\n"
            "       kanary new --show TEMPLATE\n"
            "       kanary new --list"
        ),
        description="Generate a plugin file from a template. Every template passes `kanary lint`.",
        epilog=(
            "Templates:\n"
            + _template_table_for_help()
            + "\n\nExamples:\n"
            "  kanary new -t source postgres.wide -o plugins/\n"
            "  kanary new -t range oil.temperature.high -o plugins/postgres_wide.py -a\n"
            "  kanary new -t followups oncall --stdout\n"
            "  kanary new --show ping\n"
            "\n"
            "A PLUGIN_ID ending in .py names the file: `kanary new -t source plugins/env.py`\n"
            "creates plugins/env.py with source_id \"env\"."
        ),
        formatter_class=argparse.RawDescriptionHelpFormatter,
    )
    new_parser.add_argument(
        "-t",
        "--template",
        metavar="TEMPLATE",
        help="Template name from the table below. It also decides the plugin kind.",
    )
    new_parser.add_argument("plugin_id", nargs="?", metavar="PLUGIN_ID", help="source_id, rule_id, or output_id of the new plugin.")
    new_parser.add_argument(
        "-o",
        "--output",
        metavar="PATH",
        help="File, or directory ending in '/', to write. Default: <plugin_id>.py in the current directory.",
    )
    mode = new_parser.add_mutually_exclusive_group()
    mode.add_argument("-a", "--append", action="store_true", help="Append to an existing file (imports are merged).")
    mode.add_argument("--force", action="store_true", help="Overwrite an existing file.")
    mode.add_argument("--stdout", action="store_true", help="Print the result instead of writing a file.")
    new_parser.add_argument("-l", "--list", action="store_true", help="List the templates and exit.")
    new_parser.add_argument("--show", metavar="TEMPLATE", help="Print a template as it is, without generating anything.")
    new_parser.add_argument(
        "--catalog",
        nargs="?",
        const="en",
        choices=("en", "ja"),
        help="Print every template as one Markdown page: docs/templates.md, or with 'ja' docs/templates_ja.md.",
    )
    new_parser.set_defaults(print_new_help=new_parser.print_help)
    return parser


def _new_error(message: str, *, usage: str = NEW_USAGE) -> int:
    print(f"usage: {usage}\nerror: {message}", file=sys.stderr)
    return 2


def _unknown_template(name: str, templates: dict) -> str:
    close = difflib.get_close_matches(name, list(templates), n=3, cutoff=0.6)
    hint = f"; did you mean {' or '.join(repr(item) for item in close)}?" if close else ""
    return f"unknown template {name!r}{hint} (kanary new --list shows the templates)"


def _template_table_for_help() -> str:
    # The help text is built on every start; a broken template must not stop `kanary run`.
    try:
        return scaffold.format_template_table()
    except Exception as exc:
        return f"  (the templates could not be read: {exc})"


def _run_new(args: argparse.Namespace) -> int:
    templates = scaffold.load_templates()
    if args.list:
        print(scaffold.format_template_table(templates))
        return 0
    if args.catalog:
        print(scaffold.format_catalog(templates, language=args.catalog), end="")
        return 0
    if args.show:
        template = templates.get(args.show)
        if template is None:
            return _new_error(_unknown_template(args.show, templates), usage="kanary new --show TEMPLATE")
        print(template.body, end="")
        return 0
    if not any((args.template, args.plugin_id, args.output, args.append, args.force, args.stdout)):
        # Plain `kanary new`: the help, with the template table.
        args.print_new_help(sys.stderr)
        return 2
    # Who gave some arguments already knows the templates: show how to write the command.
    if not args.template:
        missing = "PLUGIN_ID and -t TEMPLATE are missing" if not args.plugin_id else "-t TEMPLATE is missing"
        return _new_error(f"{missing} (kanary new --list shows the templates)")
    template = templates.get(args.template)
    if template is None:
        return _new_error(_unknown_template(args.template, templates))
    if not args.plugin_id:
        rest = f" -o {args.output}" if args.output else ""
        return _new_error(
            f"PLUGIN_ID is missing: the {template.kind}_id of the new {template.kind}, "
            f"as in `kanary new -t {template.name} my.{template.name.replace('-', '_')}{rest}`"
        )

    plugin_id, named_path = scaffold.split_plugin_argument(args.plugin_id)
    try:
        content = scaffold.render(template, plugin_id)
    except ValueError as exc:
        print(f"error: {exc}", file=sys.stderr)
        return 2
    if args.stdout:
        print(content, end="")
        return 0

    if args.output and (args.output.endswith(("/", os.sep)) or Path(args.output).is_dir()):
        target = Path(args.output) / (named_path.name if named_path else scaffold.file_name_for(plugin_id))
    elif args.output:
        target = Path(args.output)
    else:
        target = named_path or Path(scaffold.file_name_for(plugin_id))
    try:
        if args.append:
            scaffold.append(
                target,
                content,
                plugin_id=plugin_id,
                class_name=scaffold.class_name_for(plugin_id, template.kind),
            )
        else:
            scaffold.write(target, content, force=args.force)
    except (FileExistsError, ValueError) as exc:
        print(f"error: {exc}", file=sys.stderr)
        return 1
    action = "appended" if args.append else "created"
    print(f"{action} {template.kind} '{plugin_id}' ({template.name}) in {target}")
    return 0


def main() -> int:
    """The command; output piped into a reader that stops early (| head) ends it quietly."""
    try:
        code = _main()
        sys.stdout.flush()
        return code
    except BrokenPipeError:
        # Python would report the pipe once more when it flushes stdout at exit.
        os.dup2(os.open(os.devnull, os.O_WRONLY), sys.stdout.fileno())
        return 1


def _main() -> int:
    parser = build_parser()
    argv = sys.argv[1:]
    if not argv:
        parser.print_help()
        return 2
    if argv[0] not in {"run", "lint", "new", "-h", "--help", "--version"}:
        argv = ["run", *argv]
    args = parser.parse_args(argv)

    if args.command is None:
        parser.print_help()
        return 2

    if args.command == "new":
        return _run_new(args)

    if args.command == "lint":
        loader = RuleDirectoryLoader([Path(path) for path in args.plugin_directories])
        try:
            _, report = loader.inspect(exclude_patterns=args.exclude, lint=True)
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

    missing = [path for path in args.plugin_directories if not Path(path).exists()]
    if missing:
        print(f"kanary: plugin directory not found: {', '.join(missing)}", file=sys.stderr)
        return 2
    logging.basicConfig(
        level=getattr(logging, args.log_level),
        format="%(asctime)s %(levelname)s %(name)s: %(message)s",
    )
    try:
        runtime = _build_runtime(args)
    except OSError as exc:  # the API's port is taken, or the host is not this machine
        print(
            f"kanary: cannot listen on {args.api_host}:{args.api_port}: {exc.strerror or exc} "
            "(is another KANARY running there? choose another --api-port)",
            file=sys.stderr,
        )
        return 1
    runtime.run_forever()
    return 0


def _build_runtime(args: argparse.Namespace) -> EngineRuntime:
    return EngineRuntime(
        RuntimeConfig(
            rule_directories=[Path(path) for path in args.plugin_directories],
            auto_reload=args.auto_reload,
            exclude_plugins=args.exclude,
            log_level=args.log_level,
            api_host=args.api_host,
            api_port=args.api_port,
            enable_default_viewer=not args.disable_default_viewer,
            viewer_title=args.viewer_title,
            output_emit_enabled=not args.no_output_emit,
            state_db_path=Path(args.state_db) if args.state_db else None,
            node_id=args.node_id,
        )
    )


if __name__ == "__main__":
    raise SystemExit(main())
