import hashlib


def file_md5(path: str) -> bytes:
    """Compute MD5 hash of a file."""
    h = hashlib.md5()
    with open(path, "rb") as f:
        for chunk in iter(lambda: f.read(65536), b""):
            h.update(chunk)
    return h.digest()


def bytes_md5(data: bytes) -> bytes:
    """Compute MD5 hash of raw bytes."""
    return hashlib.md5(data).digest()
