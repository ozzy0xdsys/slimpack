"""A module that is only ever imported dynamically (name built at runtime),
so static AST analysis cannot see it - only --trace (or --include) catches it."""

import base64


def run():
    msg = base64.b64decode(b"ZHluYW1pYyBpbXBvcnQgd29ya3Mh").decode()
    return msg
