from __future__ import annotations

import json
from argparse import Namespace
from pathlib import Path

import pytest

import grokbuild.corpus_sync as corpus_sync


def _write(path: Path, value: object) -> None:
    path.write_text(json.dumps(value, indent=2) + "\n", encoding="utf-8")


def _args(root: Path) -> Namespace:
    return Namespace(corpus=root / "corpus.json", staging=root / "staging.json")


def test_merge_recovers_crash_between_corpus_and_staging_cleanup(
    tmp_path: Path, monkeypatch, capsys
) -> None:
    item = {
        "id": "case-1",
        "prompt": "repair the router",
        "expect": {"intent": "implement", "role": "implement-standard"},
    }
    _write(tmp_path / "corpus.json", {"version": 1, "cases": []})
    _write(tmp_path / "staging.json", [item])
    args = _args(tmp_path)
    real_dump = corpus_sync.dump

    def crash_after_corpus(path: Path, value: object, **kwargs) -> None:
        real_dump(path, value, **kwargs)
        if path == args.corpus:
            raise RuntimeError("simulated crash")

    monkeypatch.setattr(corpus_sync, "dump", crash_after_corpus)
    with pytest.raises(RuntimeError, match="simulated crash"):
        corpus_sync.merge(args)
    monkeypatch.setattr(corpus_sync, "dump", real_dump)
    assert json.loads((tmp_path / "staging.json").read_text()) == [item]

    assert corpus_sync.merge(args) == 0
    output = capsys.readouterr().out
    assert "recovered=1" in output
    recovered_state = (
        (tmp_path / "corpus.json").read_bytes(),
        (tmp_path / "staging.json").read_bytes(),
    )

    clean = tmp_path / "clean"
    clean.mkdir()
    _write(clean / "corpus.json", {"version": 1, "cases": []})
    _write(clean / "staging.json", [item])
    assert corpus_sync.merge(_args(clean)) == 0
    clean_state = (
        clean.joinpath("corpus.json").read_bytes(),
        clean.joinpath("staging.json").read_bytes(),
    )
    assert recovered_state == clean_state
