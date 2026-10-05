"""HA event entities get names HA accepts, and nested templates survive a
missing parent key.

Both found in Home Assistant's log on a YumShare Dual-Hopper the minute it
first fed: every `feeding_event` died with "Expected JSON to be parsed as a
dict got <class 'int'>" because the discovery config templated the bare
`event_type` out of the payload, and three sensors logged a "Template variable
error" per state report because the device never sends `state.feedState`.
"""
from jinja2 import Template

from petkit_local.devices.base import Device
from petkit_local.events.normalize import event_name_for
from petkit_local.ha.discovery import EntityDef, _value_template, build_discovery_payload
from petkit_local.ha.entities.events import FEEDER_EVENTS


def test_http_feeder_codes_map_to_the_names_ha_accepts():
    assert event_name_for("3", "d4sh") == "feed_start"
    assert event_name_for("4", "d4sh") == "feed_over"
    assert event_name_for("2", "d4") == "feed_over"
    assert event_name_for("5", "d4sh") == "eat_start"
    # The names from the MQTT path pass through untouched.
    assert event_name_for("feed_over", "d4sh") == "feed_over"
    assert event_name_for("pet_in", "t5") == "pet_in"
    # A code with no event entity (motion, pet) has no name to publish.
    assert event_name_for(None, "d4sh") is None
    assert event_name_for("nonsense", "d4sh") is None


def test_every_mapped_feeder_name_is_one_the_entity_lists():
    options = FEEDER_EVENTS[0].options
    for code in ("2", "3", "4", "5"):
        assert event_name_for(code, "d4sh") in options


def test_event_discovery_has_no_value_template():
    d = Device(device_type="d4sh", petkit_id=7, serial_number="SN")
    for entity in FEEDER_EVENTS:
        payload = build_discovery_payload(
            entity, d.petkit_id, d.device_type, "YumShare", d.serial_number,
            f"petkit-local/{d.petkit_id}/state")
        assert "value_template" not in payload
        assert payload["event_types"] == entity.options


def test_nested_path_renders_empty_when_the_parent_is_missing():
    e = EntityDef(component="sensor", key="total", name="Total",
                  value_path="state.feedState.realAmountTotal")
    tpl = Template(_value_template(e))
    assert tpl.render(value_json={"state": {"food1": 1}}) == ""
    assert tpl.render(value_json={"state": {"feedState": {"realAmountTotal": 12}}}) == "12"
