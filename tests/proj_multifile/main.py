"""Multi-file test project: main.py imports a local funcs.py plus stdlib."""

import json
import sys

import funcs


def main():
    data = {
        "greeting": funcs.greet("slimpack"),
        "area_r2": round(funcs.circle_area(2), 4),
        "argv": sys.argv[1:],
    }
    print(json.dumps(data, indent=2))


if __name__ == "__main__":
    main()
