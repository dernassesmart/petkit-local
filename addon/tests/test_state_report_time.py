"""The `time` on a `dev_state_report` answer is the device's local time.

A YumShare Dual-Hopper 2 (firmware 895) reads its timezone out of that
string's `+HHMM` after every report -- `parse_recv_from_topic` feeds it to a
parser that stores the offset, `get_ctime_timezone` writes it over
`user_info.timezone` -- so a `+0000` answer undid every timezone push within
a minute and fired scheduled meals two hours late (seen 2026-10-11/12).
"""
from petkit_local.utils.timeutil import cloud_timestamp

T = 1791779278  # 2026-10-12T04:27:58Z, the refresh the device logged at 06:27:58 CEST


def test_the_offset_and_the_wall_clock_move_together():
    assert cloud_timestamp(T) == "2026-10-12T04:27:58.000+0000"
    assert cloud_timestamp(T, 2.0) == "2026-10-12T06:27:58.000+0200"
    assert cloud_timestamp(T, -3.5) == "2026-10-12T00:57:58.000-0330"
    assert cloud_timestamp(T, 5.75) == "2026-10-12T10:12:58.000+0545"


def test_the_shape_stays_what_the_device_parses():
    s = cloud_timestamp(T + 0.123, 2.0)
    assert s == "2026-10-12T06:27:58.123+0200"
    assert len(s) == 28 and s[-5] == "+" and ":" not in s[-5:]
    assert cloud_timestamp(T, None) == cloud_timestamp(T)
