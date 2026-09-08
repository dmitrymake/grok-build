#!/usr/bin/env python3
"""Deterministic upstream model discovery tests. No pytest, no network."""

from __future__ import annotations

from _harness import make_check

from _harness import run_standalone

from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parents[1]
from _harness import bootstrap

bootstrap()
ROOT = REPO_ROOT / "grokbuild"

import email.message
import json
import stat
import tempfile
import urllib.error

import pytest


import grokbuild.discovery as discovery  # noqa: E402
from grokbuild.discovery import (  # noqa: E402
    CACHE_VERSION,
    STALE_AFTER,
    _iso,
    _quota_codex,
    _quota_opencode,
    diff,
    load_cache,
    load_targets,
    quota_report,
    stale_targets,
    sync,
)

FAILURES: list[str] = []


check = make_check(FAILURES)

CONFIG = """
[model."alpha-std"]
model = "alpha-raw"
base_url = "https://api.alpha.test/v1"
env_key = "ALPHA_KEY"

[model."alpha-flash"]
model = "alpha-flash"
base_url = "https://api.alpha.test/v1"
env_key = "ALPHA_KEY"

[model."beta-model"]
model = "beta-raw"
base_url = "http://127.0.0.1:9/v1"
env_key = "BETA_KEY"

[model.no-url]
model = "local-only"
"""

PROVIDERS = {
    "version": 2,
    "providers": {
        "alpha": {
            "label": "Alpha Plan",
            "auth": "api_key",
            "credential_env": "ALPHA_KEY",
            "models": {
                "alpha-std": {"tier": "standard"},
                "alpha-flash": {"tier": "cheap"},
            },
        }
    },
}


class _Response:
    status = 200

    def __init__(self, payload: bytes = b'{"data": []}') -> None:
        self.payload = payload

    def __enter__(self):
        return self

    def __exit__(self, *_args):
        return False

    def read(self, _limit: int) -> bytes:
        return self.payload


class _RedirectingOpener:
    def __init__(self, location: str) -> None:
        self.location = location
        self.requests = []

    def open(self, request, timeout):
        self.requests.append(request)
        if len(self.requests) == 1:
            headers = email.message.Message()
            headers["Location"] = self.location
            raise urllib.error.HTTPError(request.full_url, 302, "redirect", headers, None)
        return _Response()


def test_authenticated_discovery_rejects_cross_origin_redirect(monkeypatch) -> None:
    opener = _RedirectingOpener("https://attacker.test/models")
    monkeypatch.setattr(discovery, "_AUTHENTICATED_OPENER", opener)
    status, payload, error = discovery._get_json("https://api.alpha.test/models", "secret")
    assert (status, payload, error) == (302, None, "unsafe-auth-redirect")
    assert len(opener.requests) == 1
    assert opener.requests[0].get_header("Authorization") == "Bearer secret"


def test_authenticated_discovery_follows_same_origin_https_redirect(monkeypatch) -> None:
    opener = _RedirectingOpener("/v2/models")
    monkeypatch.setattr(discovery, "_AUTHENTICATED_OPENER", opener)
    assert discovery._get_json("https://api.alpha.test/models", "secret") == (
        200,
        {"data": []},
        None,
    )
    assert [request.full_url for request in opener.requests] == [
        "https://api.alpha.test/models",
        "https://api.alpha.test/v2/models",
    ]


def test_authenticated_discovery_refuses_http_downgrade(monkeypatch) -> None:
    opener = _RedirectingOpener("http://api.alpha.test/models")
    monkeypatch.setattr(discovery, "_AUTHENTICATED_OPENER", opener)
    assert discovery._get_json("https://api.alpha.test/models", "secret") == (
        302,
        None,
        "unsafe-auth-redirect",
    )
    assert len(opener.requests) == 1


@pytest.mark.parametrize(
    "location",
    ("https://api.alpha.test:notaport/models", "https://[::1/models"),
)
def test_authenticated_discovery_rejects_malformed_redirect(monkeypatch, location: str) -> None:
    opener = _RedirectingOpener(location)
    monkeypatch.setattr(discovery, "_AUTHENTICATED_OPENER", opener)
    assert discovery._get_json("https://api.alpha.test/models", "secret") == (
        302,
        None,
        "unsafe-auth-redirect",
    )
    assert len(opener.requests) == 1


def test_authenticated_discovery_rejects_malformed_initial_url() -> None:
    assert discovery._get_json("https://api.alpha.test:notaport/models", "secret") == (
        None,
        None,
        "invalid-auth-url",
    )


def test_endpoint_keyed_discovery_targets(tmp_path: Path) -> None:
    config = tmp_path / "config.toml"
    providers = tmp_path / "providers.json"
    config.write_text(
        """
[model.logical]
model = "logical-raw"
base_url = "https://primary.test/v1"
env_key = "PRIMARY_KEY"

[model.reserve-binding]
model = "reserve-raw"
base_url = "https://reserve.test/v1"
env_key = "RESERVE_KEY"
""",
        encoding="utf-8",
    )
    providers.write_text(
        json.dumps(
            {
                "version": 2,
                "providers": {
                    "primary": {
                        "label": "Primary",
                        "credential_env": "PRIMARY_KEY",
                        "models": {
                            "logical": {
                                "tier": "standard",
                                "endpoints": [
                                    {
                                        "provider": "primary",
                                        "credential_env": "PRIMARY_KEY",
                                    },
                                    {
                                        "provider": "reserve",
                                        "credential_env": "RESERVE_KEY",
                                    },
                                ],
                            }
                        },
                    },
                    "reserve": {
                        "label": "Reserve",
                        "credential_env": "RESERVE_KEY",
                        "models": {"reserve-binding": {"tier": "reserve"}},
                    },
                },
            }
        ),
        encoding="utf-8",
    )

    targets, warnings = load_targets(config, providers)
    assert not warnings
    assert targets["logical@primary"].base_url == "https://primary.test/v1"
    assert targets["logical@reserve"].base_url == "https://reserve.test/v1"
    assert targets["logical@reserve"].provider == "reserve"
    assert targets["logical@reserve"].env_key == "RESERVE_KEY"


def test_stale_targets_prefers_quota_timestamp_and_keeps_missing() -> None:
    target = discovery.DiscoveryTarget(
        provider="alpha",
        label="Alpha",
        base_url="https://api.alpha.test/v1",
        env_key="ALPHA_KEY",
        catalog_ids=("alpha-std",),
        raw_ids=("alpha-raw",),
    )
    targets = {"fresh": target, "stale": target, "missing": target}
    cache = {
        "providers": {
            "fresh": {"fetched_at": 1.0, "quota": {"fetched_at": 950.0}},
            "stale": {"fetched_at": 950.0, "quota": {"fetched_at": 1.0}},
        }
    }

    assert stale_targets(targets, cache, 100.0, now=1000.0) == {
        "stale": target,
        "missing": target,
    }


def test_quota_only_sync_skips_model_fetch_and_preserves_cache(tmp_path: Path) -> None:
    cache_path = tmp_path / "models.json"
    cache_path.write_text(
        json.dumps(
            {
                "version": CACHE_VERSION,
                "providers": {
                    "alpha": {
                        "status": "ok",
                        "models": ["alpha-raw"],
                        "fetched_at": 100.0,
                    }
                },
            }
        ),
        encoding="utf-8",
    )
    target = discovery.DiscoveryTarget(
        provider="alpha",
        label="Alpha",
        base_url="https://api.alpha.test/v1",
        env_key="ALPHA_KEY",
        catalog_ids=("alpha-std",),
        raw_ids=("alpha-raw",),
        quota_probe="alpha",
    )
    fetch_calls: list[str] = []
    sync(
        {"alpha": target},
        env={"ALPHA_KEY": "secret"},
        fetch=lambda base_url, _bearer: (fetch_calls.append(base_url) or (200, ["changed"], None)),
        path=cache_path,
        now=200.0,
        quota_probes={"alpha": lambda *_args: {"windows": [{"used_percent": 42.0}]}},
        quota_only=True,
    )

    entry = load_cache(cache_path)["providers"]["alpha"]
    assert fetch_calls == []
    assert entry["status"] == "ok"
    assert entry["models"] == ["alpha-raw"]
    assert entry["fetched_at"] == 100.0
    assert entry["quota"]["fetched_at"] == 200.0


def test_discovery() -> int:
    with tempfile.TemporaryDirectory() as raw:
        base = Path(raw)
        config_path = base / "config.toml"
        providers_path = base / "providers.json"
        cache_path = base / "models.json"
        config_path.write_text(CONFIG, encoding="utf-8")
        providers_path.write_text(json.dumps(PROVIDERS), encoding="utf-8")

        targets, warnings = load_targets(config_path, providers_path)
        check(
            set(targets) == {"alpha", "127.0.0.1"},
            "targets grouped by provider, url-less block skipped",
        )
        alpha = targets["alpha"]
        check(
            alpha.label == "Alpha Plan" and alpha.env_key == "ALPHA_KEY",
            "provider label/env from catalog",
        )
        check(
            alpha.raw_ids == ("alpha-flash", "alpha-raw"), "raw api ids collected from model field"
        )
        check(
            alpha.catalog_ids == ("alpha-flash", "alpha-std"),
            "catalog ids collected from block keys",
        )
        check(targets["127.0.0.1"].label == "127.0.0.1", "unknown provider falls back to host")
        check(warnings == [], "clean fixture yields no warnings")

        missing = load_targets(base / "absent.toml", providers_path)
        check(missing[0] == {} and missing[1], "missing config yields no targets plus warning")

        calls: list[str] = []

        def fetch_ok(base_url: str, bearer: str):
            calls.append(base_url)
            return 200, ["alpha-raw", "new-model", "new-model"], None

        env = {"ALPHA_KEY": "sk-alpha-secret-value"}
        result = sync(targets, env=env, fetch=fetch_ok, path=cache_path, now=1000.0)
        check(calls == ["https://api.alpha.test/v1"], "unset credential skips the network attempt")
        alpha_entry = result["providers"]["alpha"]
        beta_entry = result["providers"]["127.0.0.1"]
        check(
            alpha_entry["status"] == "ok" and alpha_entry["models"] == ["alpha-raw", "new-model"],
            "sync dedupes and sorts upstream ids",
        )
        check(beta_entry["status"] == "credential-unset", "credential-unset recorded honestly")

        text = cache_path.read_text(encoding="utf-8")
        check("sk-alpha-secret-value" not in text, "cache contains no credential value")
        check(stat.S_IMODE(cache_path.stat().st_mode) == 0o600, "cache mode 0600")

        def fetch_fail(base_url: str, bearer: str):
            return None, None, "network:URLError"

        sync(targets, env=env, fetch=fetch_fail, path=cache_path, now=2000.0)
        cached = load_cache(cache_path)
        alpha_cached = cached["providers"]["alpha"]
        check(
            alpha_cached["status"] == "error" and alpha_cached["error"] == "network:URLError",
            "failed attempt records sanitized error",
        )
        check(
            alpha_cached["models"] == ["alpha-raw", "new-model"]
            and alpha_cached["fetched_at"] == 1000.0,
            "failure preserves last-good list and stamp",
        )
        check(alpha_cached["attempted_at"] == 2000.0, "failure updates attempted_at")

        report = diff(targets, cached, now=3000.0)
        alpha_report = report["providers"]["alpha"]
        check(
            alpha_report["missing_upstream"] == ["alpha-flash"],
            "configured pin absent upstream is a dead pin",
        )
        check(
            alpha_report["unbound"] == ["new-model"],
            "upstream model without a binding is a candidate",
        )
        check(
            alpha_report["age_seconds"] == 2000.0 and alpha_report["stale"] is False,
            "diff reports cache age",
        )
        beta_report = report["providers"]["127.0.0.1"]
        check(
            beta_report["missing_upstream"] is None and beta_report["unbound"] is None,
            "no upstream data yields no verdicts",
        )

        stale_report = diff(targets, cached, now=1000.0 + STALE_AFTER + 1)
        check(stale_report["providers"]["alpha"]["stale"] is True, "old fetch is flagged stale")

        cache_path.write_text("broken", encoding="utf-8")
        check(
            load_cache(cache_path) == {"version": CACHE_VERSION, "providers": {}},
            "corrupt cache resets to empty root",
        )
        cache_path.write_text(
            json.dumps({"version": 999, "providers": {"x": {}}}), encoding="utf-8"
        )
        check(load_cache(cache_path)["providers"] == {}, "version mismatch resets to empty root")

        quota_path = base / "quota-cache.json"
        alpha_quota = {
            "plan": "plus",
            "windows": [
                {"name": "weekly", "used_percent": 33.0, "resets_at": "2026-08-24T00:00:00Z"}
            ],
        }
        probes = {"alpha": lambda base_url, bearer: alpha_quota}
        result = sync(
            targets, env=env, fetch=fetch_ok, path=quota_path, now=100.0, quota_probes=probes
        )
        recorded = result["providers"]["alpha"].get("quota")
        check(
            recorded == {**alpha_quota, "fetched_at": 100.0},
            "quota probe result recorded with fetch stamp",
        )
        check(
            "quota" not in result["providers"]["127.0.0.1"],
            "credential-unset provider skips quota probe",
        )

        sync(
            targets,
            env=env,
            fetch=fetch_ok,
            path=quota_path,
            now=200.0,
            quota_probes={"alpha": lambda b, k: None},
        )
        cached_quota = load_cache(quota_path)["providers"]["alpha"].get("quota")
        check(
            cached_quota == {**alpha_quota, "fetched_at": 100.0},
            "failed quota probe preserves last snapshot",
        )

        report = quota_report(targets, load_cache(quota_path), now=200.0, quota_probes=probes)
        alpha_q = report["providers"]["alpha"]
        beta_q = report["providers"]["127.0.0.1"]
        check(
            alpha_q["supported"] and alpha_q["age_seconds"] == 100.0,
            "quota report exposes age for supported provider",
        )
        check(
            beta_q["supported"] is False and beta_q["quota"] is None,
            "quota report marks providers without probe",
        )

        check(_iso(1787816034) == "2026-08-27T07:33:54Z", "epoch normalizes to ISO UTC")
        check(
            _iso("2026-08-24T00:00:00.380Z") == "2026-08-24T00:00:00.380Z",
            "ISO string passes through",
        )
        check(_iso("") is None and _iso(0) is None, "empty reset values become None")

        real_get_json = discovery._get_json
        try:
            discovery._get_json = lambda url, bearer: (
                200,
                {
                    "usage": {
                        "rolling": {
                            "status": "ok",
                            "percent": 0,
                            "resetsAt": "2026-08-22T02:37:40Z",
                        },
                        "weekly": {
                            "status": "ok",
                            "percent": 33,
                            "resetsAt": "2026-08-24T00:00:00Z",
                        },
                        "monthly": {
                            "status": "ok",
                            "percent": 72,
                            "resetsAt": "2026-09-05T16:05:19Z",
                        },
                    }
                },
                None,
            )
            oc = _quota_opencode("https://x", "k")
            check(
                oc is not None
                and [w["name"] for w in oc["windows"]] == ["rolling", "weekly", "monthly"],
                "opencode usage parses all windows",
            )
            check(oc["windows"][1]["used_percent"] == 33.0, "opencode percent parsed")

            discovery._get_json = lambda url, bearer: (
                200,
                {
                    "captured_at": 42.0,
                    "headers": {
                        "x-codex-plan-type": "pro",
                        "x-codex-primary-used-percent": "2",
                        "x-codex-primary-window-minutes": "10080",
                        "x-codex-primary-reset-at": "1787816034",
                        "x-codex-secondary-used-percent": "0",
                    },
                },
                None,
            )
            cx = _quota_codex("http://127.0.0.1:1456/v1", "k")
            check(
                cx is not None and cx["plan"] == "pro" and cx["captured_at"] == 42.0,
                "codex snapshot parses plan and stamp",
            )
            primary = cx["windows"][0]
            check(
                primary["used_percent"] == 2.0
                and primary["window_minutes"] == 10080
                and primary["resets_at"] == "2026-08-27T07:33:54Z",
                "codex primary window normalized",
            )

            discovery._get_json = lambda url, bearer: (
                200,
                {"captured_at": None, "headers": {}},
                None,
            )
            check(
                _quota_codex("http://127.0.0.1:1456/v1", "k") is None,
                "empty codex snapshot yields no quota",
            )
        finally:
            discovery._get_json = real_get_json

    if FAILURES:
        print(f"{len(FAILURES)} failures")
        return
    print("discovery tests passed")
    return


def main() -> int:
    test_authenticated_discovery_rejects_malformed_initial_url()
    test_stale_targets_prefers_quota_timestamp_and_keeps_missing()
    test_discovery()


if __name__ == "__main__":
    run_standalone(main)
