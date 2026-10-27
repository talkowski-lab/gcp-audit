#!/usr/bin/env python3
"""lint_readonly.py — fail if anything in `src/gcp_audit/**` or `tests/**` can reach a wire.

The guard refuses requests it does not admit; this lint checks that nothing in the tree can
reach around it.

* **banned-import** — `subprocess`, `socket`, `urllib.request`, `http.client`, `requests`,
  `importlib` and the like, except as `IMPORT_EXCEPTIONS` names.
* **banned-call** — `os.system`/`os.popen`, `exec*`/`spawn*`, `eval`, `exec`, `compile`,
  `__import__`: a shelled-out client bypasses every layer.
* **dynamic-attribute** — non-constant `getattr`: a name built at runtime turns a checked call into
  an unchecked one.
* **http-mutation** — `.post/.put/.patch/.delete/.head/.options(...)` and `allow_redirects=True`.
* **lint-allow-count** — `# lint-allow` skips its own line only, needs a reason there, and is
  pinned per file in `ALLOW_PIN`.

`tests/test_readonly_lint.py` plants each violation and fails if this script passes it.
"""
from __future__ import annotations

import argparse
import ast
import sys
from pathlib import Path

BANNED_IMPORTS = {
    "subprocess", "socket", "urllib.request", "http.client", "urllib3", "requests",
    "importlib", "asyncio", "admin", "multiprocessing", "ctypes", "pty", "fcntl",
}
BANNED_CALLS = {"eval", "exec", "compile", "__import__", "breakpoint"}
BANNED_ATTR_CALLS = {("os", "system"), ("os", "popen"), ("os", "execv"), ("os", "execve"),
                     ("os", "execvp"), ("os", "spawnl"), ("os", "spawnv"), ("os", "spawnve"),
                     ("posix", "system"), ("pty", "spawn")}
MUTATING_METHODS = {"post", "put", "patch", "delete", "head", "options", "request_raw",
                    "send", "urlopen", "open_connection", "connection"}

#: Files allowed one banned import: `guard.py` is the only code that touches a wire, and
#: `tests/conftest.py` imports `socket` to block it for the whole suite.
IMPORT_EXCEPTIONS: dict[str, set[str]] = {
    "src/gcp_audit/guard.py": {"requests"},
    "tests/conftest.py": {"socket"},
}

#: Pinned per-file count of `# lint-allow` markers; bump it in a commit that says why.
ALLOW_PIN: dict[str, int] = {}

MARKER = "# lint-allow"


def python_files(root: Path) -> list[Path]:
    return sorted(p for p in root.rglob("*.py") if "__pycache__" not in p.parts)


def _attr_chain(node: ast.Attribute) -> str | None:
    parts: list[str] = []
    cur: ast.AST = node
    while isinstance(cur, ast.Attribute):
        parts.append(cur.attr)
        cur = cur.value
    if isinstance(cur, ast.Name):
        parts.append(cur.id)
        return ".".join(reversed(parts))
    return None


def _is_dotted_name(node: ast.AST) -> str | None:
    if isinstance(node, ast.Name):
        return node.id
    if isinstance(node, ast.Attribute):
        return _attr_chain(node)
    return None


class Visitor(ast.NodeVisitor):
    def __init__(self, path: str, lines: list[str]) -> None:
        self.path = path
        self.lines = lines
        self.hits: list[str] = []
        self.allows = 0

    def _allowed(self, node: ast.AST) -> bool:
        """A `# lint-allow` comment skips its own line only, and needs a reason on that line."""
        lineno = getattr(node, "lineno", 1)
        if lineno > len(self.lines) or MARKER not in self.lines[lineno - 1]:
            return False
        reason = self.lines[lineno - 1].split(MARKER, 1)[1].strip().lstrip(":—- ").strip()
        if len(reason) < 4:
            self.hits.append(f"{self.path}:{lineno}: lint-allow without a reason on the line")
        self.allows += 1
        return True

    def visit_Import(self, node: ast.Import) -> None:
        permitted = IMPORT_EXCEPTIONS.get(self.path, set())
        for alias in node.names:
            root, _, tail = alias.name.partition(".")
            banned = alias.name in BANNED_IMPORTS or root in BANNED_IMPORTS or (
                root in {"urllib", "http"} and not tail)
            if not banned:
                continue
            if alias.name in permitted or root in permitted or self._allowed(node):
                continue
            self.hits.append(f"{self.path}:{node.lineno}: banned-import {alias.name}")
        self.generic_visit(node)

    def visit_ImportFrom(self, node: ast.ImportFrom) -> None:
        mod = node.module or ""
        if mod in BANNED_IMPORTS or mod.split(".")[0] in {"subprocess", "urllib", "http",
                                                          "importlib", "admin"}:
            if mod not in IMPORT_EXCEPTIONS.get(self.path, set()) and not self._allowed(node):
                self.hits.append(f"{self.path}:{node.lineno}: banned-import from {mod}")
        self.generic_visit(node)

    def visit_Call(self, node: ast.Call) -> None:
        called = _is_dotted_name(node.func)
        if called:
            if called.split(".")[0] in {"os"} and (called.split(".")[0], called.split(".")[-1]) \
                    in BANNED_ATTR_CALLS:
                if not self._allowed(node):
                    self.hits.append(f"{self.path}:{node.lineno}: banned-call {called}")
            if called in BANNED_CALLS and not self._allowed(node):
                self.hits.append(f"{self.path}:{node.lineno}: banned-call {called}()")
            tail = called.split(".")[-1]
            if tail in MUTATING_METHODS and called.split(".")[0] != "guard" \
                    and not self._allowed(node):
                self.hits.append(f"{self.path}:{node.lineno}: http-mutation {called}() — go "
                                 f"through guard.read(); only guard._dispatch touches a wire")
        if isinstance(node.func, ast.Name) and node.func.id == "getattr":
            if len(node.args) >= 2 and not isinstance(node.args[1], ast.Constant):
                if not self._allowed(node):
                    self.hits.append(f"{self.path}:{node.lineno}: dynamic-attribute getattr with "
                                     "a name built at runtime")
        for keyword in node.keywords:
            if keyword.arg == "allow_redirects":
                value = keyword.value
                truthy = isinstance(value, ast.Constant) and value.value is not False
                if truthy and not self._allowed(node):
                    self.hits.append(f"{self.path}:{node.lineno}: allow_redirects=True — a "
                                     "redirect is a second request that nothing admitted")
        self.generic_visit(node)


def lint(paths: list[Path], root: Path) -> tuple[list[str], dict[str, int]]:
    hits: list[str] = []
    counts: dict[str, int] = {}
    for path in paths:
        text = path.read_text(encoding="utf-8")
        lines = text.splitlines()
        try:
            tree = ast.parse(text, filename=str(path))
        except SyntaxError as exc:
            hits.append(f"{path}:{exc.lineno}: SyntaxError: {exc.msg}")
            continue
        rel = str(path.relative_to(root))
        visitor = Visitor(rel, lines)
        visitor.visit(tree)
        hits.extend(visitor.hits)
        counts[rel] = visitor.allows
    for rel, pinned in ALLOW_PIN.items():
        if counts.get(rel, 0) != pinned:
            hits.append(f"{rel}: lint-allow-count — {ALLOW_PIN[rel]} pinned, "
                        f"{counts.get(rel, 0)} present")
    return hits, counts


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    ap.add_argument("--root", default=".", type=Path)
    ap.add_argument("--quiet", action="store_true")
    args = ap.parse_args(argv)
    root = args.root.resolve()
    targets: list[Path] = []
    for sub in ("src/gcp_audit", "tests", "tools"):
        base = root / sub
        if base.exists():
            targets.extend(python_files(base))
    hits, counts = lint(targets, root)
    for hit in hits:
        print(hit)
    if not args.quiet:
        print(f"lint_readonly: {len(targets)} files scanned, {len(hits)} findings, "
              f"{sum(counts.values())} justified allowances")
    return 1 if hits else 0


if __name__ == "__main__":
    sys.exit(main())
