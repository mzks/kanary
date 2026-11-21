"""Generate plugin files for ``kanary new`` from the files in ``kanary/templates``.

Each template is a working plugin file that starts with a small header::

    # kanary-template: range
    # kind: rule
    # level: basic
    # summary: Fire when a value leaves [low, high] ...

``level`` is "basic" (one helper class, mostly settings) or "advanced"
(custom logic or several features combined).

Inside a template, the plugin id is written as ``"template.<name>"`` and the
class as ``Template<Name>``; both are replaced when a file is generated.
"""

from dataclasses import dataclass
from importlib import resources
import json
from pathlib import Path
import re

KINDS = ("source", "rule", "output")
LEVELS = ("basic", "advanced")
_CLASS_SUFFIX = {"source": "Source", "rule": "Rule", "output": "Output"}
# Display order for `kanary new --list`; templates not listed here follow alphabetically.
_ORDER = (
    "source", "command", "ssh", "ping", "tcp", "http-status", "file", "log-count", "http-json", "prometheus", "sql-wide", "sql-long",
    "http-push", "mqtt", "plugin-health",
    "push", "buffered", "trend", "spike", "frozen", "remote",
    "rule", "range", "threshold", "expected", "stale", "rate", "difference", "k-of-n", "aggregate", "count",
    "multi-source",
    "output", "webhook", "slack", "mattermost", "discord", "file-log", "command-output", "mail",
    "followups", "delayed", "quiet-hours", "oncall",
)
_HEADER = re.compile(r"^# (kanary-template|kind|level|summary|summary-ja): (.+)$")
_IMPORT = re.compile(r"^(import |from \S+ import )")


@dataclass(frozen=True, slots=True)
class Template:
    name: str
    kind: str
    level: str
    summary: str
    body: str
    summary_ja: str = ""  # for the Japanese catalog; the English summary when empty

    @property
    def placeholder_id(self) -> str:
        return "template." + self.name.replace("-", "_")

    @property
    def placeholder_class(self) -> str:
        return "Template" + _camel(self.name)


def load_templates() -> dict[str, Template]:
    templates: dict[str, Template] = {}
    for entry in resources.files("kanary").joinpath("templates").iterdir():
        if not entry.name.endswith(".py"):
            continue
        template = _parse(entry.read_text(encoding="utf-8"))
        templates[template.name] = template
    rank = {name: index for index, name in enumerate(_ORDER)}
    return dict(
        sorted(
            templates.items(),
            key=lambda item: (
                LEVELS.index(item[1].level),
                KINDS.index(item[1].kind),
                rank.get(item[0], len(rank)),
                item[0],
            ),
        )
    )


def _parse(text: str) -> Template:
    meta: dict[str, str] = {}
    lines = text.splitlines(keepends=True)
    index = 0
    while index < len(lines) and (match := _HEADER.match(lines[index].rstrip("\n"))):
        meta[match.group(1)] = match.group(2).strip()
        index += 1
    missing = {"kanary-template", "kind", "level", "summary"} - meta.keys()
    if missing:
        raise ValueError(f"template header is missing: {', '.join(sorted(missing))}")
    if meta["kind"] not in KINDS:
        raise ValueError(f"template '{meta['kanary-template']}' has unknown kind '{meta['kind']}'")
    if meta["level"] not in LEVELS:
        raise ValueError(f"template '{meta['kanary-template']}' has unknown level '{meta['level']}'")
    return Template(
        meta["kanary-template"], meta["kind"], meta["level"], meta["summary"], "".join(lines[index:]),
        summary_ja=meta.get("summary-ja", ""),
    )


def format_template_table(templates: dict[str, Template] | None = None) -> str:
    templates = templates if templates is not None else load_templates()
    width = max(len(name) for name in templates)
    rows: list[str] = []
    for level in LEVELS:
        group = [template for template in templates.values() if template.level == level]
        if not group:
            continue
        if rows:
            rows.append("")
        rows.append(f"  {level.upper()}")
        rows.append(f"  {'KIND':<7} {'TEMPLATE':<{width}}  DESCRIPTION")
        previous_kind = None
        for template in group:
            kind = template.kind if template.kind != previous_kind else ""
            rows.append(f"  {kind:<7} {template.name:<{width}}  {template.summary}")
            previous_kind = template.kind
    return "\n".join(rows)


_CATALOG_TEXT = {
    "en": {
        "title": "Template Catalog",
        "intro": (
            "Every template of `kanary new`, exactly as it writes it: `kanary new -t TEMPLATE PLUGIN_ID` "
            "creates `PLUGIN_ID` from `TEMPLATE`, and `-o FILE -a` appends it to an existing file. "
            "`kanary new --list` lists the same templates in the terminal."
        ),
        "generated": (
            "This page is generated from `src/kanary/templates/` by `kanary new --catalog > docs/templates.md`; "
            "edit the templates, not this page."
        ),
        "contents": "Contents",
        "header": "| Kind | Template | What it does |",
        "basic": "The common starting points: one helper class and mostly settings.",
        "advanced": "Less common helpers, custom logic, or several features together.",
        "kinds": {"source": "Sources", "rule": "Rules", "output": "Outputs"},
        "back": "[Contents](#contents)",
    },
    "ja": {
        "title": "Template 一覧",
        "intro": (
            "`kanary new` の全 template を、書き出されるとおりに載せています。`kanary new -t TEMPLATE PLUGIN_ID` で "
            "`TEMPLATE` から `PLUGIN_ID` を作り、`-o FILE -a` で既存の file に追記します。"
            "`kanary new --list` は同じ一覧を terminal に出します。"
        ),
        "generated": (
            "この page は `src/kanary/templates/` から `kanary new --catalog ja > docs/templates_ja.md` で生成しています。"
            "直すときはこの page ではなく template を直してください。"
        ),
        "contents": "目次",
        "header": "| 種類 | Template | 内容 |",
        "basic": "よく使う出発点。helper class 1 つで、ほぼ設定だけで使えます。",
        "advanced": "使う場面が限られる helper、独自の logic、複数の機能の組み合わせ。",
        "kinds": {"source": "Source 系", "rule": "Rule 系", "output": "Output 系"},
        "back": "[目次へ](#目次)",
    },
}


def format_catalog(templates: dict[str, Template] | None = None, *, language: str = "en") -> str:
    """Every template as one Markdown page with a table of contents (docs/templates.md, templates_ja.md)."""
    templates = templates if templates is not None else load_templates()
    text = _CATALOG_TEXT[language]
    lines = [f"# {text['title']}", "", text["intro"], "", text["generated"], "", f"## {text['contents']}", ""]
    for level in LEVELS:
        group = [template for template in templates.values() if template.level == level]
        if not group:
            continue
        lines += [f"**[{level.upper()}](#{level})**: {text[level]}", "", text["header"], "|---|---|---|"]
        for template in group:
            summary = (template.summary_ja if language == "ja" and template.summary_ja else template.summary)
            lines.append(f"| {template.kind} | [{template.name}](#{template.name}) | {summary.replace('|', chr(92) + '|')} |")
        lines.append("")
    for level in LEVELS:
        group = [template for template in templates.values() if template.level == level]
        if not group:
            continue
        lines += [f"## {level.upper()}", ""]
        previous_kind = None
        for template in group:
            if template.kind != previous_kind:
                lines += [f"### {text['kinds'][template.kind]}", ""]
                previous_kind = template.kind
            plugin_id = f"my.{template.name}"
            summary = template.summary_ja if language == "ja" and template.summary_ja else template.summary
            lines += [
                f"#### {template.name}",
                "",
                f"{template.kind} · {summary}",
                "",
                "```console",
                f"$ kanary new -t {template.name} {plugin_id}",
                "```",
                "",
                "```python",
                render(template, plugin_id).rstrip("\n"),
                "```",
                "",
                text["back"],
                "",
            ]
    return "\n".join(lines).rstrip("\n") + "\n"


def split_plugin_argument(argument: str) -> tuple[str, Path | None]:
    """Return (plugin_id, default_path); 'foo_source.py' means id 'foo_source' in that file."""
    if argument.endswith(".py"):
        path = Path(argument)
        return path.stem, path
    return argument, None


def class_name_for(plugin_id: str, kind: str) -> str:
    name = _camel(plugin_id) or "Plugin"
    if name[0].isdigit():
        name = "Plugin" + name
    suffix = _CLASS_SUFFIX[kind]
    return name if name.endswith(suffix) else name + suffix


def file_name_for(plugin_id: str) -> str:
    stem = re.sub(r"[^0-9A-Za-z]+", "_", plugin_id).strip("_").lower() or "plugin"
    if stem[0].isdigit():
        stem = "plugin_" + stem
    return f"{stem}.py"


def render(template: Template, plugin_id: str) -> str:
    if not plugin_id.strip():
        raise ValueError("plugin id must not be empty")
    escaped_id = json.dumps(plugin_id, ensure_ascii=False)[1:-1]
    # Replace the id inside string literals, including selectors such as "template.source:name"
    # and derived ids such as "template.ping.slow_or_down".
    body = re.sub(rf'(?<=")({re.escape(template.placeholder_id)})(?=[".:])', lambda _: escaped_id, template.body)
    return re.sub(rf"\b{template.placeholder_class}\b", class_name_for(plugin_id, template.kind), body)


def write(path: Path, content: str, *, force: bool = False) -> None:
    if path.exists() and not force:
        raise FileExistsError(f"{path} already exists; use -a to append or --force to overwrite")
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(content, encoding="utf-8")


def append(path: Path, content: str, *, plugin_id: str, class_name: str) -> None:
    existing = path.read_text(encoding="utf-8") if path.exists() else ""
    if re.search(rf"^class {re.escape(class_name)}\b", existing, re.MULTILINE):
        raise ValueError(f"{path} already defines class {class_name}; choose another plugin id")
    if re.search(rf"_id\s*=\s*{re.escape(json.dumps(plugin_id, ensure_ascii=False))}", existing):
        raise ValueError(f"{path} already defines plugin id '{plugin_id}'")
    if not existing.strip():
        write(path, content, force=True)
        return
    imports, rest = _split_imports(content)
    present = {line.strip() for line in existing.splitlines()}
    missing = [line for line in imports if line.strip() not in present]
    merged = _insert_imports(existing, missing).rstrip("\n")
    path.write_text(merged + "\n\n\n" + rest.lstrip("\n"), encoding="utf-8")


def _insert_imports(existing: str, imports: list[str]) -> str:
    """Add import lines to the import block at the top of an existing file."""
    if not imports:
        return existing
    lines = existing.splitlines(keepends=True)
    insert_at = 0
    index = 0
    in_docstring = False
    seen_import = False
    while index < len(lines):
        stripped = lines[index].strip()
        if in_docstring:
            in_docstring = not (stripped.endswith('"""') or stripped.endswith("'''"))
            insert_at = index + 1
        elif stripped[:3] in ('"""', "'''"):
            in_docstring = len(stripped) < 6 or not stripped.endswith(stripped[:3])
            insert_at = index + 1
        elif _IMPORT.match(lines[index]):
            insert_at = index + 1
            seen_import = True
        elif stripped.startswith("#"):
            if not seen_import:
                insert_at = index + 1  # keep a leading comment header first
        elif stripped:
            break
        index += 1
    block = "".join(line + "\n" for line in imports)
    if insert_at and not lines[insert_at - 1].endswith("\n"):
        lines[insert_at - 1] += "\n"
    if insert_at == 0 or not _IMPORT.match(lines[insert_at - 1]):
        block += "\n"  # no import block yet: keep a blank line before the code
    lines.insert(insert_at, block)
    return "".join(lines)


def _split_imports(content: str) -> tuple[list[str], str]:
    lines = content.splitlines(keepends=True)
    index = 0
    imports: list[str] = []
    while index < len(lines) and (not lines[index].strip() or _IMPORT.match(lines[index])):
        if lines[index].strip():
            imports.append(lines[index].rstrip("\n"))
        index += 1
    return imports, "".join(lines[index:])


def _camel(text: str) -> str:
    parts = [part for part in re.split(r"[^0-9A-Za-z]+", text) if part]
    return "".join(part[:1].upper() + part[1:] for part in parts)
