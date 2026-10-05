"""After a restart the add-on remembers where a camera was, and re-probes it.

Observed on a YumShare Dual-Hopper: the add-on restarted, the device's IP was
gone with the rest of its live state, and because that model sends a full
state report rarely, no stream was probed for half an hour. No RTSP for go2rtc,
no camera in Home Assistant, no two-way talk. And a probe that failed -- every
patcher ends in a device reboot -- was trusted for ten minutes.
"""
import json

from petkit_local.devices.base import Device
from petkit_local.devices.registry import DeviceRegistry
from petkit_local.media import go2rtc as g


def _cam(ip="192.168.1.50"):
    d = Device(device_type="d4sh", petkit_id=7, serial_number="SN")
    if ip:
        d.state["ip"] = ip
    return d


def test_the_last_ip_survives_a_restart_and_nothing_else_of_state_does():
    d = _cam()
    d.state["streamAvailable"] = True
    d.online = True
    back = Device.from_dict(json.loads(json.dumps(d.to_dict())))
    assert back.state == {"ip": "192.168.1.50"}
    assert back.online is False


def test_a_garbage_last_ip_is_not_seeded():
    d = _cam(ip="::1")
    back = Device.from_dict(json.loads(json.dumps(d.to_dict())))
    assert "ip" not in back.state
    back = Device.from_dict({"device_type": "d4sh", "petkit_id": 7, "last_ip": "not an ip"})
    assert "ip" not in back.state


async def test_the_seeded_ip_is_probed_on_the_first_pass(tmp_path, monkeypatch):
    probed = []

    async def fake_probe(ip, *a, **kw):
        probed.append(ip)
        return True

    monkeypatch.setattr(g, "probe_stream", fake_probe)
    reg = DeviceRegistry()
    restored = Device.from_dict(json.loads(json.dumps(_cam().to_dict())))
    reg._devices[restored.petkit_id] = restored
    s = g.Go2rtc(reg, data_dir=str(tmp_path))
    await s.refresh_probes()
    assert probed == ["192.168.1.50"]
    assert s.desired_streams()


async def test_a_new_ip_is_probed_at_once_even_inside_the_ttl(tmp_path, monkeypatch):
    probed = []

    async def fake_probe(ip, *a, **kw):
        probed.append(ip)
        return False

    monkeypatch.setattr(g, "probe_stream", fake_probe)
    reg = DeviceRegistry()
    d = _cam()
    reg._devices[d.petkit_id] = d
    s = g.Go2rtc(reg, data_dir=str(tmp_path))
    await s.refresh_probes()
    await s.refresh_probes()
    assert probed == ["192.168.1.50"], "the stale-address verdict is cached"
    d.state["ip"] = "192.168.1.51"
    await s.refresh_probes()
    assert probed == ["192.168.1.50", "192.168.1.51"], "a new address is not covered by it"


async def test_a_failed_probe_is_retried_sooner_than_a_good_one(tmp_path, monkeypatch):
    async def fake_probe(ip, *a, **kw):
        return False

    monkeypatch.setattr(g, "probe_stream", fake_probe)
    reg = DeviceRegistry()
    reg._devices[7] = _cam()
    s = g.Go2rtc(reg, data_dir=str(tmp_path))
    await s.refresh_probes()
    deadline, verdict, ip = s._probes[7]
    assert verdict is False and ip == "192.168.1.50"
    assert g.PROBE_RETRY_SECONDS < g.PROBE_TTL_SECONDS
