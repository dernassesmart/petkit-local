"""The Feeding Plan sensor: the stored schedule in a shape a dashboard can use.

An HA `text` entity caps its state at 255 characters, so the raw-JSON
schedule entity reads `unknown` for any plan with more than a couple of
meals. A sensor's attributes have no such cap: this module turns the stored
`feed_schedule` into a short state (the next meal, "Mon 18:00 · 2× hopper 1")
plus attributes a Markdown card can iterate over.
"""
from __future__ import annotations

import time
from typing import Any

#: PetKit's weekday numbering (`events/codes.py::WEEKDAY_NAMES`): Sunday is 1.
WEEKDAY_SHORT = {1: "Sun", 2: "Mon", 3: "Tue", 4: "Wed", 5: "Thu", 6: "Fri", 7: "Sat"}
DAY_SECONDS = 86400


def _days(group: dict[str, Any]) -> list[int]:
    out = set()
    for part in str(group.get("re", "")).split(","):
        part = part.strip()
        if part.isdigit() and 1 <= int(part) <= 7:
            out.add(int(part))
    return sorted(out)


def _portions(meal: dict[str, Any]) -> str:
    bits = []
    if meal["hopper1"]:
        bits.append(f"{meal['hopper1']}× hopper 1")
    if meal["hopper2"]:
        bits.append(f"{meal['hopper2']}× hopper 2")
    return " + ".join(bits) or "0 portions"


def feed_plan_view(feed: dict[str, Any] | None,
                   now_local: time.struct_time | None = None) -> dict[str, Any]:
    """`{next, next_day, next_time, next_in, count, meals, schedule}`.

    `meals` is the flattened plan sorted by time of day, each with its PetKit
    weekday numbers (`days`), their names and the portions per hopper; `next`
    is the sentence the sensor shows as its state, `next_in` the seconds
    until it. `now_local` is the local wall-clock time "next" is measured
    from (the add-on's zone is Home Assistant's); a parameter so tests can
    pin it.
    """
    groups = feed.get("schedule") if isinstance(feed, dict) else None
    groups = [g for g in (groups or []) if isinstance(g, dict)]
    meals: list[dict[str, Any]] = []
    for group in groups:
        days = _days(group)
        for item in group.get("it") or []:
            if not isinstance(item, dict):
                continue
            try:
                secs = int(item.get("t", 0))
            except (TypeError, ValueError):
                continue
            meals.append({
                "time": f"{secs // 3600:02d}:{(secs // 60) % 60:02d}",
                "seconds": secs,
                "days": days,
                "day_names": [WEEKDAY_SHORT[d] for d in days],
                "hopper1": int(item.get("a1") or 0),
                "hopper2": int(item.get("a2") or 0),
            })
    meals.sort(key=lambda m: (m["seconds"], m["days"]))

    lt = now_local or time.localtime()
    today = (lt.tm_wday + 1) % 7 + 1  # tm_wday Monday=0 -> PetKit 2, Sunday=6 -> 1
    now_secs = lt.tm_hour * 3600 + lt.tm_min * 60 + lt.tm_sec
    best = None
    for offset in range(8):
        day = (today - 1 + offset) % 7 + 1
        for m in meals:
            if day not in m["days"]:
                continue
            wait = offset * DAY_SECONDS + m["seconds"] - now_secs
            if wait > 0 and (best is None or wait < best[0]):
                best = (wait, day, m)
        if best is not None:
            break

    view: dict[str, Any] = {
        "count": len(meals),
        "meals": meals,
        "schedule": [{"re": g.get("re", ""), "it": g.get("it") or []} for g in groups],
    }
    if best is None:
        view.update({"next": "no meals", "next_day": "", "next_time": "", "next_in": None})
    else:
        wait, day, m = best
        view.update({"next": f"{WEEKDAY_SHORT[day]} {m['time']} · {_portions(m)}",
                     "next_day": WEEKDAY_SHORT[day], "next_time": m["time"], "next_in": wait})
    return view
