# BENCHMARK FIXTURE — deliberately insecure; never use in production.
"""Upload handling for untrusted clients. See TASK.md."""

import os
import urllib.parse
import zipfile


def save_upload(root, name, data):
    decoded = urllib.parse.unquote(urllib.parse.unquote(name))
    target = os.path.join(root, "uploads", decoded)
    os.makedirs(os.path.dirname(target), exist_ok=True)
    with open(target, "wb") as handle:
        handle.write(data)
    return target


def fetch(root, name):
    decoded = urllib.parse.unquote(urllib.parse.unquote(name))
    with open(os.path.join(root, "uploads", decoded), "rb") as handle:
        return handle.read()


def extract_bundle(root, zip_path):
    dest = os.path.join(root, "uploads", "bundle")
    os.makedirs(dest, exist_ok=True)
    with zipfile.ZipFile(zip_path) as bundle:
        bundle.extractall(dest)
    return dest


def log_access(root, client_id, name):
    with open(os.path.join(root, "access.log"), "a") as handle:
        handle.write(f"{client_id} accessed {name}\n")


def check_token(stored, presented):
    return stored == presented
