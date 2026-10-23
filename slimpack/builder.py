"""The slimpack build pipeline.

Turns an analyzed module graph into the smallest working Windows .exe:

  1. compile every needed pure module to optimize=2 bytecode (no docstrings,
     no asserts) and pack it into python_modules.zip (tree-shaken: only the
     modules the graph touches).
  2. assemble a minimal CPython runtime - python3XX.dll, vcruntime, only the
     .pyd extensions used, and any DLLs those extensions need - never the whole
     stdlib or install.
  3. compile the tiny C launcher stub and, for --onefile, LZMA-compress the
     whole runtime into an overlay appended to the stub.
  4. UPX everything it is safe to UPX.
  5. report where the bytes went.
"""

from __future__ import annotations

import glob
import marshal
import os
import shutil
import struct
import subprocess
import sys
import zipfile
import importlib.util
from dataclasses import dataclass, field

from . import analyzer
from .pescan import imported_dlls


NATIVE_DIR = os.path.join(os.path.dirname(os.path.abspath(__file__)), "native")
PYVER = f"{sys.version_info.major}{sys.version_info.minor}"
PYDLL = f"python{PYVER}.dll"


# ----------------------------------------------------------------------------
@dataclass
class BuildConfig:
    entry: str
    output: str | None = None
    onefile: bool = False
    trace: bool = False
    includes: list[str] = field(default_factory=list)
    paths: list[str] = field(default_factory=list)
    exclude: list[str] = field(default_factory=list)
    add_data: list[str] = field(default_factory=list)
    report: bool = False
    verbose: bool = False
    keep_build: bool = False
    no_upx: bool = False
    scratch: str = "."


def human(n: int) -> str:
    if n < 1024:
        return f"{n} B"
    if n < 1024 ** 2:
        return f"{n/1024:.1f} KB"
    if n < 1024 ** 3:
        return f"{n/1024**2:.2f} MB"
    return f"{n/1024**3:.2f} GB"


def which(name):
    return shutil.which(name)


def find_upx():
    """Locate upx.exe even when it isn't on PATH (e.g. freshly winget-installed)."""
    import glob
    env = os.environ.get("SLIMPACK_UPX")
    if env and os.path.exists(env):
        return env
    p = shutil.which("upx")
    if p:
        return p
    localapp = os.environ.get("LOCALAPPDATA", "")
    patterns = [
        os.path.join(localapp, "Microsoft", "WinGet", "Packages", "UPX.UPX*", "**", "upx.exe"),
        os.path.join(os.environ.get("ProgramData", ""), "chocolatey", "bin", "upx.exe"),
        os.path.join(localapp, "Microsoft", "WinGet", "Links", "upx.exe"),
    ]
    su = os.environ.get("SCOOP") or os.path.join(os.environ.get("USERPROFILE", ""), "scoop")
    patterns.append(os.path.join(su, "shims", "upx.exe"))
    for pat in patterns:
        hits = glob.glob(pat, recursive=True)
        if hits:
            return hits[0]
    return None


def compile_pyc(src_path: str, name: str) -> bytes:
    """Compile a .py source file to a .pyc payload (optimize=2)."""
    with open(src_path, "r", encoding="utf-8-sig") as f:   # utf-8-sig strips a BOM
        source = f.read()
    code = compile(source, name, "exec", optimize=2, dont_inherit=True)
    header = importlib.util.MAGIC_NUMBER + b"\x00" * 12  # flags/mtime/size = 0
    return header + marshal.dumps(code)


def empty_pkg_pyc() -> bytes:
    code = compile("", "<synthetic __init__>", "exec", optimize=2)
    return importlib.util.MAGIC_NUMBER + b"\x00" * 12 + marshal.dumps(code)


# ----------------------------------------------------------------------------
class Builder:
    def __init__(self, cfg: BuildConfig):
        self.cfg = cfg
        self.entry = os.path.abspath(cfg.entry)
        self.name = os.path.splitext(os.path.basename(self.entry))[0]
        self.base_prefix = sys.base_prefix
        self.dll_search = [self.base_prefix, os.path.join(self.base_prefix, "DLLs")]
        self.site_roots = self._site_roots()
        self.exclude = {e.split(".")[0] for e in cfg.exclude}
        self.payload_rows: list[tuple[str, int]] = []   # raw runtime contributors
        self.overlay_size = 0     # onefile: compressed overlay bytes
        self.raw_payload = 0      # onefile: uncompressed archive bytes
        self.stub_size = 0
        self.modules: dict[str, analyzer.Module] = {}

    def log(self, *a):
        if self.cfg.verbose:
            print("[build]", *a)

    def _site_roots(self):
        import sysconfig, site
        roots = set()
        try:
            roots.add(os.path.abspath(sysconfig.get_paths()["purelib"]))
        except Exception:
            pass
        try:
            for p in site.getsitepackages():
                roots.add(os.path.abspath(p))
            roots.add(os.path.abspath(site.getusersitepackages()))
        except Exception:
            pass
        # Keep only real site-packages-style dirs, not the prefix root itself
        return {r for r in roots if r.lower().endswith("site-packages")}

    def _is_thirdparty(self, path):
        if not path:
            return False
        ap = os.path.abspath(path)
        return any(ap.lower().startswith(r.lower() + os.sep) for r in self.site_roots)

    def _top(self, name):
        return name.split(".")[0]

    # -- analysis -----------------------------------------------------------
    def analyze(self):
        source_roots = [os.path.dirname(self.entry)] + [os.path.abspath(p) for p in self.cfg.paths]
        az = analyzer.Analyzer(source_roots, verbose=self.cfg.verbose)
        az.add_entry(self.entry)
        for name in analyzer.BASE_RUNTIME_MODULES:
            az.add_include(name)
        for name in self.cfg.includes:
            az.add_include(name)
        if self.cfg.trace:
            traced = analyzer.run_trace(self.entry, source_roots, [], self.cfg.scratch,
                                        verbose=self.cfg.verbose)
            self.log(f"trace saw {len(traced)} modules")
            az.merge_traced(traced)
        self._resolve_metadata_deps(az)
        self.az = az
        self.modules = az.modules
        self.missing = az.missing

    def _resolve_metadata_deps(self, az):
        """Pull in each collected third-party package's DECLARED runtime
        dependencies (dist-info `Requires-Dist`, markers evaluated with no
        extras). This catches deps imported dynamically - e.g. requests imports
        idna via `__import__(name)` - that static analysis can't see."""
        try:
            from packaging.requirements import Requirement
        except Exception:
            self.log("packaging not available; skipping metadata dep resolution")
            return
        env = {"extra": ""}
        processed = set()
        for _ in range(50):   # fixpoint, bounded
            tops = set()
            for n, m in az.modules.items():
                if n == "__main__":
                    continue
                loc = m.origin or (m.search_locations[0] if m.search_locations else None)
                if self._is_thirdparty(loc):
                    tops.add(self._top(n))
            tops -= self.exclude
            todo = [t for t in tops if t not in processed]
            if not todo:
                break
            for top in todo:
                processed.add(top)
                di = self._find_dist_info(top)
                if not di:
                    continue
                for line in self._requires_dist(di):
                    try:
                        req = Requirement(line)
                    except Exception:
                        continue
                    if req.marker is not None and not req.marker.evaluate(env):
                        continue   # an extra / platform dep that doesn't apply
                    dep_di = self._find_dist_info_by_distname(req.name)
                    dep_tops = (self._tops_of_dist_info(dep_di) if dep_di
                                else [req.name.replace("-", "_")])
                    for dt in dep_tops:
                        if dt and self._top(dt) not in self.exclude:
                            az.resolve(dt)

    def _requires_dist(self, di):
        out = []
        md = os.path.join(di, "METADATA")
        try:
            with open(md, encoding="utf-8", errors="ignore") as f:
                for line in f:
                    if line.startswith("Requires-Dist:"):
                        out.append(line.split(":", 1)[1].strip())
                    elif not line.strip():
                        break   # blank line ends the header block
        except OSError:
            pass
        return out

    def _find_dist_info_by_distname(self, distname):
        import re as _re
        norm = lambda s: _re.sub(r"[-_.]+", "_", s).lower()
        nd = norm(distname)
        for root in self.site_roots:
            if not os.path.isdir(root):
                continue
            for base in os.listdir(root):
                if base.lower().endswith(".dist-info"):
                    stem = base[: -len(".dist-info")]
                    if norm(stem.rsplit("-", 1)[0]) == nd:
                        return os.path.join(root, base)
        return None

    def _tops_of_dist_info(self, di):
        tl = os.path.join(di, "top_level.txt")
        if os.path.exists(tl):
            try:
                tops = [t for t in open(tl, encoding="utf-8").read().split() if t]
                if tops:
                    return tops
            except OSError:
                pass
        tops = set()
        rec = os.path.join(di, "RECORD")
        if os.path.exists(rec):
            try:
                for line in open(rec, encoding="utf-8", errors="ignore"):
                    p = line.split(",", 1)[0].replace("\\", "/")
                    first = p.split("/", 1)[0]
                    if first.endswith((".dist-info", ".data")):
                        continue
                    if "/" in p:
                        tops.add(first)
                    elif p.endswith(".py"):
                        tops.add(p[:-3])
                    elif p.endswith(".pyd"):
                        tops.add(first.split(".")[0])
            except OSError:
                pass
        return sorted(tops)

    def _pkg_dir(self, top, az):
        """Return the on-disk directory of a third-party top-level package."""
        m = az.modules.get(top)
        if m is None:
            m = az.resolve(top)
        if m is None:
            return None
        if m.origin and os.path.basename(m.origin).startswith("__init__"):
            return os.path.dirname(m.origin)
        if m.is_package and m.search_locations:
            return m.search_locations[0]
        return None

    # -- partition ----------------------------------------------------------
    def partition(self):
        """Split modules into: pure (to pyc), extensions (pyd), and skipped."""
        self.pure = {}        # name -> Module (source/bytecode, not frozen)
        self.exts = {}        # name -> Module (extension)
        self.skipped = {}
        for name, m in self.modules.items():
            if name == "__main__":
                continue
            if m.kind in ("frozen", "builtin"):
                self.skipped[name] = m
            elif m.kind == "extension":
                self.exts[name] = m
            elif m.kind in ("source", "bytecode"):
                self.pure[name] = m
            elif m.kind == "namespace":
                self.pure[name] = m  # emit synthetic __init__ so it imports
            else:
                self.skipped[name] = m

        # Third-party top-level names (anything living in site-packages).
        self.thirdparty_tops = set()
        for name, m in self.modules.items():
            if name == "__main__":
                continue
            loc = m.origin or (m.search_locations[0] if m.search_locations else None)
            if self._is_thirdparty(loc):
                self.thirdparty_tops.add(self._top(name))
        self.thirdparty_tops -= self.exclude

        # Packages shipped as real on-disk directories rather than in the zip:
        #   * third-party packages  -> collected WHOLE (code + data + .pyd +
        #     bundled DLLs), so nothing a real PyPI package needs is missed;
        #   * any package containing a C extension (a .pyd can't load from a zip).
        self.dir_packages = set()
        for name in self.exts:
            if "." in name:
                self.dir_packages.add(self._top(name))
        for top in self.thirdparty_tops:
            tm = self.modules.get(top)
            is_pkg = (tm and tm.is_package) or any(n.startswith(top + ".") for n in self.modules)
            if is_pkg:
                self.dir_packages.add(top)

        # Drop excluded tops entirely.
        if self.exclude:
            for d in (self.pure, self.exts):
                for n in list(d):
                    if self._top(n) in self.exclude:
                        del d[n]

    # -- zip of pure modules ------------------------------------------------
    def build_zip(self, dest_zip: str, stored: bool):
        comp = zipfile.ZIP_STORED if stored else zipfile.ZIP_DEFLATED
        pkgs_with_children = set()
        for name in list(self.pure):
            if "." in name:
                pkgs_with_children.add(name.rpartition(".")[0])

        total = 0
        with zipfile.ZipFile(dest_zip, "w", comp, compresslevel=9) as z:
            for name, m in self.pure.items():
                if name.split(".")[0] in self.dir_packages:
                    continue  # goes to disk instead
                arc = name.replace(".", "/")
                if m.is_package:
                    arc += "/__init__.pyc"
                else:
                    arc += ".pyc"
                if m.kind == "namespace" or not m.origin:
                    data = empty_pkg_pyc()
                else:
                    data = compile_pyc(m.origin, name)
                z.writestr(arc, data)
                total += len(data)
            # synthesize __init__ for any package that has children but no
            # __init__ of its own (namespace packages) so zipimport finds it.
            present = set()
            for name, m in self.pure.items():
                if name.split(".")[0] in self.dir_packages:
                    continue
                if m.is_package:
                    present.add(name)
            for pkg in pkgs_with_children:
                if pkg in self.dir_packages:
                    continue
                if pkg not in present and pkg in self.pure:
                    continue
                if pkg not in present:
                    z.writestr(pkg.replace(".", "/") + "/__init__.pyc", empty_pkg_pyc())
        return total

    # -- runtime assembly ---------------------------------------------------
    def assemble_runtime(self, runtime_dir: str, stored_zip: bool):
        os.makedirs(runtime_dir, exist_ok=True)

        # python DLL + C runtime
        self._copy_native(os.path.join(self.base_prefix, PYDLL), runtime_dir, "python-dll")
        for vc in ("vcruntime140.dll", "vcruntime140_1.dll"):
            p = os.path.join(self.base_prefix, vc)
            if os.path.exists(p):
                self._copy_native(p, runtime_dir, "vcruntime")

        # Third-party packages: collect each WHOLE - all code (.py -> .pyc),
        # data files, .pyd extensions, package-local DLLs, sibling *.libs dirs
        # and .dist-info metadata - so a real PyPI package works unchanged.
        self.collect_thirdparty(runtime_dir)

        # Stdlib / local extension modules (.pyd) that aren't inside a collected
        # third-party package.
        for name, m in self.exts.items():
            if not m.origin:
                continue
            top = self._top(name)
            if top in self.thirdparty_tops and top in self.dir_packages:
                continue   # inside a whole-collected third-party package
            if top in self.dir_packages and "." in name:
                rel = name.replace(".", "/").rpartition("/")[0]
                target = os.path.join(runtime_dir, rel.replace("/", os.sep),
                                      os.path.basename(m.origin))
            else:
                target = os.path.join(runtime_dir, os.path.basename(m.origin))
            self._copy_native(m.origin, os.path.dirname(target), "extension",
                              basename=os.path.basename(target))

        # Non-third-party dir-package pure members (rare: a stdlib package that
        # also contains a C extension) -> emitted to disk as .pyc.
        for name, m in self.pure.items():
            top = self._top(name)
            if top not in self.dir_packages or top in self.thirdparty_tops:
                continue
            rel = name.replace(".", os.sep)
            target = (os.path.join(runtime_dir, rel, "__init__.pyc") if m.is_package
                      else os.path.join(runtime_dir, rel + ".pyc"))
            os.makedirs(os.path.dirname(target), exist_ok=True)
            data = empty_pkg_pyc() if (m.kind == "namespace" or not m.origin) else compile_pyc(m.origin, name)
            with open(target, "wb") as f:
                f.write(data)

        # Extra data requested with --add-data.
        self._collect_add_data(runtime_dir)

        # Follow PE import tables across everything bundled and pull in any
        # still-missing non-system DLL (openssl for _ssl, sqlite3, etc.),
        # flattening it into the runtime root so it loads by name.
        self._resolve_dlls(runtime_dir)

        # module zip (stdlib + local + pure third-party singles)
        zip_path = os.path.join(runtime_dir, "python_modules.zip")
        self.build_zip(zip_path, stored=stored_zip)
        self.payload_rows.append((f"python_modules.zip ({len(self.pure)} modules)",
                                  os.path.getsize(zip_path)))

        # entry as __main__.pyc
        main_pyc = os.path.join(runtime_dir, "__main__.pyc")
        with open(main_pyc, "wb") as f:
            f.write(compile_pyc(self.entry, "__main__"))
        self.payload_rows.append(("__main__.pyc", os.path.getsize(main_pyc)))

        # path configuration file next to the DLL
        with open(os.path.join(runtime_dir, f"python{PYVER}._pth"), "w", encoding="ascii") as f:
            f.write("python_modules.zip\n.\n")

    # -- third-party whole-package collection -------------------------------
    def collect_thirdparty(self, runtime_dir):
        for top in sorted(self.dir_packages & self.thirdparty_tops):
            pkgdir = self._pkg_dir(top, self.az)
            if not pkgdir or not os.path.isdir(pkgdir):
                continue
            size = self._copy_package_tree(pkgdir, os.path.join(runtime_dir, top))
            libs = os.path.join(os.path.dirname(pkgdir), top + ".libs")
            if os.path.isdir(libs):
                size += self._copy_tree_verbatim(libs, os.path.join(runtime_dir, top + ".libs"))
            self.payload_rows.append((f"{top}/  (third-party package)", size))
        self._collect_metadata(runtime_dir)

    def _copy_package_tree(self, srcdir, dstdir):
        total = 0
        skip_ext = (".pyi", ".pyx", ".pxd", ".c", ".h", ".cpp", ".cc", ".so", ".o", ".a")
        for root, dirs, files in os.walk(srcdir):
            parts = root.split(os.sep)
            if "__pycache__" in parts:
                dirs[:] = []
                continue
            rel = os.path.relpath(root, srcdir)
            outdir = dstdir if rel == "." else os.path.join(dstdir, rel)
            for fn in files:
                ext = os.path.splitext(fn)[1].lower()
                if ext in skip_ext:
                    continue
                src = os.path.join(root, fn)
                os.makedirs(outdir, exist_ok=True)
                if ext == ".py":
                    out = os.path.join(outdir, fn + "c")   # foo.py -> foo.pyc
                    try:
                        data = compile_pyc(src, src)
                        with open(out, "wb") as f:
                            f.write(data)
                        total += len(data)
                        continue
                    except (SyntaxError, ValueError):
                        pass   # fall through and copy the source verbatim
                dst = os.path.join(outdir, fn)
                shutil.copy2(src, dst)
                total += os.path.getsize(dst)
        return total

    def _copy_tree_verbatim(self, srcdir, dstdir):
        total = 0
        for root, dirs, files in os.walk(srcdir):
            if "__pycache__" in root.split(os.sep):
                dirs[:] = []
                continue
            rel = os.path.relpath(root, srcdir)
            outdir = dstdir if rel == "." else os.path.join(dstdir, rel)
            os.makedirs(outdir, exist_ok=True)
            for fn in files:
                dst = os.path.join(outdir, fn)
                shutil.copy2(os.path.join(root, fn), dst)
                total += os.path.getsize(dst)
        return total

    def _collect_add_data(self, runtime_dir):
        """--add-data "SRC[;DEST]": copy a file or directory into the runtime
        at DEST (default: SRC's basename), verbatim."""
        for spec in self.cfg.add_data:
            src, _, dest = spec.partition(";")
            src = os.path.abspath(src.strip())
            dest = (dest.strip() or os.path.basename(src.rstrip("/\\")))
            dest = dest.replace("/", os.sep).replace("\\", os.sep).strip(os.sep)
            target = os.path.join(runtime_dir, dest)
            if os.path.isdir(src):
                sz = self._copy_tree_verbatim(src, target)
            elif os.path.isfile(src):
                os.makedirs(os.path.dirname(target) or runtime_dir, exist_ok=True)
                shutil.copy2(src, target)
                sz = os.path.getsize(target)
            else:
                print(f"[slimpack] --add-data: source not found: {src}")
                continue
            self.payload_rows.append((f"{dest}  (added data)", sz))

    def _collect_metadata(self, runtime_dir):
        for top in sorted(self.thirdparty_tops):
            di = self._find_dist_info(top)
            if di:
                dst = os.path.join(runtime_dir, os.path.basename(di))
                if not os.path.exists(dst):
                    sz = self._copy_tree_verbatim(di, dst)
                    self.payload_rows.append((os.path.basename(di) + " (metadata)", sz))

    def _find_dist_info(self, top):
        import re as _re
        norm = lambda s: _re.sub(r"[-_.]+", "_", s).lower()
        ntop = norm(top)
        for root in self.site_roots:
            if not os.path.isdir(root):
                continue
            for base in os.listdir(root):
                if not base.lower().endswith(".dist-info"):
                    continue
                di = os.path.join(root, base)
                tl = os.path.join(di, "top_level.txt")
                if os.path.exists(tl):
                    try:
                        if top in open(tl, encoding="utf-8").read().split():
                            return di
                    except OSError:
                        pass
                stem = base[: -len(".dist-info")]
                if norm(stem.rsplit("-", 1)[0]) == ntop:
                    return di
                rec = os.path.join(di, "RECORD")
                if os.path.exists(rec):
                    try:
                        for line in open(rec, encoding="utf-8", errors="ignore"):
                            first = line.split(",", 1)[0].replace("\\", "/").split("/", 1)[0]
                            if first in (top, top + ".py"):
                                return di
                    except OSError:
                        pass
        return None

    # -- dependent DLL resolution -------------------------------------------
    def _dll_candidates(self):
        if getattr(self, "_dllcache", None) is not None:
            return self._dllcache
        idx = {}

        def add(path):
            n = os.path.basename(path).lower()
            if n.endswith(".dll") and n not in idx:
                idx[n] = path

        for f in glob.glob(os.path.join(self.base_prefix, "*.dll")):
            add(f)
        for f in glob.glob(os.path.join(self.base_prefix, "DLLs", "*.dll")):
            add(f)
        for root in self.site_roots:
            if not os.path.isdir(root):
                continue
            for r, _d, files in os.walk(root):
                for fn in files:
                    if fn.lower().endswith(".dll"):
                        add(os.path.join(r, fn))
        self._dllcache = idx
        return idx

    def _resolve_dlls(self, runtime_dir):
        import collections as _c
        present = {PYDLL.lower()}
        pe_files = []
        for root, _d, files in os.walk(runtime_dir):
            for fn in files:
                low = fn.lower()
                if low.endswith((".dll", ".pyd")):
                    present.add(low)
                    pe_files.append(os.path.join(root, fn))
        candidates = self._dll_candidates()
        queue = _c.deque(pe_files)
        processed = set()
        while queue:
            t = queue.popleft()
            if t in processed:
                continue
            processed.add(t)
            for dep in imported_dlls(t):
                d = dep.lower()
                if d in present:
                    continue
                src = candidates.get(d)
                if not src:
                    continue   # not in the Python tree -> a system DLL, skip
                dst = os.path.join(runtime_dir, os.path.basename(src))
                if not os.path.exists(dst):
                    shutil.copy2(src, dst)
                    self.payload_rows.append((os.path.basename(src) + " (dependency)",
                                              os.path.getsize(dst)))
                present.add(d)
                queue.append(dst)

    def _copy_native(self, src: str, dest_dir: str, category: str, basename=None):
        os.makedirs(dest_dir, exist_ok=True)
        dst = os.path.join(dest_dir, basename or os.path.basename(src))
        shutil.copy2(src, dst)
        self.payload_rows.append((os.path.basename(dst), os.path.getsize(dst)))
        return dst

    # -- stub compilation ---------------------------------------------------
    def _write_stub_config(self, build_dir, script_name, build_hash):
        cfg_h = os.path.join(build_dir, "stub_config.h")
        with open(cfg_h, "w", encoding="ascii") as f:
            f.write(f'#define SLIMPACK_PYDLL L"{PYDLL}"\n')
            f.write(f'#define SLIMPACK_SCRIPT L"{script_name}"\n')
            f.write(f'#define SLIMPACK_HASH "{build_hash}"\n')
        return cfg_h

    def compile_stub(self, build_dir, out_exe, onefile, build_hash):
        gcc = which("gcc")
        if not gcc:
            raise RuntimeError("gcc (MinGW) not found on PATH; cannot build the launcher")
        self._write_stub_config(build_dir, "__main__.pyc", build_hash)
        stub_c = os.path.join(NATIVE_DIR, "stub.c")
        lzma_dir = os.path.join(NATIVE_DIR, "lzma")
        cmd = [gcc, "-Os", "-s", "-municode", "-DNDEBUG",
               "-ffunction-sections", "-fdata-sections", "-Wl,--gc-sections",
               f"-I{build_dir}", stub_c]
        if onefile:
            cmd += ["-DSLIMPACK_ONEFILE", f"-I{lzma_dir}", os.path.join(lzma_dir, "LzmaDec.c")]
        cmd += ["-o", out_exe, "-lshell32", "-luser32"]
        self.log("gcc:", " ".join(cmd))
        r = subprocess.run(cmd, capture_output=True, text=True)
        if r.returncode != 0:
            raise RuntimeError("stub compile failed:\n" + r.stdout + r.stderr)

    # -- onefile overlay ----------------------------------------------------
    def make_archive(self, runtime_dir: str) -> bytes:
        files = []
        for root, _dirs, names in os.walk(runtime_dir):
            for n in names:
                if n == ".slimpack_ok":
                    continue
                full = os.path.join(root, n)
                rel = os.path.relpath(full, runtime_dir).replace(os.sep, "/")
                with open(full, "rb") as f:
                    files.append((rel, f.read()))
        out = bytearray()
        out += struct.pack("<I", len(files))
        for rel, data in files:
            name_b = rel.encode("utf-8")
            out += struct.pack("<H", len(name_b)) + name_b
            out += struct.pack("<I", len(data)) + data
        return bytes(out)

    def lzma_compress(self, raw: bytes):
        import lzma
        dict_size = 1 << 16
        while dict_size < len(raw) and dict_size < (1 << 27):
            dict_size <<= 1
        lc, lp, pb = 3, 0, 2
        filt = [{"id": lzma.FILTER_LZMA1, "dict_size": dict_size,
                 "lc": lc, "lp": lp, "pb": pb, "mode": lzma.MODE_NORMAL,
                 "nice_len": 273, "mf": lzma.MF_BT4, "depth": 0}]
        comp = lzma.compress(raw, format=lzma.FORMAT_RAW, filters=filt)
        props = bytes([(pb * 5 + lp) * 9 + lc]) + struct.pack("<I", dict_size)
        return comp, props

    def append_overlay(self, stub_exe: str, out_exe: str, archive: bytes):
        comp, props = self.lzma_compress(archive)
        with open(stub_exe, "rb") as f:
            stub = f.read()
        trailer = props + struct.pack("<Q", len(archive)) + struct.pack("<Q", len(comp)) + b"SLIMPK01"
        with open(out_exe, "wb") as f:
            f.write(stub)
            f.write(comp)
            f.write(trailer)
        return len(archive), len(comp)

    # -- UPX ----------------------------------------------------------------
    _upx = ...  # resolved lazily

    def upx(self, path: str):
        if self.cfg.no_upx:
            return
        if self._upx is ...:
            self._upx = find_upx()
            if not self._upx:
                self.log("upx not found; skipping native compression")
        if not self._upx:
            return
        before = os.path.getsize(path)
        r = subprocess.run([self._upx, "--best", "--lzma", "-q", path],
                           capture_output=True, text=True)
        if r.returncode != 0:
            self.log(f"upx skipped {os.path.basename(path)} (not compressible)")
        else:
            self.log(f"upx {os.path.basename(path)}: {human(before)} -> "
                     f"{human(os.path.getsize(path))}")

    # -- orchestrate --------------------------------------------------------
    def run(self):
        self.analyze()
        self.partition()
        # Names whose parent module resolved are `from pkg import name` where
        # `name` is an attribute, not a submodule - not actually missing.
        real_missing = sorted(n for n in self.missing
                              if n.rpartition(".")[0] not in self.modules)
        if real_missing:
            print(f"[slimpack] note: {len(real_missing)} module name(s) could not be "
                  f"resolved (likely conditional/optional). Use --include if required.")
            if self.cfg.verbose:
                for n in real_missing:
                    print("    unresolved:", n)

        build_dir = os.path.join(self.cfg.scratch, f"_slimpack_build_{self.name}")
        runtime_dir = os.path.join(build_dir, "runtime")
        if os.path.exists(build_dir):
            shutil.rmtree(build_dir, ignore_errors=True)
        os.makedirs(runtime_dir, exist_ok=True)

        self.assemble_runtime(runtime_dir, stored_zip=self.cfg.onefile)

        import hashlib
        build_hash = hashlib.md5(self.entry.encode("utf-8", "replace")).hexdigest()[:12]

        if self.cfg.onefile:
            out_exe = self.cfg.output or os.path.join("dist", self.name + ".exe")
            os.makedirs(os.path.dirname(os.path.abspath(out_exe)) or ".", exist_ok=True)
            stub_exe = os.path.join(build_dir, "stub.exe")
            self.compile_stub(build_dir, stub_exe, onefile=True, build_hash=build_hash)
            self.upx(stub_exe)
            archive = self.make_archive(runtime_dir)
            raw_n, comp_n = self.append_overlay(stub_exe, out_exe, archive)
            self.stub_size = os.path.getsize(stub_exe)
            self.raw_payload = raw_n
            self.overlay_size = comp_n
            final = out_exe
        else:
            out_dir = self.cfg.output or os.path.join("dist", self.name)
            os.makedirs(out_dir, exist_ok=True)
            # copy assembled runtime into the output dir
            for item in os.listdir(runtime_dir):
                s = os.path.join(runtime_dir, item)
                d = os.path.join(out_dir, item)
                if os.path.isdir(s):
                    shutil.copytree(s, d, dirs_exist_ok=True)
                else:
                    shutil.copy2(s, d)
            out_exe = os.path.join(out_dir, self.name + ".exe")
            self.compile_stub(build_dir, out_exe, onefile=False, build_hash=build_hash)
            # UPX everything safe in the folder
            self.upx(out_exe)
            for item in os.listdir(out_dir):
                if item.lower().endswith((".dll", ".pyd")):
                    self.upx(os.path.join(out_dir, item))
            final = out_dir

        if not self.cfg.keep_build:
            shutil.rmtree(build_dir, ignore_errors=True)
        return final
