"""Repository layout anchors for the grokbuild package."""

from __future__ import annotations
from pathlib import Path


def repo_root() -> Path:
    """Return the checkout root (the parent of this package directory)."""
    return Path(__file__).resolve().parents[1]


def config_path() -> Path:
    """Return the repository config shipped with the checkout."""
    return repo_root() / "config" / "config.toml"
