"""dev_iot_device_info / dev_only_iot_device_info[_v2] — MQTT credentials.

The second of the two handlers that may CREATE a registry entry (the other is
signup): a device that was provisioned before this server existed asks for its
credentials without ever signing up again, and answering "unknown device" would
leave it with no way to reach the broker.
"""
from __future__ import annotations

import logging
from urllib.parse import urlparse

from aiohttp import web

from petkit_local.devices import payloads
from petkit_local.devices.base import Device
from petkit_local.http.handlers._common import (
    device_id, device_serial, no_device_response, request_device,
)
from petkit_local.utils.const import DEVICE_TYPES_ESP32

log = logging.getLogger(__name__)


def self_mqtt_host(config: dict) -> str:
    """Return the broker hostname to hand devices — the box they already reach.

    Derived from the configured `api_url`, not from the request's Host header:
    the device opens a separate MQTT connection, so the value has to be an
    address it can reach on its own.

    Takes the config rather than the request because proxy mode's redaction
    policy needs the same answer with no request in hand, and the two must not
    be able to disagree: redaction rewrites the upstream cloud's broker address
    to whatever this returns, so a second derivation that drifted would hand a
    device an address it is not listening on.

    Returns:
        The hostname, or "" when `api_url` has none to give. Empty is a working
        outcome, not a failure — the device then falls back to the HTTP
        heartbeat, which is slower but needs no broker.
    """
    return urlparse(config.get("api_url", "")).hostname or ""


def _resolve_or_create_device(request: web.Request) -> Device | None:
    """Return the requesting device, registering it if it is unknown.

    Resolution (id, then serial) is the shared one, so an unusable id falls
    back to the serial number and we return the SAME device with its real MQTT
    credentials instead of minting a phantom device with fresh keys. Only if
    that finds nothing AND the request carries a usable id do we create.
    """
    device = request_device(request)
    if device is not None:
        return device

    petkit_id = device_id(request)
    sn = device_serial(request)
    if petkit_id is None:
        log.warning("iot_device_info: no device for id=%s sn=%s (X-Device=%s)",
                    petkit_id, sn, request.get("x_device", {}))
        return None

    registry = request.app["registry"]
    return registry.get_or_create(
        petkit_id=petkit_id,
        device_type=request.get("device_type", "t5"),
        serial_number=sn,
    )


async def handle_iot_device_info(request: web.Request) -> web.Response:
    """`dev_only_iot_device_info[_v2]` (Ingenic) — Aliyun-wrapped credentials.

    Returns:
        `payloads.to_iot_device_info()`: the broker host plus the product key,
        device name and secret the device signs its MQTT CONNECT with (see
        `mqtt/auth.py`), nested in the Aliyun IoT envelope this firmware family
        expects. `handle_iot_device_info_flat` returns the same facts unnested.
    """
    device = _resolve_or_create_device(request)
    if not device:
        return no_device_response()
    mqtt_host = device.resolve_mqtt_host(self_mqtt_host(request.app["config"]))
    # The shape follows the device FAMILY, not the endpoint. An ESP32 model
    # reads its credentials from a flat block — a Pura X's capture of the real
    # cloud (upstream issue #35) has them at `result` level, and served the
    # wrapped block it re-signed up every minute, never having found them.
    # The Linux models read `result.ali` (D4SH capture) and keep it.
    dtype = str(request.get("device_type") or device.device_type or "").lower()
    if dtype in DEVICE_TYPES_ESP32:
        log.info("IoT device info (flat, ESP32 %s): id=%d -> pk=%s dn=%s mqttHost=%s",
                 dtype, device.petkit_id, device.mqtt_product_key,
                 device.mqtt_device_name, mqtt_host)
        return web.json_response(payloads.to_iot_device_info_flat(device, mqtt_host))
    log.info("IoT device info: id=%d -> pk=%s dn=%s mqttHost=%s",
             device.petkit_id, device.mqtt_product_key, device.mqtt_device_name, mqtt_host)
    return web.json_response(payloads.to_iot_device_info(device, mqtt_host))


async def handle_iot_device_info_flat(request: web.Request) -> web.Response:
    """`dev_iot_device_info` (ESP32) — the same credentials, un-nested.

    Resolution and credentials are identical to `handle_iot_device_info`; only
    the response schema differs (`payloads.to_iot_device_info_flat()` — same
    fields without the `ali` wrapper or its `iotPlatform` marker). Both are
    routed, because which endpoint a device calls is decided by its firmware.
    """
    device = _resolve_or_create_device(request)
    if not device:
        return no_device_response()
    mqtt_host = device.resolve_mqtt_host(self_mqtt_host(request.app["config"]))
    log.info("IoT device info (flat): id=%d -> pk=%s dn=%s mqttHost=%s",
             device.petkit_id, device.mqtt_product_key, device.mqtt_device_name, mqtt_host)
    return web.json_response(payloads.to_iot_device_info_flat(device, mqtt_host))
