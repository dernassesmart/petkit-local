"""A device that reports by heartbeat alone is announced to Home Assistant.

The logging middleware publishes availability on a device's first contact by
checking `online` after the handler ran; the heartbeat handler sets the flag
itself, so the middleware saw nothing to announce. After an add-on restart a
YumShare Dual-Hopper stayed unavailable in HA until its next state report.
"""
from aiohttp.test_utils import TestClient, TestServer

from petkit_local.devices.registry import DeviceRegistry
from petkit_local.http.server import create_app

CONFIG = {
    "api_url": "http://server/6/",
    "mqtt_port": 1883,
    "proxy_mode": False,
    "proxy_upstream": "",
    "proxy_block_run_cmd": True,
}
HDR = {"X-Device": "id=100&sn=SN100"}


async def test_first_heartbeat_after_restart_announces_the_device():
    reg = DeviceRegistry()
    seen = []
    app = create_app(reg, CONFIG)
    app["on_device_seen"] = lambda device: _record(seen, device)
    client = TestClient(TestServer(app))
    await client.start_server()
    try:
        await client.post("/6/d4sh/dev_signup", headers=HDR)
        dev = reg.get(100)
        dev.online = False  # what a restart leaves behind: known, but not yet heard from
        seen.clear()  # the signup itself was announced; that is not the case under test

        r = await client.get("/6/poll/d4sh/heartbeat", headers=HDR)
        assert r.status == 200
        assert seen == [100], "the first heartbeat must announce the device"
        assert dev.online is True

        await client.get("/6/poll/d4sh/heartbeat", headers=HDR)
        assert seen == [100], "a device already online is not announced again"
    finally:
        await client.close()


async def _record(seen, device):
    seen.append(device.petkit_id)
