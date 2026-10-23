"""Entry point for the standalone slimpack.exe.

slimpack is a build tool: to pack a script it must read modules and DLLs from a
real Python installation and compile a C stub. A frozen copy of slimpack only
carries its own minimal runtime, so instead of trying to pack *from* that, this
launcher re-dispatches to a Python interpreter on the user's machine and runs
the real slimpack (whose source is bundled alongside this exe) under it. That
keeps everything version-correct: the produced .exe bundles whichever CPython
actually did the build.

Requirements on the user's machine: a Python 3 interpreter, plus MinGW `gcc`
(and optionally `upx`) on PATH, exactly as when running slimpack from source.

Usage is identical to slimpack, with one extra option:
    slimpack.exe [--python PATH] <script.py> [slimpack options...]
`--python` (or the SLIMPACK_PYTHON env var) selects which interpreter to use;
otherwise the `py` launcher / `python` / `python3` on PATH is used.
"""

import os
import shutil
import subprocess
import sys


def _source_root():
    """Directory to put on PYTHONPATH so `-m slimpack` resolves to the bundled
    source. Frozen: <runtime>/src/slimpack (via --add-data). Dev: the dir that
    contains this file (the repo root, where slimpack/ lives)."""
    here = os.path.dirname(os.path.abspath(__file__))
    frozen = os.path.join(here, "src")
    if os.path.isdir(os.path.join(frozen, "slimpack")):
        return frozen
    if os.path.isdir(os.path.join(here, "slimpack")):
        return here
    return frozen


def _works(cmd):
    try:
        r = subprocess.run(cmd + ["-c", "import sys; assert sys.version_info[0] == 3"],
                           capture_output=True)
        return r.returncode == 0
    except OSError:
        return False


def _find_python(explicit):
    cands = []
    if explicit:
        cands.append([explicit])
    env = os.environ.get("SLIMPACK_PYTHON")
    if env:
        cands.append([env])
    py = shutil.which("py")
    if py:
        cands.append([py, "-3"])
    for name in ("python", "python3"):
        p = shutil.which(name)
        if p and os.path.basename(p).lower() != "slimpack.exe":
            cands.append([p])
    for cmd in cands:
        if _works(cmd):
            return cmd
    return None


def main():
    argv = sys.argv[1:]

    # pull out --python / --python=PATH (consumed here, not passed to slimpack)
    explicit = None
    rest = []
    i = 0
    while i < len(argv):
        a = argv[i]
        if a == "--python":
            explicit = argv[i + 1] if i + 1 < len(argv) else None
            i += 2
            continue
        if a.startswith("--python="):
            explicit = a.split("=", 1)[1]
            i += 1
            continue
        rest.append(a)
        i += 1

    py = _find_python(explicit)
    if not py:
        sys.stderr.write(
            "slimpack: no Python 3 interpreter found.\n"
            "slimpack needs a Python install to pack from. Install Python 3 and\n"
            "make sure `py` or `python` is on PATH, or pass --python <path>.\n")
        return 2

    env = dict(os.environ)
    src = _source_root()
    env["PYTHONPATH"] = src + os.pathsep + env.get("PYTHONPATH", "")
    try:
        return subprocess.call(py + ["-m", "slimpack"] + rest, env=env)
    except OSError as e:
        sys.stderr.write(f"slimpack: failed to run {py!r}: {e}\n")
        return 2


if __name__ == "__main__":
    sys.exit(main())
