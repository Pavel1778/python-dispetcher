"""Structural guard: no request is dispatched before its auth gate.

The web server authenticates by a single, deliberate choke point: every
``do_*`` handler resolves the path, dispatches the explicitly *public* routes,
and then refuses everything else unless ``self._require_read()`` (GET) or
``self._require_write()`` (POST/PUT-less writes) succeeds. Two failure modes
have actually shipped in this repository and are what this suite exists to
make impossible to re-introduce:

1. a new route added *above* the gate line (handled like /login) that was
   never meant to be public;
2. a route added *after* a gate that never existed on that method (no gate at
   all).

``tests/test_route_auth.py`` pins the observable behavior of a hand-maintained
list of probes. That list drifts: an endpoint nobody added to it passes the
suite anonymously. This test instead reads ``orchestration/web_server.py``
with :mod:`ast` and requires that every path dispatched *before* the gate on
each method is a member of an explicit, minimal allowlist. Anything else —
or a method whose gate disappears — fails here, loudly, until a human either
moves the route below the gate or consciously widens the allowlist.

This test performs no I/O, imports nothing from the server, and needs no
database; it is pure source analysis, so it is also exercised by linters and
runs in milliseconds.
"""

from __future__ import annotations

import ast
from pathlib import Path

SRC = Path(__file__).resolve().parent.parent / "orchestration" / "web_server.py"
HANDLER = "Handler"

#: The gate each method must call before dispatching anything else.
GATE = {"do_GET": "_require_read", "do_POST": "_require_write",
        "do_DELETE": "_require_write", "do_PATCH": "_require_write"}

#: Collections from which pre-gate ``path in <name>`` is acceptable: the static
#: asset maps. ``PWA_ASSETS`` lives in web_server.py; ``web_ui.STATIC`` in
#: web_ui.py. Both hold only public, content-hashed or otherwise non-sensitive
#: bytes; reading them goes nowhere near project data.
ALLOWED_DICT_DISPATCH = {"PWA_ASSETS", "STATIC"}

#: Path literals each method may dispatch before its gate, and why every entry
#: belongs there. Widening this list is a security-relevant edit: every entry
#: must be reachable with no credentials and must expose no project data.
PRE_GATE_ALLOWED = {
    # Public entry points. "/" serves the static studio shell (or redirects an
    # anonymous browser to /login); the shell holds no data — every /api/*
    # route stays behind the gate and the static assets are public by design.
    "do_GET": {"/", "/login", "/login/state", "/healthz", "/readyz"},
    # /login: issues a session (rate limited). /logout: ends one; it cannot
    # grant access to anything and only clears the caller's own cookie.
    "do_POST": {"/login", "/logout"},
    # No dispatch before the gate.
    "do_DELETE": set(),
    "do_PATCH": set(),
}


def _flatten_preorder(node: ast.AST):
    """Depth-first pre-order traversal (execution order).

    ``ast.walk`` is breadth-first and scrambles statement order across nesting
    levels; this is the traversal whose indices are meaningful for "what ran
    before the gate".
    """
    yield node
    for child in ast.iter_child_nodes(node):
        yield from _flatten_preorder(child)


def _find_handler_class(tree: ast.AST) -> ast.ClassDef:
    for node in ast.walk(tree):
        if isinstance(node, ast.ClassDef) and node.name == HANDLER:
            return node
    raise AssertionError(f"class {HANDLER} not found in {SRC} "
                         "(rename this test's expectation if the handler moved)")


def _gate_index(flat: list[ast.AST], gate: str) -> int:
    for i, node in enumerate(flat):
        if (isinstance(node, ast.Call)
                and isinstance(node.func, ast.Attribute)
                and node.func.attr == gate):
            return i
    return -1


def _str_consts(node: ast.AST):
    """Yield the string constants of *node* (a Tuple/Set of them, or one)."""
    if isinstance(node, ast.Constant) and isinstance(node.value, str):
        yield node.value
    elif isinstance(node, (ast.Tuple, ast.Set, ast.List)):
        for elt in node.elts:
            yield from _str_consts(elt)


def _is_path(node: ast.AST) -> bool:
    return isinstance(node, ast.Name) and node.id == "path"


def _pre_gate_nodes(func: ast.FunctionDef, gate: str) -> list[ast.AST]:
    flat = list(_flatten_preorder(func))
    idx = _gate_index(flat, gate)
    if idx < 0:
        return []  # caller turns this into a failure
    return flat[:idx]


def _collect(pre: list[ast.AST], method: str) -> tuple[set[str], list[str]]:
    literals: set[str] = set()
    problems: list[str] = []
    allowed_dicts = ALLOWED_DICT_DISPATCH
    for node in pre:
        if isinstance(node, ast.Compare):
            # path == "x" / "x" == path / path in ("a", ...) / path in DICT
            pairs = [(node.left, c, op) for op, c in zip(node.ops, node.comparators)]
            for left, comparator, op in pairs:
                other = comparator if _is_path(left) else (left if _is_path(comparator) else None)
                if other is None:
                    continue
                if isinstance(op, ast.Eq):
                    consts = list(_str_consts(other))
                    if len(consts) == 1:
                        literals.add(consts[0])
                    else:
                        problems.append(f"non-literal equality with path: {ast.dump(node)}")
                elif isinstance(op, (ast.In, ast.NotIn)):
                    is_allowed_map = (
                        (isinstance(other, ast.Name) and other.id in allowed_dicts)
                        or (isinstance(other, ast.Attribute) and other.attr in allowed_dicts)
                    )
                    if is_allowed_map:
                        continue  # path in PWA_ASSETS / web_ui.STATIC — static, public
                    consts = list(_str_consts(other))
                    if consts:
                        literals.update(consts)
                    else:
                        problems.append(f"path {'not ' if isinstance(op, ast.NotIn) else ''}in "
                                        f"non-literal collection before the gate: {ast.dump(node)}")
                else:
                    problems.append(f"unsupported path comparison before the gate: {ast.dump(node)}")
        elif isinstance(node, ast.Call):
            # path.startswith("...") / path.endswith("...") before the gate
            func = node.func
            if (isinstance(func, ast.Attribute) and isinstance(func.value, ast.Name)
                    and func.value.id == "path"):
                problems.append(f"path.{func.attr}(...) before the gate is not an allowed dispatch")
    return literals, problems


def test_handler_class_exists_and_gates_are_called():
    tree = ast.parse(SRC.read_text(encoding="utf-8"))
    handler = _find_handler_class(tree)
    methods = {n.name: n for n in handler.body if isinstance(n, ast.FunctionDef)}
    for method, gate in GATE.items():
        func = methods.get(method)
        assert func is not None, f"{method} disappeared from {HANDLER}"
        flat = list(_flatten_preorder(func))
        assert _gate_index(flat, gate) >= 0, (
            f"{method} no longer calls self.{gate}(); the write/read gate is the "
            "only thing standing between an anonymous request and every route — "
            "restore it or consciously widen this guard")


def test_no_dispatch_before_the_auth_gate():
    tree = ast.parse(SRC.read_text(encoding="utf-8"))
    handler = _find_handler_class(tree)
    methods = {n.name: n for n in handler.body if isinstance(n, ast.FunctionDef)}
    for method, gate in GATE.items():
        func = methods[method]
        pre = _pre_gate_nodes(func, gate)
        assert pre, (
            f"could not locate the pre-gate region of {method}; "
            "refusing to pass on a blind guard")
        literals, problems = _collect(pre, method)
        unknown = literals - PRE_GATE_ALLOWED[method]
        assert not unknown, (
            f"{method} dispatches path(s) before self.{gate}() that are not in the "
            f"public allowlist: {sorted(unknown)}. Move the route below the gate, or — "
            "if it is genuinely public — extend PRE_GATE_ALLOWED with a comment "
            "explaining why it exposes no project data.")
        assert not problems, f"{method} pre-gate dispatch problems: {problems}"


def test_pre_gate_allowlists_are_exact():
    """Every entry of the allowlist must actually be reachable pre-gate.

    Keeps the allowlist from quietly accumulating permission it no longer
    uses: remove a public route, and its allowlist entry must go with it.
    """
    tree = ast.parse(SRC.read_text(encoding="utf-8"))
    handler = _find_handler_class(tree)
    methods = {n.name: n for n in handler.body if isinstance(n, ast.FunctionDef)}
    for method, allowed in PRE_GATE_ALLOWED.items():
        literals, _ = _collect(_pre_gate_nodes(methods[method], GATE[method]), method)
        stale = allowed - literals
        assert not stale, (
            f"PRE_GATE_ALLOWED[{method!r}] lists {sorted(stale)} but the method no longer "
            "dispatches them before the gate — remove the stale entry")
