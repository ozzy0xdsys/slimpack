"""Dependency graph resolution for slimpack.

Hybrid strategy:
  * static pass  - walk the AST of every reachable module, following
                   ``import`` / ``from ... import`` (absolute and relative) and
                   literal ``__import__`` / ``importlib.import_module`` calls.
  * trace pass   - (optional, --trace) run the target under an import hook and
                   record every module actually imported at runtime, then merge.
  * includes     - (--include NAME) force a module and its subgraph in.

Resolution never imports third-party code: it locates modules with
``PathFinder.find_spec`` against an explicit search path, plus the builtin and
frozen finders for top-level names.
"""

from __future__ import annotations

import ast
import os
import sys
import importlib.machinery as machinery
from dataclasses import dataclass, field


@dataclass
class Module:
    name: str
    kind: str            # source | bytecode | extension | builtin | frozen | namespace
    origin: str | None   # file path, or None
    is_package: bool = False
    search_locations: list[str] = field(default_factory=list)


class Analyzer:
    def __init__(self, source_roots, verbose=False):
        self.source_roots = [os.path.abspath(p) for p in source_roots]
        self.verbose = verbose
        # Search path for locating modules: our source roots first, then the
        # stdlib / DLLs / site-packages of the interpreter running slimpack
        # (this is also the interpreter that will be bundled).
        clean = []
        here = os.path.dirname(os.path.abspath(__file__))
        for p in sys.path:
            if not p:
                continue
            ap = os.path.abspath(p)
            if ap == here or ap == os.path.dirname(here):
                continue   # don't resolve user imports into slimpack itself
            clean.append(ap)
        self.search_path = self.source_roots + clean
        self.modules: dict[str, Module] = {}
        self.missing: set[str] = set()
        self.edges: dict[str, str] = {}   # module -> first referrer (for debug)
        self._current = None

    def log(self, *a):
        if self.verbose:
            print("[analyze]", *a)

    # -- location -----------------------------------------------------------
    def _locate(self, fullname: str, parent_locations):
        # Builtin and frozen modules live inside python3XX.dll and win at import
        # time over anything on sys.path, so they are recorded but never shipped.
        if "." not in fullname and fullname in sys.builtin_module_names:
            return Module(fullname, "builtin", None)
        try:
            fs = machinery.FrozenImporter.find_spec(fullname)
        except Exception:
            fs = None
        if fs is not None:
            is_pkg = fs.submodule_search_locations is not None
            locs = list(fs.submodule_search_locations or [])
            if is_pkg and not locs:
                # Need the real directory so frozen-package children resolve.
                try:
                    ps = machinery.PathFinder.find_spec(fullname, parent_locations)
                except Exception:
                    ps = None
                if ps is not None and ps.submodule_search_locations:
                    locs = list(ps.submodule_search_locations)
            return Module(fullname, "frozen", None, is_pkg, locs)
        path = parent_locations if parent_locations is not None else self.search_path
        try:
            spec = machinery.PathFinder.find_spec(fullname, path)
        except Exception:
            spec = None
        if spec is None:
            return None
        loc = list(spec.submodule_search_locations or [])
        is_pkg = bool(spec.submodule_search_locations is not None)
        if spec.origin in (None, "namespace") and is_pkg:
            return Module(fullname, "namespace", None, True, loc)
        origin = spec.origin
        if isinstance(spec.loader, machinery.ExtensionFileLoader):
            kind = "extension"
        elif origin and origin.endswith(".pyc"):
            kind = "bytecode"
        else:
            kind = "source"
        return Module(fullname, kind, origin, is_pkg, loc)

    def resolve(self, fullname: str) -> Module | None:
        """Resolve a dotted name, pulling in every parent package too."""
        if fullname in self.modules:
            return self.modules[fullname]
        if fullname in self.missing:
            return None
        if self._current is not None:
            self.edges.setdefault(fullname, self._current)
        parts = fullname.split(".")
        parent_locations = None
        resolved = None
        for i in range(len(parts)):
            partial = ".".join(parts[: i + 1])
            if partial in self.modules:
                resolved = self.modules[partial]
                parent_locations = resolved.search_locations or None
                continue
            m = self._locate(partial, parent_locations)
            if m is None:
                self.missing.add(fullname)
                return None
            self.modules[partial] = m
            self._visit(m)
            resolved = m
            parent_locations = m.search_locations or None
        return resolved

    # -- AST walk -----------------------------------------------------------
    def _visit(self, mod: Module):
        if mod.kind not in ("source",) or not mod.origin:
            return
        try:
            with open(mod.origin, "r", encoding="utf-8-sig") as f:   # tolerate a BOM
                src = f.read()
        except (OSError, UnicodeDecodeError):
            return
        try:
            tree = ast.parse(src, mod.origin)
        except SyntaxError:
            return
        prev = self._current
        self._current = mod.name
        for name in self._imports_in(tree, mod):
            self.resolve(name)
        self._current = prev

    def _imports_in(self, tree, mod: Module):
        names: list[str] = []
        mod_top = mod.name.split(".")[0]

        def keep(name, in_func):
            # Inside a function body, only follow imports that stay within this
            # module's own top-level package (relative imports always do).
            return (not in_func) or (name and name.split(".")[0] == mod_top)

        for node, in_func in self._import_time_nodes(tree):
            if isinstance(node, ast.Import):
                for alias in node.names:
                    if keep(alias.name, in_func):
                        names.append(alias.name)
            elif isinstance(node, ast.ImportFrom):
                base = self._resolve_relative(mod, node.level, node.module)
                if base is None:
                    continue
                if not keep(base, in_func):
                    continue
                if base:
                    names.append(base)
                for alias in node.names:
                    if alias.name == "*":
                        continue
                    if base:
                        names.append(base + "." + alias.name)
            elif isinstance(node, ast.Call) and not in_func:
                lit = self._dynamic_literal(node, mod)
                if lit:
                    names.append(lit)
        return names

    def _import_time_nodes(self, tree):
        """Yield the import nodes that matter.

        At module level (and inside if/try/with/for/while/class bodies, which
        run on import) we take every import. Inside function/lambda bodies
        (lazy, run only if called) we take ONLY relative imports
        (``from . import x``): those pull sibling submodules of a package that
        is already in use - bounded and almost always needed - while skipping
        absolute lazy imports, which would drag in half the stdlib (tokenize's
        ``import argparse``, pdb's ``import asyncio``). Use --trace/--include
        for absolute lazy imports."""
        out = []

        def visit(node, in_func):
            for child in ast.iter_child_nodes(node):
                if isinstance(child, (ast.FunctionDef, ast.AsyncFunctionDef, ast.Lambda)):
                    visit(child, True)
                    continue
                if isinstance(child, ast.Try) and _is_optional_try(child):
                    continue   # try/except ImportError -> optional, skip it
                if isinstance(child, (ast.Import, ast.ImportFrom, ast.Call)):
                    out.append((child, in_func))
                visit(child, in_func)

        visit(tree, False)
        return out

    def _resolve_relative(self, mod: Module, level: int, module):
        if level == 0:
            return module
        bits = mod.name.split(".")
        if mod.is_package:
            base_parts = bits[: len(bits) - (level - 1)]
        else:
            base_parts = bits[: len(bits) - level]
        if base_parts and base_parts[0] == "":
            return None
        base = ".".join(base_parts)
        if module:
            base = base + "." + module if base else module
        return base or None

    def _dynamic_literal(self, node: ast.Call, mod: Module):
        """Catch __import__('x') / importlib.import_module('x'[, 'pkg'])."""
        func = node.func
        fname = None
        if isinstance(func, ast.Name):
            fname = func.id
        elif isinstance(func, ast.Attribute):
            fname = func.attr
        if fname not in ("__import__", "import_module"):
            return None
        if not node.args:
            return None
        arg0 = node.args[0]
        if not (isinstance(arg0, ast.Constant) and isinstance(arg0.value, str)):
            return None
        name = arg0.value
        if name.startswith("."):
            # relative import_module; second arg is the anchor package
            pkg = None
            if len(node.args) > 1 and isinstance(node.args[1], ast.Constant):
                pkg = node.args[1].value
            if not pkg:
                pkg = mod.name if mod.is_package else mod.name.rpartition(".")[0]
            level = len(name) - len(name.lstrip("."))
            tail = name[level:]
            bits = pkg.split(".")
            base = ".".join(bits[: len(bits) - (level - 1)]) if level > 1 else pkg
            return (base + "." + tail) if tail else base
        return name

    # -- public entry points ------------------------------------------------
    def add_entry(self, entry_path: str):
        name = "__main__"
        m = Module(name, "source", os.path.abspath(entry_path), False)
        self.modules[name] = m
        self._visit(m)

    def add_include(self, name: str):
        self.resolve(name)

    def merge_traced(self, names):
        for n in names:
            self.resolve(n)


# Modules the CPython runtime needs at startup that are not discoverable by
# walking the user's imports (codecs are loaded by name by the interpreter).
# Frozen ones (abc, io, os, site, ...) live inside the DLL and are filtered out
# later, so this list is deliberately just the codec machinery.
BASE_RUNTIME_MODULES = [
    "encodings",
    "encodings.aliases",
    "encodings.utf_8",
    "encodings.ascii",
    "encodings.latin_1",
    "encodings.cp1252",
    "encodings.cp437",
    "encodings.utf_16_le",
    "encodings.utf_16",
    "encodings.oem",
    "encodings.mbcs",
]


def _exc_name(node):
    if isinstance(node, ast.Name):
        return node.id
    if isinstance(node, ast.Attribute):
        return node.attr
    return ""


def _is_optional_try(node):
    """A module-level try whose except catches ImportError (or is bare/broad)
    marks its imports as optional - we don't bundle optional backends."""
    optional = {"ImportError", "ModuleNotFoundError", "Exception", "BaseException"}
    for h in node.handlers:
        if h.type is None:
            return True
        elts = h.type.elts if isinstance(h.type, ast.Tuple) else [h.type]
        if any(_exc_name(e) in optional for e in elts):
            return True
    return False


def run_trace(entry_path, source_roots, args, scratch_dir, verbose=False):
    """Execute the target under an import tracer; return the set of module
    names imported at runtime. Runs the program for real."""
    import subprocess
    import json

    tracer = os.path.join(scratch_dir, "_slimpack_tracer.py")
    out_file = os.path.join(scratch_dir, "_slimpack_traced.json")
    entry_abs = os.path.abspath(entry_path)
    with open(tracer, "w", encoding="utf-8") as f:
        f.write(_TRACER_SRC)

    env = dict(os.environ)
    extra = os.pathsep.join([scratch_dir] + [os.path.abspath(p) for p in source_roots])
    env["PYTHONPATH"] = extra + os.pathsep + env.get("PYTHONPATH", "")
    env["SLIMPACK_TRACE_OUT"] = out_file
    env["SLIMPACK_TRACE_ENTRY"] = entry_abs

    cmd = [sys.executable, tracer] + list(args)
    if verbose:
        print("[trace] running:", " ".join(cmd))
    try:
        subprocess.run(cmd, env=env, cwd=os.path.dirname(entry_abs) or ".",
                       timeout=120)
    except subprocess.TimeoutExpired:
        print("[trace] warning: target timed out; using modules seen so far")

    try:
        with open(out_file, "r", encoding="utf-8") as f:
            return set(json.load(f))
    except OSError:
        return set()


_TRACER_SRC = r'''
import sys, os, json, atexit, runpy

_seen = set()

class _Tracer:
    def find_spec(self, fullname, path=None, target=None):
        _seen.add(fullname)
        return None  # let the real finders handle it

sys.meta_path.insert(0, _Tracer())

def _dump():
    out = os.environ.get("SLIMPACK_TRACE_OUT")
    if out:
        try:
            with open(out, "w", encoding="utf-8") as f:
                json.dump(sorted(_seen), f)
        except OSError:
            pass

atexit.register(_dump)

entry = os.environ["SLIMPACK_TRACE_ENTRY"]
sys.argv = [entry] + sys.argv[1:]
try:
    runpy.run_path(entry, run_name="__main__")
except SystemExit:
    pass
except Exception as e:
    sys.stderr.write("[trace] target raised: %r\n" % (e,))
finally:
    _dump()
'''
