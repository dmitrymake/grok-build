from grokbuild import gate
from grokbuild.classify import is_synthetic_stop_feedback


def test_all_live_recipe_shapes_are_synthetic() -> None:
    route = {"intent": "review", "session_id": None}
    assert is_synthetic_stop_feedback(gate.station_recipe(route))
    assert is_synthetic_stop_feedback(gate._stage_recipe(route, "review"))
    assert is_synthetic_stop_feedback(gate._hold_recipe(route))


def test_user_route_text_with_marker_is_not_synthetic() -> None:
    marker = gate.STOP_FEEDBACK_MARK
    assert not is_synthetic_stop_feedback(f"route=review: review the diff {marker}.")
    assert not is_synthetic_stop_feedback(f'quoted "route=review: NEXT: review" {marker}.')
    assert not is_synthetic_stop_feedback("route=review: NEXT: review the diff.")
