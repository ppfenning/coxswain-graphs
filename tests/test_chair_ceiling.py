import json

from runner.tier_resolution import clamp_to_chair

# The contract as the tools repository publishes it, pasted so no file from that repository is read.
_CONTRACT = '{"env": "COXSWAIN_CHAIR_MODEL", "aliases": ["sonnet", "opus", "haiku"]}'
_ALIASES = json.loads(_CONTRACT)["aliases"]
_EXEMPT = ("validate_phase",)


def test_every_contract_alias_is_known_to_the_clamp():
    assert all("unknown" not in clamp_to_chair("deep", "build", a, _EXEMPT)[1] for a in _ALIASES)


def test_a_sonnet_chair_lowers_a_deep_build_to_sonnet():
    assert clamp_to_chair("deep", "build", "sonnet", _EXEMPT) == ("standard", "chair_ceiling")


def test_an_exempt_role_keeps_opus_under_a_sonnet_chair():
    assert clamp_to_chair("deep", "validate_phase", "sonnet", _EXEMPT) == (
        "deep",
        "role 'validate_phase' is exempt from the chair ceiling",
    )


def test_an_opus_chair_clamps_nothing():
    assert clamp_to_chair("deep", "build", "opus", _EXEMPT) == ("deep", "within ceiling")


def test_an_unknown_chair_model_clamps_nothing_and_is_named():
    assert clamp_to_chair("deep", "build", "gpt-x", _EXEMPT) == ("deep", "unknown chair model 'gpt-x'")


def test_a_haiku_chair_lowers_a_standard_seat_to_haiku():
    assert clamp_to_chair("standard", "build", "haiku", _EXEMPT) == ("cheap", "chair_ceiling")


def test_no_chair_model_clamps_nothing():
    assert clamp_to_chair("deep", "build", None, _EXEMPT) == ("deep", "no chair model given")
