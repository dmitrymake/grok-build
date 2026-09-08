"""Leaf persistence primitives for Grok Build state and logs."""

from __future__ import annotations

import contextlib
import fcntl
import json
import os
import stat
import tempfile
import time
from pathlib import Path
from typing import Any, Mapping
from datetime import UTC, datetime

from grokbuild.redact import redact


def parse_iso_utc(value: str) -> datetime:
    """Parse an ISO-8601 timestamp and normalize it to UTC."""
    parsed = datetime.fromisoformat(value.strip().replace("Z", "+00:00"))
    if parsed.tzinfo is None:
        parsed = parsed.replace(tzinfo=UTC)
    return parsed.astimezone(UTC)


HISTORY_LIMIT = 200
HISTORY_ROTATE_LINES = 400
TEMP_GC_SCAN_LIMIT = 100
TEMP_GC_DELETE_LIMIT = 50
STALE_AFTER = 24 * 60 * 60.0
MAX_LOG_BYTES = 2 * 1024 * 1024
MAX_LOG_FILES = 3


def state_dir() -> Path:
    return Path(os.environ.get("XDG_STATE_HOME", Path.home() / ".local" / "state")) / "grok-route"


def sidecar_path(name: str) -> Path:
    return state_dir() / name


def _decisions_path(state_path: Path) -> Path:
    return state_path.parent / "decisions.jsonl"


def _migration_marker(state_path: Path) -> Path:
    return state_path.parent / ".decisions-migrated"


def sidecar_generations(path: Path) -> tuple[Path, ...]:
    """Return current and rotated sidecars without imposing read order."""
    return (path, Path(f"{path}.1"), Path(f"{path}.2"))


def iter_jsonl(
    path: Path,
    *,
    skip_malformed: bool = True,
    require_mapping: bool = True,
    whole_file_catch: bool = True,
):
    """Yield parsed JSONL records; callers retain site-specific policies."""
    try:
        handle = path.open("r", encoding="utf-8")
    except OSError:
        if whole_file_catch:
            return
        raise
    with handle:
        for lineno, line in enumerate(handle, 1):
            if not line.strip():
                continue
            try:
                item = json.loads(line)
            except (json.JSONDecodeError, UnicodeDecodeError):
                if skip_malformed:
                    continue
                raise
            if require_mapping and not isinstance(item, Mapping):
                if skip_malformed:
                    continue
                raise ValueError(f"JSONL line {lineno} is not a mapping")
            yield lineno, item


def _read_decision_history(path: Path) -> list[dict[str, Any]]:
    records = [item for _, item in iter_jsonl(path)]
    return records[-HISTORY_LIMIT:]


def _rewrite_jsonl(path: Path, records: list[dict[str, Any]]) -> None:
    fd, tmp_name = tempfile.mkstemp(
        dir=str(path.parent), prefix="." + path.name + ".", suffix=".tmp"
    )
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as handle:
            for record in records:
                handle.write(json.dumps(redact(record), ensure_ascii=False) + "\n")
            handle.flush()
            os.fsync(handle.fileno())
        os.chmod(tmp_name, 0o600)
        os.replace(tmp_name, path)
    finally:
        try:
            os.unlink(tmp_name)
        except OSError:
            pass


def _append_decisions_locked(path: Path, records: list[dict[str, Any]]) -> None:
    if not records:
        return
    path.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
    with path.open("a", encoding="utf-8") as handle:
        for record in records:
            handle.write(json.dumps(redact(record), ensure_ascii=False) + "\n")
        handle.flush()
        os.fsync(handle.fileno())
    try:
        os.chmod(path, 0o600)
    except OSError:
        pass
    all_records = _read_decision_history_unbounded(path)
    if len(all_records) > HISTORY_ROTATE_LINES:
        _rewrite_jsonl(path, all_records[-HISTORY_LIMIT:])


def _read_decision_history_unbounded(
    path: Path, stop_after: int | None = None
) -> list[dict[str, Any]]:
    records: list[dict[str, Any]] = []
    for _, item in iter_jsonl(path):
        records.append(item)
        if stop_after is not None and len(records) >= stop_after:
            break
    return records


def _write_marker(path: Path) -> None:
    fd, tmp_name = tempfile.mkstemp(
        dir=str(path.parent), prefix="." + path.name + ".", suffix=".tmp"
    )
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as handle:
            handle.write("1\n")
            handle.flush()
            os.fsync(handle.fileno())
        os.chmod(tmp_name, 0o600)
        os.replace(tmp_name, path)
        directory_fd = os.open(path.parent, os.O_RDONLY)
        try:
            os.fsync(directory_fd)
        finally:
            os.close(directory_fd)
    finally:
        try:
            os.unlink(tmp_name)
        except OSError:
            pass


def _gc_state_temps(path: Path, now: float | None = None) -> None:
    cutoff = (time.time() if now is None else now) - STALE_AFTER
    prefix = f".{path.name}."
    scanned = deleted = 0
    try:
        entries = os.scandir(path.parent)
    except OSError:
        return
    with entries:
        for entry in entries:
            if scanned >= TEMP_GC_SCAN_LIMIT or deleted >= TEMP_GC_DELETE_LIMIT:
                break
            scanned += 1
            name = entry.name
            if not (name.startswith(prefix) and name.endswith(".tmp")):
                continue
            try:
                metadata = os.lstat(entry.path)
                if not stat.S_ISREG(metadata.st_mode) or metadata.st_mtime >= cutoff:
                    continue
                os.unlink(entry.path)
                deleted += 1
            except OSError:
                continue


def atomic_update_json(
    path: Path,
    updater,
    default: Any = None,
    *,
    append_decision: Mapping[str, Any] | None = None,
    on_load=None,
) -> Any:
    """Read-modify-write `path` under a single flock, with a unique tempfile."""

    path.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
    lock_path = path.with_name(path.name + ".lock")
    with open(lock_path, "w", encoding="utf-8") as lock_handle:
        fcntl.flock(lock_handle, fcntl.LOCK_EX)
        try:
            _gc_state_temps(path)
            data: Any = default
            if path.is_file():
                try:
                    data = json.loads(path.read_text(encoding="utf-8"))
                except (json.JSONDecodeError, UnicodeDecodeError):
                    observed_at = datetime.now(UTC).isoformat()
                    corrupt = path.with_name(
                        f"{path.name}.corrupt-{datetime.now(UTC).strftime('%Y%m%dT%H%M%S%fZ')}"
                    )
                    try:
                        os.replace(path, corrupt)
                    except OSError:
                        pass
                    else:
                        try:
                            append_jsonl(
                                path.parent / (path.name + ".quarantine.jsonl"),
                                [{
                                    "event": "state_quarantine",
                                    "file": path.name,
                                    "observed_at": observed_at,
                                }],
                            )
                        except OSError:
                            pass
                    data = default
                except OSError:
                    data = default
            if on_load is not None:
                data = on_load(data)
            new_data = updater(data)
            if isinstance(new_data, Mapping):
                new_data = dict(new_data)
                new_data.pop("history", None)
            persisted_data = redact(new_data)
            fd, tmp_name = tempfile.mkstemp(
                dir=str(path.parent), prefix="." + path.name + ".", suffix=".tmp"
            )
            try:
                with os.fdopen(fd, "w", encoding="utf-8") as handle:
                    json.dump(persisted_data, handle, ensure_ascii=False, indent=2)
                    handle.write("\n")
                    handle.flush()
                    os.fsync(handle.fileno())
                os.chmod(tmp_name, 0o600)
                os.replace(tmp_name, path)
                try:
                    directory_fd = os.open(path.parent, os.O_RDONLY)
                    try:
                        os.fsync(directory_fd)
                    finally:
                        os.close(directory_fd)
                except OSError:
                    pass
            finally:
                if os.path.exists(tmp_name):
                    try:
                        os.unlink(tmp_name)
                    except OSError:
                        pass
            if append_decision is not None:
                _append_decisions_locked(_decisions_path(path), [dict(append_decision)])
            return new_data
        finally:
            fcntl.flock(lock_handle, fcntl.LOCK_UN)


def atomic_write_json(path: Path, payload: Any) -> None:
    """Transactionally replace `path` with `payload` (flock + unique temp)."""

    atomic_update_json(path, lambda _data: payload, default=None)


def _rotate_log(path: Path) -> None:
    """Roll route.jsonl -> .1 -> .2 and drop the oldest, keeping a bounded log."""
    oldest = Path(f"{path}.{MAX_LOG_FILES - 1}")
    if oldest.exists():
        try:
            oldest.unlink()
        except OSError:
            pass
    for i in range(MAX_LOG_FILES - 1, 0, -1):
        src = path if i == 1 else Path(f"{path}.{i - 1}")
        dst = Path(f"{path}.{i}")
        if src.exists():
            os.replace(src, dst)
            try:
                os.chmod(dst, 0o600)
            except OSError:
                pass


def _append_jsonl_locked(path: Path, payload: Any) -> None:
    """Append one record while the caller owns the path lock."""
    if path.is_file() and path.stat().st_size >= MAX_LOG_BYTES:
        _rotate_log(path)
    line = json.dumps(redact(payload), ensure_ascii=False)
    with open(path, "a", encoding="utf-8") as handle:
        handle.write(line + "\n")
        handle.flush()
        os.fsync(handle.fileno())
    try:
        os.chmod(path, 0o600)
    except OSError:
        pass


def append_jsonl(path: Path | str, payload: Any) -> None:
    """Redact and append a payload to a bounded, locked JSONL log."""
    path = Path(path)
    path.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
    lock_path = path.with_name(path.name + ".lock")
    with open(lock_path, "w", encoding="utf-8") as lock_handle:
        fcntl.flock(lock_handle, fcntl.LOCK_EX)
        try:
            _append_jsonl_locked(path, payload)
        finally:
            fcntl.flock(lock_handle, fcntl.LOCK_UN)


def dump(path: Path, value: Any, *, private_dir: Path | None = None) -> None:
    """Atomically write indented JSON, optionally applying private directory modes.

    This intentionally retains its PID-named temporary and has no fsync; its
    durability contract differs from the fsynced JSONL/state writers.
    """
    path.parent.mkdir(parents=True, exist_ok=True)
    is_private = private_dir is not None and (
        path.parent == private_dir or path.is_relative_to(private_dir)
    )
    if is_private:
        path.parent.chmod(0o700)
    temporary = path.with_name(f".{path.name}.{os.getpid()}.tmp")
    temporary.write_text(json.dumps(value, indent=2, ensure_ascii=False) + "\n", encoding="utf-8")
    temporary.chmod(0o600)
    os.replace(temporary, path)
    if is_private:
        path.chmod(0o600)


@contextlib.contextmanager
def state_lock(path: Path, *, lock_name: str = "corpus-sync.lock"):
    """Lock a state directory for the duration of a context."""
    directory = path.parent
    directory.mkdir(parents=True, exist_ok=True)
    directory.chmod(0o700)
    lock_path = directory / lock_name
    lock_path.touch(mode=0o600, exist_ok=True)
    lock_path.chmod(0o600)
    with lock_path.open("r+") as lock:
        fcntl.flock(lock.fileno(), fcntl.LOCK_EX)
        try:
            yield
        finally:
            fcntl.flock(lock.fileno(), fcntl.LOCK_UN)
