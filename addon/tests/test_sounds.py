"""Custom feeding sounds reach the device, and in the one format it plays.

Observed on a YumShare Dual-Hopper 2: an uploaded .m4a never played. Three
things, two of them ours: `dev_sound_get` raised (the sounds helper read the
panel's config key on the device-facing server) so the device was told there
were no sounds; Play/Select answered 400 "no event hub" (wrong key again, the
other way round); and the file was stored as received, while the firmware
keeps a download as `user_feed_over_<id>.aac` and plays it as ADTS.
"""
import json
import os

from aiohttp.test_utils import TestClient, TestServer

from petkit_local.devices.base import Device
from petkit_local.devices.registry import DeviceRegistry
from petkit_local.http.server import create_app
from petkit_local.media import sounds as snd
from petkit_local.devices.ble import BLERegistry
from petkit_local.web.hub import EventHub
from petkit_local.web.panel import create_panel_app

HDR = {"X-Device": "id=100&sn=SN100"}


def _adts(frames=25, rate_index=8, payload=100):
    """Synthetic ADTS: `frames` frames of `payload` bytes at 16 kHz (index 8)."""
    out = b""
    for _ in range(frames):
        length = 7 + payload
        hdr = bytes([0xFF, 0xF1, (1 << 6) | (rate_index << 2), (1 << 6) | (length >> 11),
                     (length >> 3) & 0xFF, ((length & 7) << 5) | 0x1F, 0xFC])
        out += hdr + bytes(payload)
    return out


def _seed(tmp_path, device_id=100, filename="sound_1.aac", data=None):
    d = tmp_path / "sounds" / str(device_id)
    d.mkdir(parents=True)
    (d / filename).write_bytes(data if data is not None else _adts())
    (d / "sounds.json").write_text(json.dumps([{
        "id": 1, "name": "Futti", "filename": filename, "size": 1, "digest": "x",
        "duration": 0, "uploaded_at": 1700000000}]))
    return d


def test_adts_is_recognised_and_timed():
    data = _adts(frames=25)  # 25 x 1024 samples at 16 kHz = 1.6 s
    assert snd.is_adts(data)
    assert abs(snd.adts_duration_seconds(data) - 1.6) < 0.01
    assert not snd.is_adts(b"\x00\x00\x00\x18ftypM4A ")
    assert snd.describe(data)["duration"] == 2


async def test_the_device_is_told_about_its_sounds(tmp_path):
    """`dev_sound_get` runs on the device-facing server, whose config key is
    `config`, and lists the upload with the bucket URL and the firmware's
    fields."""
    _seed(tmp_path)
    reg = DeviceRegistry()
    app = create_app(reg, {"api_url": "http://server/6/", "mqtt_port": 1883, "proxy_mode": False,
                           "proxy_upstream": "", "proxy_block_run_cmd": True,
                           "bucket_endpoint": "https://192.0.2.1:9000", "data_dir": str(tmp_path)})
    client = TestClient(TestServer(app))
    await client.start_server()
    try:
        await client.post("/6/d4sh/dev_signup", headers=HDR)
        r = await client.get("/6/d4sh/dev_sound_get", headers=HDR)
        body = await r.json()
    finally:
        await client.close()
    assert r.status == 200
    # Plain HTTP on the API host -- the bucket's HTTPS is beyond the device's wget.
    assert body["result"][0]["url"] == "http://server/sounds/100/sound_1.aac"
    # a STRING: the firmware strcmp()s it without a type check and segfaults on a number
    assert body["result"][0]["gmtCreate"] == "1700000000000"
    assert set(body["result"][0]) >= {"id", "name", "duration", "url", "digest", "size"}


async def test_the_list_never_exceeds_the_firmwares_five_slots(tmp_path):
    d = tmp_path / "sounds" / "100"
    d.mkdir(parents=True)
    rows = []
    for i in range(1, 8):
        (d / f"sound_{i}.aac").write_bytes(_adts(frames=2))
        rows.append({"id": i, "name": f"s{i}", "filename": f"sound_{i}.aac", "size": 1,
                     "digest": "x", "duration": 1, "uploaded_at": 1700000000 + i})
    (d / "sounds.json").write_text(json.dumps(rows))
    reg = DeviceRegistry()
    app = create_app(reg, {"api_url": "http://server/6/", "mqtt_port": 1883, "proxy_mode": False,
                           "proxy_upstream": "", "proxy_block_run_cmd": True, "data_dir": str(tmp_path)})
    client = TestClient(TestServer(app))
    await client.start_server()
    try:
        await client.post("/6/d4sh/dev_signup", headers=HDR)
        body = await (await client.get("/6/d4sh/dev_sound_get", headers=HDR)).json()
    finally:
        await client.close()
    assert len(body["result"]) == 5


async def test_the_api_port_serves_the_sound_file(tmp_path):
    _seed(tmp_path)
    app = create_app(DeviceRegistry(), {"api_url": "http://server/6/", "mqtt_port": 1883, "proxy_mode": False,
                                        "proxy_upstream": "", "proxy_block_run_cmd": True, "data_dir": str(tmp_path)})
    client = TestClient(TestServer(app))
    await client.start_server()
    try:
        r = await client.get("/sounds/100/sound_1.aac")
        data = await r.read()
        missing = await client.get("/sounds/100/nope.aac")
        bad = await client.get("/sounds/100/..%2Fsounds.json")
    finally:
        await client.close()
    assert r.status == 200 and snd.is_adts(data)
    assert missing.status == 404 and bad.status in (400, 404)


async def test_play_and_select_find_the_panels_hub(tmp_path):
    reg = DeviceRegistry()
    d = Device(device_type="d4sh", petkit_id=100, serial_number="SN100")
    reg._devices[100] = d
    cfg = {"api_url": "http://x/6/", "mqtt_tls": True, "mqtt_tls_port": 443,
           "capture": False, "capture_dir": "/nope", "data_dir": str(tmp_path)}
    app = create_panel_app(reg, BLERegistry(), EventHub(), cfg, None)
    client = TestClient(TestServer(app))
    await client.start_server()
    try:
        r = await client.post("/api/devices/100/sounds/1/play")
        assert r.status == 200, await r.text()
        r = await client.post("/api/devices/100/sounds/1/select")
        assert r.status == 200, await r.text()
    finally:
        await client.close()
    queued = [json.loads(c) if isinstance(c, str) else c for c in d.command_queue]
    assert any("play_sound" in json.dumps(c) for c in queued), "play reached the heartbeat queue"
    assert d.config["settings"]["selectedSound"] == 1


async def test_an_upload_is_stored_as_adts_with_its_duration(tmp_path, monkeypatch):
    async def fake_transcode(data, workdir=None):
        assert data == b"not really an m4a"
        return _adts(frames=50)  # 3.2 s

    monkeypatch.setattr("petkit_local.web.api.sounds.transcode_to_adts", fake_transcode)
    reg = DeviceRegistry()
    reg._devices[100] = Device(device_type="d4sh", petkit_id=100, serial_number="SN100")
    cfg = {"api_url": "http://x/6/", "mqtt_tls": True, "mqtt_tls_port": 443,
           "capture": False, "capture_dir": "/nope", "data_dir": str(tmp_path)}
    app = create_panel_app(reg, BLERegistry(), EventHub(), cfg, None)
    client = TestClient(TestServer(app))
    await client.start_server()
    try:
        import aiohttp
        form = aiohttp.FormData()
        form.add_field("file", b"not really an m4a", filename="Futtifutti.m4a", content_type="audio/mp4")
        r = await client.post("/api/devices/100/sounds", data=form)
        body = await r.json()
    finally:
        await client.close()
    assert r.status == 200, body
    assert body["sound"]["filename"] == "sound_1.aac"
    assert not reg._devices[100].command_queue, "no soundList push: the device fetches on select"
    assert body["sound"]["duration"] == 3
    assert body["sound"]["name"] == "Futtifutti"
    stored = (tmp_path / "sounds" / "100" / "sound_1.aac").read_bytes()
    assert snd.is_adts(stored) and body["sound"]["size"] == len(stored)


async def test_a_pre_2_1_21_upload_is_converted_at_startup(tmp_path, monkeypatch):
    d = _seed(tmp_path, filename="sound_1.m4a", data=b"\x00\x00\x00\x18ftypM4A ")

    async def fake_transcode(data, workdir=None):
        return _adts(frames=25)

    monkeypatch.setattr(snd, "transcode_to_adts", fake_transcode)
    assert await snd.migrate_sounds(str(tmp_path)) == 1
    meta = json.loads((d / "sounds.json").read_text())
    assert meta[0]["filename"] == "sound_1.aac" and meta[0]["duration"] == 2
    assert not os.path.exists(d / "sound_1.m4a") and snd.is_adts((d / "sound_1.aac").read_bytes())
    assert await snd.migrate_sounds(str(tmp_path)) == 0, "already ADTS: nothing to do"
