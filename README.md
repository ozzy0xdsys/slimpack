# slimpack

Pack a Python script into the **smallest possible** Windows `.exe`.

slimpack is a from-scratch Python-to-EXE converter whose first priority is
minimum output size, while still running real programs — including third-party
PyPI packages with C extensions, data files and metadata — unchanged.

| Project (same script, both built **and run**) | PyInstaller `--onefile` | slimpack `--onefile` | |
|---|---|---|---|
| multi-file (local module + stdlib `json`/`math`) | 9.46 MB | **2.25 MB** | **4.2× smaller** |
| `requests` (urllib3 + certifi data + idna + charset C-ext + ssl) | 16.12 MB | **6.28 MB** | **2.6× smaller** |

All figures measured on this machine; both tools' executables were run to
confirm identical behavior.

---

## Requirements

| Tool | Why | On the test machine |
|------|-----|---------------------|
| CPython 3.x (Windows) | the interpreter that gets bundled | 3.14.7 |
| MinGW-w64 `gcc` | compiles the ~20 KB C launcher stub | 16.2.0 (UCRT) |
| `upx` (optional) | compresses `python3XX.dll` and native modules | 5.2.1 |
| `packaging` (optional) | resolves third-party `Requires-Dist` deps | present |

slimpack bundles the **same** CPython that runs it (matching bytecode magic and
ABI), so build with the interpreter whose programs you want to ship. `gcc` must
be on `PATH`. `upx` is located automatically even when not on `PATH`
(`SLIMPACK_UPX` env var → winget / choco / scoop); without it the `.exe` just
ends up larger. Without `packaging`, declared-dependency resolution is skipped
(use `--include` / `--trace` instead).

---

## Install / run

```bash
pip install .          # then:  slimpack myscript.py
# or without installing:
python -m slimpack myscript.py
```

```bash
slimpack main.py                      # -> dist\main\   (onedir folder)
slimpack main.py --onefile            # -> dist\main.exe (single file)
slimpack app.py --onefile --report    # build + size breakdown + PyInstaller compare
```

### Flags

| Flag | Meaning |
|------|---------|
| `--onefile` | one self-extracting `.exe` instead of a folder |
| `--output, -o PATH` | output `.exe` (onefile) or folder (onedir) |
| `--trace` | also **run** the script under an import tracer and bundle every module imported at runtime (catches dynamic imports) |
| `--include MOD` | force-bundle a module/package (repeatable) |
| `--exclude MOD` | drop a top-level package/module from the bundle (repeatable) |
| `--path DIR` | extra source root for local modules (repeatable) |
| `--report` | print the size breakdown and compare against PyInstaller |
| `--no-upx` | skip UPX |
| `--keep-build` | keep the intermediate build directory |
| `-v, --verbose` | show build steps and the report |

---

## How it works

slimpack never bundles "a Python install". It figures out exactly what a script
needs, keeps only that, and compresses what remains.

### 1. Resolve the dependency graph (`analyzer.py`)

* **Static pass** walks the AST of the entry script and, transitively, of every
  reachable module. It follows `import`, `from x import y` (absolute **and**
  relative), and literal `__import__("x")` / `importlib.import_module("x")`.
* **Which imports count.** Module-level imports (and those in `if`/`try`/`with`/
  `for`/class bodies, which run on import) are always followed. Imports **inside
  function bodies** are lazy; slimpack follows them only when they stay **within
  the module's own top-level package** (e.g. `importlib.resources._common`
  lazily importing `importlib.readers`). Cross-package lazy imports are skipped,
  which is what stops a trivial program from dragging in half the stdlib
  (`tokenize` does `import argparse` in a function; `pdb` imports `asyncio`).
  One example dropped from 480 modules to 46 this way.
* **Optional backends are skipped.** Imports guarded by `try/except ImportError`
  are treated as optional and not bundled — this is why packing `requests` does
  **not** pull in `cryptography`/pyOpenSSL (an optional urllib3 backend).
* **Trace pass** (`--trace`) runs the program under a `sys.meta_path` recorder
  and merges in everything imported at runtime — the catch-all for dynamic
  imports whose names are built at runtime.
* **`--include` / `--exclude`** force a module in or drop one out.

Modules **frozen or built into `python3XX.dll`** (`os`, `io`, `importlib`,
`runpy`, `zipimport`, `_io`, …) are detected and never shipped — they already
ride inside the DLL for free.

### 2. Third-party PyPI packages — collected to work unchanged

When the graph reaches a package in `site-packages`, slimpack collects it
**whole** so nothing it needs at runtime is missed:

* every module compiled to bytecode, **plus all data files** (e.g. certifi's
  `cacert.pem`), `.pyd` extensions, and package-local / sibling `*.libs` DLLs;
* its **`.dist-info` metadata**, so `importlib.metadata.version(...)` works;
* its **declared dependencies** from `Requires-Dist` (markers evaluated, extras
  excluded) are pulled in too — this is how `requests`'s `idna`, imported via a
  dynamic `__import__(name)` loop, still gets bundled, and how `cryptography`
  pulls `cffi` / `_cffi_backend`.

Stdlib and local modules are still aggressively tree-shaken to bytecode in a
zip; only `site-packages` packages are taken whole.

### 3. Assemble a minimal runtime

`python3XX.dll` + `vcruntime140*.dll`, only the `.pyd` extensions actually used,
and — via a PE import-table scanner (`pescan.py`) — any **non-system** DLLs those
extensions need (`_ssl` → `libssl`/`libcrypto`, `_sqlite3` → `sqlite3.dll`, …).
System DLLs are filtered out automatically because they don't live in the Python
tree. A `python3XX._pth` file pins `sys.path` to `[python_modules.zip, .]` —
isolated, no `site`.

### 4. Launch (`native/stub.c`)

A ~20 KB C program loads `python3XX.dll` dynamically and calls `Py_Main()` on a
compiled `__main__.pyc`. Path config comes from the `._pth` file (the mechanism
CPython's own *embeddable* distribution uses), so no version-fragile `PyConfig`
struct is baked into the binary. Arguments are forwarded, so `sys.argv` is
normal.

### 5. Compress

* **onedir**: the bytecode zip is DEFLATE; `python3XX.dll` and native modules go
  through `upx --best --lzma` (`python314.dll`: 6.47 MB → 1.93 MB); the stub is
  built `-Os -s`.
* **onefile**: the whole runtime is packed into one archive, compressed as a
  single **LZMA** stream, and appended to the stub as an overlay. On first launch
  the stub decompresses it once into `%TEMP%\slimpack_<hash>\` (with a vendored,
  public-domain LZMA decoder from the 7-Zip SDK) and caches it, so later launches
  are instant.

---

## Test projects

Under `tests/`, all built and run end-to-end:

* `proj_multifile/` — local `funcs.py` + stdlib. Local-file bundling + stdlib
  tree-shaking.
* `proj_dynamic/` — module name built at runtime and imported via
  `importlib.import_module`. Without `--trace` the `.exe` correctly raises
  `ModuleNotFoundError`; with `--trace` (or `--include plugins`) it prints
  `dynamic import works!`.
* `proj_pypi/req_test.py` — `requests`: multi-package collection, certifi's
  `cacert.pem` **data file**, `importlib.metadata` version lookups, the
  `charset_normalizer` C extension, `idna` via metadata, and ssl DLLs. Prints
  real version numbers and a prepared URL.
* `proj_pypi/crypto_test.py` — `cryptography`: a large Rust `_rust.pyd` plus
  `cffi` / `_cffi_backend`; computes a SHA-256 and a Fernet round-trip.

---

## Verified / not verified

**Verified** (Windows 11, Python 3.14.7, gcc 16.2.0 UCRT, UPX 5.2.1):

* onedir **and** onefile builds of all four test projects compile to `.exe` and
  run with correct output, exit code 0 — including onefile cold extraction and
  cached warm restart.
* real PyPI packages: `requests` (+ `urllib3`, `certifi`, `idna`,
  `charset_normalizer`) and `cryptography` (+ `cffi`), covering data files,
  metadata, dynamically-imported deps, ssl DLLs and a compiled extension.
* size wins vs PyInstaller: 4.2× (multi-file), 2.6× (`requests`).

**Not verified here:**

* **Nuitka** was not installed — no direct Nuitka comparison was run.
* Packages that place C extensions' DLLs in a sibling `name.libs/` directory
  (e.g. **numpy**, Pillow). The code copies `name.libs` and flattens dependent
  DLLs into the runtime, but this path wasn't exercised against such a package on
  this machine (the `cryptography` wheel statically links its native code).
* 32-bit Python and Python versions other than the 3.14 used here.
* Antivirus behavior — UPX-compressed binaries are sometimes flagged by AV
  heuristics; use `--no-upx` if that is a concern.

For genuinely dynamic programs (plugins discovered at runtime, imports built
from external input), `--trace` or `--include` remains the way to guarantee a
complete bundle.
