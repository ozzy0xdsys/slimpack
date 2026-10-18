"""Third-party test: requests (+ urllib3, certifi, charset_normalizer, idna).

Exercises: multi-package third-party collection, a package DATA file
(certifi's cacert.pem), package METADATA (importlib.metadata.version), and an
optional C extension (charset_normalizer)."""

import os
from importlib.metadata import version

import requests
import charset_normalizer


def main():
    print("requests           :", version("requests"))
    print("urllib3            :", version("urllib3"))
    print("charset_normalizer :", charset_normalizer.__version__)

    ca = requests.certs.where()
    print("cacert.pem path    :", os.path.basename(ca))
    print("cacert.pem exists  :", os.path.exists(ca), os.path.getsize(ca), "bytes")

    # build a PreparedRequest without network I/O -> exercises urllib3 + idna
    r = requests.Request("GET", "https://example.com/path?q=1").prepare()
    print("prepared url       :", r.url)
    print("OK")


if __name__ == "__main__":
    main()
