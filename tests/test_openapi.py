"""docs/openapi.yaml lists exactly the routes that src/kanary/api.py serves.

Read without a YAML library: the spec keeps each path at two spaces and each method at
four under ``paths:``, and api.py routes with ``if request_path == ...`` (GET) and
``if self.path == ...`` (POST), or ``startswith(...)`` / ``endswith(...)`` for the paths with
an id in them.
"""

from pathlib import Path
import re
import unittest

ROOT = Path(__file__).resolve().parents[1]
SPEC = ROOT / "docs" / "openapi.yaml"
API = ROOT / "src" / "kanary" / "api.py"


def spec_operations() -> set[tuple[str, str]]:
    operations: set[tuple[str, str]] = set()
    in_paths = False
    path = None
    for line in SPEC.read_text(encoding="utf-8").splitlines():
        if line == "paths:":
            in_paths = True
            continue
        if not in_paths:
            continue
        if line and not line.startswith(" "):
            break  # the next top-level key
        match = re.fullmatch(r"  (/\S*):", line)
        if match:
            path = match.group(1)
            continue
        match = re.fullmatch(r"    (get|put|post|delete|patch):", line)
        if match and path is not None:
            operations.add((match.group(1).upper(), path))
    return operations


def served_routes() -> tuple[set[tuple[str, str]], int]:
    """(method, regex of the paths it takes), and how many route conditions api.py has."""
    source = API.read_text(encoding="utf-8")
    parts = (
        ("GET", source[source.index("def do_GET"):source.index("def do_POST")], "request_path"),
        ("POST", source[source.index("def _post"):source.index("def log_message")], r"self\.path"),
    )
    routes: set[tuple[str, str]] = set()
    conditions = 0
    for method, text, subject in parts:
        conditions += len(re.findall(rf"^\s*if {subject}\b", text, flags=re.MULTILINE))
        for match in re.finditer(rf'if {subject} == "(/[^"]*)"', text):
            routes.add((method, re.escape(match.group(1))))
        for match in re.finditer(rf'if {subject}\.startswith\("(/[^"]*)"\)(?: and {subject}\.endswith\("(/[^"]*)"\))?', text):
            routes.add((method, re.escape(match.group(1)) + "[^?]+" + re.escape(match.group(2) or "")))
    return routes, conditions


def sample(path: str) -> str:
    return re.sub(r"{\w+}", "x", path)


class OpenAPITest(unittest.TestCase):
    def test_every_route_is_in_the_spec_and_every_spec_path_is_served(self):
        operations = spec_operations()
        routes, conditions = served_routes()
        self.assertEqual(len(routes), conditions, "a route in api.py is written in a way this test does not read")
        self.assertGreater(len(operations), 20)
        for method, path in sorted(operations):
            served = [pattern for kind, pattern in routes if kind == method and re.fullmatch(pattern, sample(path))]
            self.assertEqual(len(served), 1, f"{method} {path}: served by {served}")
        for method, pattern in sorted(routes):
            described = [path for kind, path in operations if kind == method and re.fullmatch(pattern, sample(path))]
            self.assertTrue(described, f"{method} {pattern} is served but not in docs/openapi.yaml")

    def test_the_spec_names_the_openapi_version_and_its_parts(self):
        text = SPEC.read_text(encoding="utf-8")
        self.assertTrue(text.startswith("openapi: 3.1."))
        for key in ("info:", "paths:", "components:"):
            self.assertIn(f"\n{key}\n", text)
        referenced = set(re.findall(r'"#/components/schemas/(\w+)"', text))
        defined = set(re.findall(r"^    (\w+):$", text.split("\n  schemas:\n", 1)[1], flags=re.MULTILINE))
        self.assertEqual(referenced - defined, set(), "schemas referenced but not defined")


if __name__ == "__main__":
    unittest.main()
