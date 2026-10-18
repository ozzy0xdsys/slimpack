"""Compiled-extension test: cryptography ships a large Rust extension
(_rust.pyd). Exercises collecting a package whose code is mostly native."""

from cryptography.hazmat.primitives import hashes
from cryptography.fernet import Fernet


def main():
    digest = hashes.Hash(hashes.SHA256())
    digest.update(b"hello slimpack")
    print("sha256 :", digest.finalize().hex())

    key = Fernet.generate_key()
    f = Fernet(key)
    token = f.encrypt(b"secret message")
    print("fernet :", f.decrypt(token).decode())
    print("OK")


if __name__ == "__main__":
    main()
