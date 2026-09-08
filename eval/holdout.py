"""Fail-closed validation of controller-owned hidden holdout boundaries."""

from __future__ import annotations

from dataclasses import dataclass
import hashlib
from pathlib import Path
from typing import Mapping, Sequence


@dataclass(frozen=True)
class HoldoutBoundary:
    verified: bool
    reason: str
    manifest_digest: str = ""


def file_digest(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as handle:
        for chunk in iter(lambda: handle.read(65536), b""):
            digest.update(chunk)
    return digest.hexdigest()


def validate_holdout_boundary(
    *,
    holdout_root: Path | str,
    workspace_root: Path | str,
    manifest: Mapping[str, str],
    network: str,
    grader_read_only: bool,
    actor_views: Mapping[str, Sequence[str]],
) -> HoldoutBoundary:
    """Verify realpaths, immutable digests, network isolation, and disjoint views."""
    try:
        root = Path(holdout_root).resolve(strict=True)
        workspace = Path(workspace_root).resolve(strict=True)
    except OSError:
        return HoldoutBoundary(False, "holdout boundary path is unavailable")
    if root == workspace or workspace in root.parents:
        return HoldoutBoundary(False, "holdout is inside the workspace")
    if network != "off":
        return HoldoutBoundary(False, "holdout grading network is not off")
    if not grader_read_only:
        return HoldoutBoundary(False, "grader mount is not read-only")
    hidden_roles = {"candidate", "proposer", "applier", "judge"}
    if any(actor_views.get(role) for role in hidden_roles):
        return HoldoutBoundary(False, "actor capability view exposes holdout paths")
    expected_files = manifest.get("files")
    expected_digest = manifest.get("digest")
    if not isinstance(expected_files, str) or not expected_files or not expected_digest:
        return HoldoutBoundary(False, "holdout manifest is incomplete")
    try:
        manifest_path = (root / expected_files).resolve(strict=True)
    except OSError:
        return HoldoutBoundary(False, "holdout manifest path is unavailable")
    if root not in manifest_path.parents:
        return HoldoutBoundary(False, "holdout manifest escapes its root")
    actual = file_digest(manifest_path)
    if actual != expected_digest:
        return HoldoutBoundary(False, "holdout digest mismatch")
    return HoldoutBoundary(True, "boundary verified", actual)


__all__ = ["HoldoutBoundary", "file_digest", "validate_holdout_boundary"]
