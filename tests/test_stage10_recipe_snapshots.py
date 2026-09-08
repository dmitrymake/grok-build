from __future__ import annotations

from grokbuild import gate
from grokbuild.classify import STOP_FEEDBACK_MARK
from grokbuild.safe_alternatives import SAFE_ALTERNATIVES


def test_station_recipe_has_one_retrieval_sentence_and_terminal_stop_marker(monkeypatch):
    monkeypatch.setattr(gate, "last_user_prompt", lambda _session: "inspect the change")
    recipe = gate.station_recipe({"intent": "explore", "session_id": "snapshot"})
    assert recipe.count("Retrieve task results one id at a time") == 0
    assert recipe.endswith(f"{STOP_FEEDBACK_MARK}.")


def test_wrong_parallel_member_recipe_preserves_barrier_member_shape():
    recipe = SAFE_ALTERNATIVES["wrong_parallel_member"]
    assert "barrier key" in recipe["operator_context"]
    assert recipe["kind"] == "required_parallel_member"
