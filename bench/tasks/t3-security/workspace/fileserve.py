# BENCHMARK FIXTURE — deliberately insecure; never use in production.
"""Serve files from a fixed root directory to untrusted callers."""

import os


def read_served_file(root, user_path):
    """Return the bytes of the file the caller requested under `root`.

    `root` is a trusted absolute directory. `user_path` is an untrusted
    relative path supplied by the caller. See TASK.md for the security
    contract this must satisfy.
    """
    full = os.path.join(root, user_path)
    with open(full, "rb") as handle:
        return handle.read()
