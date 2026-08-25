#!/usr/bin/env python3
"""Audit Python files against hard convention #1 (global imports only).

Two independent checks:

1. Use of np./xr./pd./cfgrib. inside a function where that module is
   NOT in scope at all -- the exact pattern that caused the
   '_ffill_time: name xr is not defined' error.
2. A third-party ``import`` statement placed INSIDE a function, which
   CLAUDE.md bans outright even though it does put the name in scope.
   The sanctioned optional-dependency form (an import inside a ``try``
   with an ``ImportError``/``Exception`` handler) is exempt.

Usage:  python audit_imports.py [path.py ...]

With no arguments every ``.py`` under ``src/weather`` is audited.
"""
import ast
import glob
import os
import pathlib
import sys

WATCH = {"np", "xr", "pd", "cfgrib", "warnings"}

#: Hard runtime dependencies -- pyproject's [project].dependencies.
#: No install can be missing these, so a function-local import of one
#: is pure convention drift and check 2 flags it.
#:
#: Everything else this repo uses (xarray, netCDF4, cfgrib, eccodes,
#: dask, scipy, pvlib, matplotlib) sits behind an optional extra, so a
#: deliberate function-local import there is what keeps a light install
#: importable -- see pyproject's `pointquery` extra. Those are left to
#: check 1, which still catches genuinely out-of-scope usage.
HARD_DEPS = {"numpy", "pandas", "requests", "urllib3", "dotenv"}


def _expand(args):
    """Expand glob patterns that the shell (e.g. PowerShell) left literal.

    With no arguments, audit every module under ``src/weather`` -- an
    empty argv previously made the whole run a silent no-op that still
    exited 0.
    """
    if not args:
        root = pathlib.Path(__file__).resolve().parent.parent
        return sorted(str(p) for p in root.rglob("*.py"))
    paths = []
    for a in args:
        if os.path.isfile(a):
            paths.append(a)
            continue
        matches = glob.glob(a)
        if not matches:
            print(f"[WARN] no files matched: {a}", file=sys.stderr)
        paths.extend(matches)
    return paths


def _assigned_names(stmts):
    """Names bound at this level via ``import``/``from`` OR a plain
    single-target assignment (``xr = _import_xarray()``,
    ``xr = pytest.importorskip("xarray")``, etc.) -- both genuinely put
    the name in scope, same as a real import statement. Two real
    examples in this repo use the latter (a lazy-import helper in
    point_query.py, and pytest's own importorskip idiom in tests) --
    without this, both are false positives below.
    """
    names = set()
    for n in stmts:
        if isinstance(n, (ast.Import, ast.ImportFrom)):
            for a in n.names:
                names.add(a.asname or a.name)
        elif (
            isinstance(n, ast.Assign)
            and len(n.targets) == 1
            and isinstance(n.targets[0], ast.Name)
        ):
            names.add(n.targets[0].id)
    return names


def _param_names(fn):
    """Parameter names of *fn*.

    A parameter binds the name in that scope exactly like an import
    does, so ``def f(..., xr)`` must not be reported as out-of-scope
    usage -- this repo really does pass ``xr`` in that way.
    """
    args = fn.args
    names = {
        p.arg
        for p in (
            list(args.posonlyargs) + list(args.args) + list(args.kwonlyargs)
        )
    }
    if args.vararg:
        names.add(args.vararg.arg)
    if args.kwarg:
        names.add(args.kwarg.arg)
    return names


def _optional_dep_linenos(fn):
    """Line numbers of imports using the sanctioned optional-dep form.

    That is, an ``import`` guarded by ``try: ... except ImportError``
    (or a bare ``except Exception``), which CLAUDE.md explicitly allows
    as the one exception to global-imports-only.
    """
    exempt = set()
    for node in ast.walk(fn):
        if not isinstance(node, ast.Try):
            continue
        caught = []
        for h in node.handlers:
            caught.extend(
                n.id for n in ast.walk(h) if isinstance(n, ast.Name)
            )
        if not {"ImportError", "Exception", "ModuleNotFoundError"} & set(
            caught
        ):
            continue
        for stmt in node.body:
            for n in ast.walk(stmt):
                if isinstance(n, (ast.Import, ast.ImportFrom)):
                    exempt.add(n.lineno)
    return exempt


def _nested_imports(fn):
    """Hard-dependency ``import`` statements inside a function body."""
    exempt = _optional_dep_linenos(fn)
    found = []
    for node in ast.walk(fn):
        if not isinstance(node, (ast.Import, ast.ImportFrom)):
            continue
        if node.lineno in exempt:
            continue
        if isinstance(node, ast.ImportFrom):
            roots = [(node.module or "").split(".")[0]]
        else:
            roots = [a.name.split(".")[0] for a in node.names]
        for root in roots:
            if root in HARD_DEPS:
                found.append((node.lineno, root))
    return found


def audit(path):
    with open(path) as fh:
        src = fh.read()
    mod = ast.parse(src)
    top = _assigned_names(mod.body)
    hits = []
    for fn in ast.walk(mod):
        if not isinstance(fn, ast.FunctionDef):
            continue
        local = _assigned_names(ast.walk(fn)) | _param_names(fn)
        for lineno, root in _nested_imports(fn):
            hits.append(
                (fn.name, lineno, f"import {root}  <- move to module top")
            )
        for stmt in fn.body:  # body only -> skips annotations
            for n in ast.walk(stmt):
                if (isinstance(n, ast.Attribute)
                        and isinstance(n.value, ast.Name)
                        and n.value.id in WATCH
                        and n.value.id not in top
                        and n.value.id not in local):
                    hits.append((fn.name, n.lineno, f"{n.value.id}.{n.attr}"))
    if hits:
        print(f"[FAIL] {path}")
        for fname, lineno, e in hits:
            suffix = "" if "<-" in e else "  <- not imported in scope"
            print(f"    {fname}  line {lineno}:  {e}{suffix}")
    else:
        print(f"[ OK ] {path}")
    return not hits


if __name__ == "__main__":
    # NB: list, not a generator -- all() short-circuits, which
    # silently stopped the sweep at the first failing file.
    ok = all([audit(p) for p in _expand(sys.argv[1:])])
    sys.exit(0 if ok else 1)
