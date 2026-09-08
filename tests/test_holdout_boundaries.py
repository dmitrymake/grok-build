from eval.holdout import file_digest, validate_holdout_boundary


def test_external_immutable_holdout_boundary(tmp_path):
    workspace = tmp_path / "workspace"
    holdout = tmp_path / "private"
    workspace.mkdir(); holdout.mkdir()
    manifest = holdout / "cases.bin"
    manifest.write_bytes(b"hidden")
    result = validate_holdout_boundary(
        holdout_root=holdout,
        workspace_root=workspace,
        manifest={"files": "cases.bin", "digest": file_digest(manifest)},
        network="off",
        grader_read_only=True,
        actor_views={"proposer": (), "applier": (), "judge": ()},
    )
    assert result.verified


def test_workspace_descendant_symlink_digest_and_capability_leaks_fail_closed(tmp_path):
    workspace = tmp_path / "workspace"; workspace.mkdir()
    inside = workspace / "holdout"; inside.mkdir()
    cases = inside / "cases.bin"; cases.write_bytes(b"hidden")
    common = dict(
        holdout_root=inside,
        workspace_root=workspace,
        manifest={"files": "cases.bin", "digest": file_digest(cases)},
        network="off",
        grader_read_only=True,
        actor_views={"proposer": (), "applier": (), "judge": ()},
    )
    assert not validate_holdout_boundary(**common).verified
    external = tmp_path / "external"; external.mkdir()
    hidden = external / "cases.bin"; hidden.write_bytes(b"changed")
    common.update(holdout_root=external, manifest={"files": "cases.bin", "digest": "wrong"})
    assert not validate_holdout_boundary(**common).verified
    common.update(manifest={"files": "cases.bin", "digest": file_digest(hidden)}, actor_views={"judge": ("cases.bin",)})
    assert not validate_holdout_boundary(**common).verified
