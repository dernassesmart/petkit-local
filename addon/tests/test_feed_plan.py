"""The Feeding Plan sensor's view of a stored schedule."""
import time

from petkit_local.ha.feed_plan import feed_plan_view

PLAN = {"schedule": [
    {"re": "1,3,5,7", "it": [{"id": "n_25200", "t": 25200, "a1": 0, "a2": 2},
                             {"id": "n_43200", "t": 43200, "a1": 0, "a2": 2},
                             {"id": "n_64800", "t": 64800, "a1": 0, "a2": 2}],
     "itemJsonString": "[]"},
    {"re": "2,4,6", "it": [{"id": "n_25200", "t": 25200, "a1": 2, "a2": 0},
                           {"id": "n_43200", "t": 43200, "a1": 2, "a2": 0},
                           {"id": "n_64800", "t": 64800, "a1": 2, "a2": 0}]},
]}


def _at(text):
    return time.strptime(text, "%Y-%m-%d %H:%M:%S")


def test_the_next_meal_is_measured_in_petkit_weekdays():
    v = feed_plan_view(PLAN, _at("2026-10-12 13:00:00"))  # a Monday = PetKit day 2
    assert v["next"] == "Mon 18:00 · 2× hopper 1"
    assert (v["next_day"], v["next_time"], v["next_in"]) == ("Mon", "18:00", 5 * 3600)
    v = feed_plan_view(PLAN, _at("2026-10-12 19:00:00"))
    assert v["next"] == "Tue 07:00 · 2× hopper 2" and v["next_in"] == 12 * 3600
    v = feed_plan_view(PLAN, _at("2026-10-11 20:00:00"))  # Sunday = day 1
    assert v["next"] == "Mon 07:00 · 2× hopper 1"


def test_the_attributes_carry_the_flattened_plan():
    v = feed_plan_view(PLAN, _at("2026-10-12 13:00:00"))
    assert v["count"] == 6
    assert v["meals"][0] == {"time": "07:00", "seconds": 25200, "days": [1, 3, 5, 7],
                             "day_names": ["Sun", "Tue", "Thu", "Sat"], "hopper1": 0, "hopper2": 2}
    assert [m["time"] for m in v["meals"]] == ["07:00", "07:00", "12:00", "12:00", "18:00", "18:00"]
    assert v["schedule"][0] == {"re": "1,3,5,7", "it": PLAN["schedule"][0]["it"]}
    assert "itemJsonString" not in v["schedule"][0]


def test_an_empty_or_odd_plan_still_renders():
    assert feed_plan_view(None)["next"] == "no meals"
    assert feed_plan_view({"schedule": []})["count"] == 0
    v = feed_plan_view({"schedule": [{"re": "x,9,2", "it": [{"t": "oops"}, {"t": 60, "a1": 1}]}]},
                       _at("2026-10-12 00:00:30"))
    assert v["meals"] == [{"time": "00:01", "seconds": 60, "days": [2], "day_names": ["Mon"],
                           "hopper1": 1, "hopper2": 0}]
    assert v["next"] == "Mon 00:01 · 1× hopper 1" and v["next_in"] == 30
