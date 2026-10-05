"""A device that reports by heartbeat alone is announced to Home Assistant.

The logging middleware publishes availability on a device's first contact by
checking `online` after the handler ran; the heartbeat handler sets the flag
itself, so the middleware saw nothing to announce. After an add-on restart a
YumShare Dual-Hopper stayed unavailable in HA until its next state report.
"""
from unittest import mock

from aiohttp.test_utils import TestClient, TestServer, make_mocked_request

from petkit_local.devices.registry import DeviceRegistry
from petkit_local.http.handlers.heartbeat import handle_heartbeat
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


def _poll_from(app, ip):
    """A heartbeat request whose transport says it came from `ip`.

    Identified by the `id` query parameter: the X-Device header is parsed by
    a middleware a mocked request never passes through.
    """
    transport = mock.Mock()
    transport.get_extra_info.side_effect = lambda key: (ip, 40000) if key == "peername" else None
    return make_mocked_request("GET", "/6/poll/d4sh/heartbeat?id=100", app=app, transport=transport)


async def test_the_heartbeat_records_where_the_device_is():
    """The poll's source address is the device's address.

    A YumShare Dual-Hopper sends the state report that otherwise carries it
    rarely; after a restart the stream probe and the talk sink waited for that
    report while the device was polling every ~15 s. A poll relayed by the TLS
    multiplexer arrives from loopback and must not be recorded.
    """
    reg = DeviceRegistry()
    app = create_app(reg, CONFIG)
    client = TestClient(TestServer(app))
    await client.start_server()
    try:
        await client.post("/6/d4sh/dev_signup", headers=HDR)
        dev = reg.get(100)
        dev.state.pop("ip", None)
        dirty = []
        reg.mark_dirty = lambda: dirty.append(True)  # in-memory registry: the real one is a no-op

        await handle_heartbeat(_poll_from(app, "127.0.0.1"))
        assert "ip" not in dev.state, "loopback is the multiplexer, not the device"
        assert not dirty

        await handle_heartbeat(_poll_from(app, "192.168.1.148"))
        assert dev.state["ip"] == "192.168.1.148"
        assert dirty, "the address is persisted as last_ip for the next restart"

        await handle_heartbeat(_poll_from(app, "192.168.1.148"))
        assert len(dirty) == 1, "an unchanged address is not written again"

        await handle_heartbeat(_poll_from(app, "192.168.1.149"))
        assert dev.state["ip"] == "192.168.1.149", "a new lease is followed"
    finally:
        await client.close()
