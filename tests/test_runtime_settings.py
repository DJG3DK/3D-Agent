"""Operator-tunable limits.

The properties that matter are not "a number round-trips". They are: a bad
value cannot break the agent, a typo cannot masquerade as configuration, and
every knob is actually WIRED to something -- a dial in the UI that quietly
drives nothing is worse than no dial.
"""

import pytest

from agent import runtime_settings as rs


class _FakeItem:
    def __init__(self, value):
        self.value = value


class _FakeStore:
    def __init__(self, value=None):
        self.value = value
        self.written = None

    async def aget(self, ns, key):
        return _FakeItem(self.value) if self.value is not None else None

    async def aput(self, ns, key, value):
        self.written = value
        self.value = value


@pytest.fixture(autouse=True)
def _restore_values():
    before = rs.all_values()
    yield
    rs._values.clear()
    rs._values.update(before)


def test_every_knob_has_a_usable_spec():
    for name, spec in rs.KNOBS.items():
        assert spec["label"] and spec["help"], f"{name} needs a label and help text"
        assert spec["min"] < spec["max"], name
        assert spec["min"] <= spec["default"] <= spec["max"], f"{name}'s default is outside its own bounds"
        assert spec["unit"], name
        assert spec["group"], name


def test_defaults_are_live_before_anything_is_stored():
    for name, spec in rs.KNOBS.items():
        assert rs.value(name) == pytest.approx(spec["default"]) or spec.get("env")


@pytest.mark.parametrize("name", list(rs.KNOBS))
def test_values_are_clamped_not_rejected(name):
    """A fat-fingered zero should become the minimum, not an error to decode."""
    spec = rs.KNOBS[name]
    assert rs.clamp(name, 0) == spec["min"]
    assert rs.clamp(name, 10**9) == spec["max"]
    assert rs.clamp(name, spec["default"]) == spec["default"]


@pytest.mark.asyncio
async def test_saving_applies_immediately_without_a_restart():
    store = _FakeStore()
    await rs.save(store, {"planning_turn_budget_usd": 12.5})
    assert rs.value("planning_turn_budget_usd") == 12.5
    assert store.written["planning_turn_budget_usd"] == 12.5


@pytest.mark.asyncio
async def test_saving_one_knob_leaves_the_others_alone():
    store = _FakeStore()
    before = rs.value("model_call_run_limit")
    await rs.save(store, {"planning_turn_budget_usd": 9.0})
    assert rs.value("model_call_run_limit") == before
    assert store.written["model_call_run_limit"] == before


@pytest.mark.asyncio
async def test_an_out_of_range_save_is_clamped_on_the_way_in():
    store = _FakeStore()
    await rs.save(store, {"planning_stall_timeout_s": 5})
    assert rs.value("planning_stall_timeout_s") == rs.KNOBS["planning_stall_timeout_s"]["min"]


@pytest.mark.asyncio
async def test_an_unknown_name_is_rejected_rather_than_stored():
    """A typo must not sit in the database looking like configuration."""
    store = _FakeStore()
    with pytest.raises(ValueError, match="unknown setting"):
        await rs.save(store, {"planing_turn_budget": 5})
    assert store.written is None


@pytest.mark.asyncio
async def test_load_ignores_junk_rather_than_failing_to_boot():
    store = _FakeStore({
        "planning_turn_budget_usd": 7.5,
        "a_knob_from_a_future_version": 1,   # removed/renamed later
        "model_call_run_limit": "not a number",
    })
    await rs.load(store)
    assert rs.value("planning_turn_budget_usd") == 7.5
    # the unusable one keeps its default rather than taking down startup
    assert rs.value("model_call_run_limit") == rs.KNOBS["model_call_run_limit"]["default"]


@pytest.mark.asyncio
async def test_load_survives_a_broken_store():
    class _Broken:
        async def aget(self, ns, key):
            raise RuntimeError("database is down")

    await rs.load(_Broken())  # must not raise
    assert rs.value("planning_turn_budget_usd") > 0


def test_every_knob_is_actually_wired_to_something():
    """A dial that drives nothing is worse than no dial: it invites someone to
    change it and conclude the system ignores them."""
    import pathlib

    root = pathlib.Path(__file__).resolve().parent.parent / "agent"
    sources = "\n".join(
        p.read_text() for p in root.rglob("*.py") if p.name != "runtime_settings.py"
    )
    unwired = [n for n in rs.KNOBS if f'"{n}"' not in sources]
    assert not unwired, f"knobs exposed in the UI but read by nothing: {unwired}"
