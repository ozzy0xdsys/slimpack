"""slimpack command-line interface."""

from __future__ import annotations

import argparse
import os
import sys
import time

from .builder import Builder, BuildConfig, human, which


def _folder_size(path: str) -> int:
    if os.path.isfile(path):
        return os.path.getsize(path)
    total = 0
    for root, _d, files in os.walk(path):
        for f in files:
            total += os.path.getsize(os.path.join(root, f))
    return total


def _print_report(builder: Builder, final: str, elapsed: float):
    print()
    print("=" * 62)
    print(f"  slimpack build report: {builder.name}")
    print("=" * 62)
    kept = len(builder.pure) + len(builder.exts)
    print(f"  modules bundled : {kept}  "
          f"(pure {len(builder.pure)}, native ext {len(builder.exts)})")
    print(f"  modules in DLL  : {len(builder.skipped)} frozen/builtin (shipped free)")
    print(f"  build time      : {elapsed:.1f}s")

    if os.path.isfile(final):   # onefile
        print("-" * 62)
        print("  payload contributors (raw, before LZMA):")
        rows = sorted(builder.payload_rows, key=lambda r: -r[1])
        for label, size in rows[:12]:
            print(f"  {human(size):>12}   {label}")
        print("-" * 62)
        print(f"  {human(builder.raw_payload):>12}   payload raw total")
        print(f"  {human(builder.overlay_size):>12}   -> LZMA overlay (compressed)")
        print(f"  {human(builder.stub_size):>12}   launcher stub")
    else:                        # onedir
        print("-" * 62)
        print("  files in output folder (on disk, after UPX):")
        files = []
        for root, _d, names in os.walk(final):
            for n in names:
                p = os.path.join(root, n)
                files.append((os.path.relpath(p, final), os.path.getsize(p)))
        for label, size in sorted(files, key=lambda r: -r[1])[:12]:
            print(f"  {human(size):>12}   {label}")

    print("-" * 62)
    total = _folder_size(final)
    kind = "onefile exe" if os.path.isfile(final) else "onedir folder"
    print(f"  {human(total):>12}   TOTAL ({kind})")
    print("=" * 62)
    print(f"  output: {final}")
    print()


def _maybe_compare(entry: str, slim_size: int):
    """If PyInstaller is available, build the same entry onefile and compare."""
    pyi = which("pyinstaller")
    if not pyi:
        print("  (PyInstaller not found - skipping comparison)")
        return
    print("  Building the same script with PyInstaller for comparison...")
    import subprocess, tempfile, shutil
    work = tempfile.mkdtemp(prefix="pyi_cmp_")
    try:
        r = subprocess.run(
            [pyi, "--onefile", "--distpath", os.path.join(work, "dist"),
             "--workpath", os.path.join(work, "build"),
             "--specpath", work, "--noconfirm", "--clean", entry],
            capture_output=True, text=True)
        exe = os.path.join(work, "dist",
                           os.path.splitext(os.path.basename(entry))[0] + ".exe")
        if r.returncode == 0 and os.path.exists(exe):
            pyi_size = os.path.getsize(exe)
            print(f"  PyInstaller onefile : {human(pyi_size)}")
            print(f"  slimpack  onefile   : {human(slim_size)}")
            if slim_size:
                print(f"  --> slimpack is {pyi_size/slim_size:.1f}x smaller "
                      f"({human(pyi_size - slim_size)} saved)")
        else:
            print("  (PyInstaller build failed; see --verbose)")
            if r.returncode != 0:
                sys.stderr.write(r.stdout[-2000:] + r.stderr[-2000:])
    finally:
        shutil.rmtree(work, ignore_errors=True)


def main(argv=None):
    p = argparse.ArgumentParser(
        prog="slimpack",
        description="Pack a Python script into the smallest possible Windows .exe.")
    p.add_argument("script", help="entry-point .py file")
    p.add_argument("--onefile", action="store_true",
                   help="produce a single self-extracting .exe (default: a folder)")
    p.add_argument("--output", "-o", help="output .exe (onefile) or folder (onedir)")
    p.add_argument("--trace", action="store_true",
                   help="also run the script under an import tracer to catch "
                        "dynamic imports (executes the program)")
    p.add_argument("--include", action="append", default=[], metavar="MOD",
                   help="force-include a module (repeatable)")
    p.add_argument("--path", action="append", default=[], metavar="DIR",
                   help="extra source root for local modules (repeatable)")
    p.add_argument("--exclude", action="append", default=[], metavar="MOD",
                   help="drop a top-level package/module from the bundle (repeatable)")
    p.add_argument("--add-data", action="append", default=[], metavar="SRC[;DEST]",
                   help="bundle an extra file/dir into the runtime; DEST defaults "
                        "to the basename (repeatable)")
    p.add_argument("--report", action="store_true",
                   help="print the size breakdown and compare against PyInstaller")
    p.add_argument("--no-upx", action="store_true", help="do not run UPX")
    p.add_argument("--keep-build", action="store_true",
                   help="keep the intermediate build directory")
    p.add_argument("-v", "--verbose", action="store_true")
    args = p.parse_args(argv)

    if not os.path.isfile(args.script):
        p.error(f"no such file: {args.script}")

    scratch = os.environ.get("SLIMPACK_SCRATCH", os.path.abspath("build"))
    os.makedirs(scratch, exist_ok=True)

    cfg = BuildConfig(
        entry=args.script, output=args.output, onefile=args.onefile,
        trace=args.trace, includes=args.include, paths=args.path,
        exclude=args.exclude, add_data=getattr(args, "add_data", []),
        report=args.report, verbose=args.verbose,
        keep_build=args.keep_build, no_upx=args.no_upx, scratch=scratch,
    )
    b = Builder(cfg)
    t0 = time.time()
    final = b.run()
    elapsed = time.time() - t0

    if args.report or args.verbose:
        _print_report(b, final, elapsed)
    else:
        print(f"[slimpack] built {final}  ({human(_folder_size(final))})")

    if args.report:
        slim_size = _folder_size(final)
        _maybe_compare(os.path.abspath(args.script), slim_size)

    return 0


if __name__ == "__main__":
    sys.exit(main())
