"""The `time` component: seconds since midnight <-> Home Assistant's clock.

The W7H schedules its two water-treatment cycles as a time of day, stored as
SECONDS since midnight (43200 is noon) while HA's MQTT time platform speaks
`HH:MM:SS`. Three places do the translation and they must agree — the discovery
value template rendering for HA, `handle_ha_command` parsing HA's write, and the
web panel's own control emitting the same shape.
"""
import pytest

from petkit_local.devices import defaults
from petkit_local.devices.base import Device, Refused
from petkit_local.ha.categories import get_entities_for_device
from petkit_local.ha.commands import _coerce_time, handle_ha_command
from petkit_local.ha.discovery import build_discovery_payload
from petkit_local.ha.entities.times import FOUNTAIN_W7H_TIMES


def _fountain():
    d = Device(device_type="w7h", petkit_id=1, serial_number="SN")
    d.config.setdefault("settings", defaults.default_settings(d))
    index = {e.unique_id_suffix: e for e in get_entities_for_device(d) if e.is_settable}
    return d, index


# The encodings from the capture-derived map: the app's own writes.
@pytest.mark.parametrize("clock,seconds", [
    ("12:00:00", 43200),
    ("13:00:00", 46800),
    ("08:15:00", 29700),
    ("20:00:00", 72000),
    ("00:00:00", 0),
    ("23:59:59", 86399),
])
def test_the_captured_encodings_round_trip(clock, seconds):
    assert _coerce_time(clock) == seconds


def test_seconds_are_optional():
    """A time typed by hand in the panel, or copied from the app's own `13:00`,
    is not rejected over a formatting detail. HA always sends all three."""
    assert _coerce_time("13:00") == 46800
    assert _coerce_time(" 13:00:00 ") == 46800


@pytest.mark.parametrize("bad", [
    "", "noon", "13", "13:00:00:00", "13:0a", "1e2:00",
    "13:60:00",   # minutes out of range
    "13:00:60",   # seconds out of range
    "24:00:00",   # the one that looks valid: a schedule the device never reaches
    "25:00:00",
    "-1:00:00",
])
def test_anything_that_is_not_a_time_is_dropped(bad):
    assert _coerce_time(bad) is None


def test_a_write_reaches_the_device_as_seconds():
    d, idx = _fountain()
    suffix, payload = handle_ha_command(d, idx["flush_time"], "13:00:00")
    assert suffix == "property/set"
    assert payload["params"] == {"flushTime": 46800}
    # Optimistically applied, like every other settings write, so the control
    # stops bouncing back while the command is in flight.
    assert d.config["settings"]["flushTime"] == 46800


def test_an_unparseable_write_changes_nothing():
    d, idx = _fountain()
    assert handle_ha_command(d, idx["water_change_time"], "half past two") is None
    assert "waterChangeTime" not in d.config["settings"]


def test_the_discovery_payload_is_a_settable_time():
    d, idx = _fountain()
    entity = idx["flush_time"]
    payload = build_discovery_payload(
        entity, 1, "w7h", "Fountain", "SN", "petkit-local/1/state")
    assert payload["command_topic"] == "petkit-local/1/cmd/flush_time"
    # HA's time platform has no `format` option, and an unknown key makes it
    # reject the whole discovery message rather than complain about that key.
    assert "format" not in payload
    assert "min" not in payload and "max" not in payload


def test_the_value_template_renders_a_clock_and_blanks_an_unknown():
    """Rendered by Home Assistant, never by these tests — so the assertion is
    on the Jinja itself. What matters is that an absent setting produces
    NOTHING rather than midnight: none of these fields is seeded, and a
    00:00:00 would show a schedule nobody set."""
    d, idx = _fountain()
    tmpl = build_discovery_payload(
        idx["flush_time"], 1, "w7h", "Fountain", "SN",
        "petkit-local/1/state")["value_template"]
    # Intermediate keys through `.get(key, {})` since 2.1.11: a device that
    # never sends `settings` must not make HA log a template error per state.
    assert "value_json.get('settings', {}).flushTime" in tmpl
    assert "| default(-1) | int(-1)" in tmpl
    assert "'%02d:%02d:%02d' | format" in tmpl


def test_every_time_entity_reads_a_settings_field():
    """`handle_ha_command` writes `setting_field`, the last path segment. A
    `time` pointing anywhere else would be accepted and silently dropped."""
    for entity in FOUNTAIN_W7H_TIMES:
        assert entity.value_path.startswith("settings.")
        assert entity.setting_field
        assert entity.is_settable


def test_a_time_is_not_bound_by_the_number_range():
    """`min_value`/`max_value` mean nothing on a `time` entity, and a time of
    day in seconds is far outside any plausible number range. The number
    branch's check must not be reached — 46800 is not "out of range", it is one
    o'clock."""
    d, idx = _fountain()
    try:
        handle_ha_command(d, idx["flush_time"], "13:00:00")
    except Refused as exc:  # pragma: no cover - the failure this guards
        pytest.fail(f"a valid time was refused as an out-of-range number: {exc}")
