from __future__ import annotations

from grokbuild.state import ProviderAvailability, RuntimeState


def test_prune_provider_availability_uses_expiry_and_last_seen():
    state = RuntimeState()
    state.provider_availability = {
        "old": ProviderAvailability(False, 90.0, "old", 0.0),
        "recent": ProviderAvailability(False, 90.0, "recent", 99950.0),
        "future": ProviderAvailability(False, 200000.0, "future", 0.0),
    }
    state.prune(now=100000.0)
    assert set(state.provider_availability) == {"recent", "future"}
    assert set(state.to_dict()["provider_availability"]) == {"recent", "future"}
