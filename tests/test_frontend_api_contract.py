"""Every endpoint the UI calls has to exist in the backend.

The Knowledge Center shipped calling ``POST /api/rag/test`` while the backend
served ``/api/rag/query-test``. Nothing failed loudly: the frontend caught the
404 and rendered "this feature needs a backend endpoint", so a wiring bug read
as an unimplemented feature for as long as nobody tried it.

The route table is built by parsing the decorators rather than importing the
app, so this stays a fast, side-effect-free test that runs before the server
does. Router prefixes are honoured, because that is what made ``/api/status``
look missing on the first attempt at this check.
"""

import ast
import re
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
BACKEND = ROOT / "backend"
FRONTEND = ROOT / "frontend"
APP_JS = (FRONTEND / "app.js").read_text(encoding="utf-8")


def first_party_javascript():
    """Every executable we author, excluding vendored/minified libraries.

    M14 moves the Extension Center out of the already oversized app.js.  Only
    scanning app.js would let a typo in that new module call a nonexistent API
    while this contract remained green.
    """

    return {
        path: path.read_text(encoding="utf-8")
        for path in sorted(FRONTEND.rglob("*.js"))
        if "vendor" not in path.parts and not path.name.endswith(".min.js")
    }

sys.path.insert(0, str(BACKEND))

HTTP_METHODS = {"get", "post", "put", "delete", "patch", "head", "options"}

#: Frontend paths intentionally not backed by a route in this repo.
KNOWN_EXTERNAL = frozenset()


def _router_prefixes(tree):
    """Map each APIRouter variable to its prefix, e.g. system.py's "/api"."""
    prefixes = {}
    for node in ast.walk(tree):
        if not isinstance(node, ast.Assign):
            continue
        call = node.value
        if not (isinstance(call, ast.Call) and getattr(call.func, "id", "") == "APIRouter"):
            continue
        prefix = ""
        for keyword in call.keywords:
            if keyword.arg == "prefix" and isinstance(keyword.value, ast.Constant):
                prefix = str(keyword.value.value)
        for target in node.targets:
            if isinstance(target, ast.Name):
                prefixes[target.id] = prefix
    return prefixes


def backend_routes():
    routes = set()
    for path in sorted(BACKEND.rglob("*.py")):
        if "__pycache__" in path.parts:
            continue
        try:
            tree = ast.parse(path.read_text(encoding="utf-8", errors="ignore"))
        except SyntaxError:  # pragma: no cover - a broken module fails elsewhere
            continue
        prefixes = _router_prefixes(tree)
        for node in ast.walk(tree):
            for decorator in getattr(node, "decorator_list", []):
                if not isinstance(decorator, ast.Call):
                    continue
                func = decorator.func
                if not (isinstance(func, ast.Attribute) and func.attr in HTTP_METHODS):
                    continue
                if not (decorator.args and isinstance(decorator.args[0], ast.Constant)):
                    continue
                owner = getattr(func.value, "id", "")
                routes.add(prefixes.get(owner, "") + str(decorator.args[0].value))
    return routes


def frontend_calls():
    """Template literals, reduced to the path the server will actually see.

    A complete ``${...}`` becomes a path parameter. An *incomplete* one is the
    start of an inline expression that builds a query string
    (``/api/documents${sessionId ? `?session_id=...` : \'\'}``), so everything
    from there on is cut rather than treated as more path.
    """
    calls = set()
    for javascript in first_party_javascript().values():
        matches = list(
            re.finditer(r"\$\{API_BASE\}(/api/[^`'\"\s]*)", javascript)
        )
        matches.extend(
            re.finditer(
                r"(?:apiFetch|runtimeJson|fetch)\s*\(\s*[`'\"]"
                r"(/api/[^`'\"\s]*)",
                javascript,
            )
        )
        for match in matches:
            path = re.sub(r"\$\{[^{}\s]*\}", "{param}", match.group(1))
            cut = path.find("${")
            if cut != -1:
                path = path[:cut]
            path = path.split("?")[0].rstrip("/")
            if path.startswith("/api/"):
                calls.add(path)
    return calls


def _normalise(path):
    path = re.sub(r"\$\{[^}]*\}", "{param}", path)
    path = re.sub(r"\{[^}]*\}", "{param}", path)
    return path.rstrip("/") or "/"


def _pattern(route):
    return re.compile("^" + re.sub(r"\\\{param\\\}", "[^/]+", re.escape(_normalise(route))) + "$")


def test_every_frontend_endpoint_exists_in_the_backend():
    patterns = [_pattern(route) for route in backend_routes()]
    missing = sorted(
        call for call in frontend_calls()
        if call not in KNOWN_EXTERNAL
        and not any(pattern.match(_normalise(call)) for pattern in patterns)
    )
    assert not missing, (
        "frontend calls endpoints the backend does not serve: "
        + ", ".join(missing)
        + " -- the UI will render a 404 as an unimplemented feature"
    )


def test_the_route_table_is_actually_being_built():
    """A parser that silently finds nothing would make this suite vacuous."""
    routes = backend_routes()
    assert len(routes) > 40, f"only {len(routes)} routes parsed"
    assert "/api/status" in routes, "router prefixes are not being applied"
    assert "/api/models/catalog" in routes
    assert len(frontend_calls()) > 20


def test_every_first_party_javascript_file_is_in_the_api_inventory():
    files = first_party_javascript()
    assert FRONTEND / "app.js" in files
    assert all("vendor" not in path.parts for path in files)
    assert all(not path.name.endswith(".min.js") for path in files)
