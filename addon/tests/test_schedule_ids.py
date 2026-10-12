"""A feeder meal's id is its time, rebuilt on every save.

The cloud's scheme is `n_<seconds since midnight>`; the editor used to keep
whatever id a meal was created with, so a meal moved from 08:00 to 07:15 kept
`n_28800` and the device, which keys meals by that 7-byte id, never fired it
(YumShare Dual-Hopper 2, 2026-10-12).
"""
from petkit_local.web.api.schedules import _clean_feed_schedule


def test_a_moved_meal_gets_the_id_of_its_new_time():
    feed = _clean_feed_schedule({"schedule": [
        {"re": "1,3,5,7", "it": [{"id": "n_28800", "t": 28800, "a1": 0, "a2": 2}]},
        {"re": "2,4,6", "it": [{"id": "n_28800", "t": 26100, "a1": 2, "a2": 0}]},
    ]})
    ids = [m["id"] for g in feed["schedule"] for m in g["it"]]
    assert ids == ["n_28800", "n_26100"]
    assert feed["schedule"][1]["itemJsonString"] == '[{"a1":2,"a2":0,"id":"n_26100","t":26100}]'


def test_a_meal_without_an_id_gets_one_too():
    feed = _clean_feed_schedule({"schedule": [{"re": "2", "it": [{"t": 50400, "a1": 1}]}]})
    assert feed["schedule"][0]["it"] == [{"id": "n_50400", "t": 50400, "a1": 1, "a2": 0}]
