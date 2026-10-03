"""Which shape of `dev_iot_device_info` a device family gets.

The Linux models read their MQTT credentials from an `ali`-wrapped block (a
D4SH capture settled that). The ESP32 models read a FLAT block: a Pura X's
proxy capture of the real cloud (upstream issue #35) shows the fields at
`result` level, and served the wrapped block instead it re-signed up every
minute, never having found its credentials. The shape is therefore decided by
the device type in the path, not by which of the three endpoints was called.
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

FLAT_KEYS = {"deviceName", "deviceSecret", "productKey", "mqttHost"}


async def _client():
    client = TestClient(TestServer(create_app(DeviceRegistry(), CONFIG)))
    await client.start_server()
    return client


async def test_esp32_models_get_the_flat_block():
    client = await _client()
    try:
        for dtype in ("d4", "t3", "t4"):
            r = await client.post(f"/6/{dtype}/dev_iot_device_info", headers=HDR)
            assert r.status == 200
            result = (await r.json())["result"]
            assert "ali" not in result, dtype
            assert FLAT_KEYS <= set(result), dtype
    finally:
        await client.close()


async def test_linux_models_keep_the_wrapped_block():
    client = await _client()
    try:
        for dtype, endpoint in (("t5", "dev_only_iot_device_info_v2"),
                                ("d4sh", "dev_iot_device_info")):
            r = await client.post(f"/6/{dtype}/{endpoint}", headers=HDR)
            assert r.status == 200
            result = (await r.json())["result"]
            assert FLAT_KEYS <= set(result["ali"]), dtype
    finally:
        await client.close()
