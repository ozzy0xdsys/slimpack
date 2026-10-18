"""Dynamic-import test project.

The module name is assembled at runtime from pieces, so a static AST pass
cannot possibly see it. slimpack must either --trace the program or be told
--include plugins. We also exercise importlib.import_module."""

import importlib


def load(part_a, part_b):
    name = part_a + part_b          # -> "plugins", invisible to static analysis
    mod = importlib.import_module(name)
    return mod


def main():
    mod = load("plug", "ins")
    print(mod.run())


if __name__ == "__main__":
    main()
