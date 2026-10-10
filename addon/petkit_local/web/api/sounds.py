"""Custom sounds: upload, list, delete, play and select.

A camera feeder can play custom audio through its speaker. The flow:

1. Upload an audio file through this API
2. The sound appears in ``dev_sound_get`` for the device to download
3. ``property.set{selectedSound: N}`` selects the active sound
4. ``thing/service/play_sound{soundId: N}`` plays it

Sound files live in ``{data_dir}/sounds/{device_id}/`` with a metadata
sidecar ``sounds.json``. The device downloads them from the bucket
endpoint, so the file is served at ``/sounds/{device_id}/{filename}``
on the bucket port.
"""
from __future__ import annotations

import json
import logging
import os
import time
from typing import Any
from urllib.parse import urlparse

from aiohttp import web

from petkit_local.ha.commands import make_mqtt_property_set, _envelope
from petkit_local.media.sounds import TranscodeError, describe, transcode_to_adts
from petkit_local.web.api._common import _device_or_404, _deliver, _refuse

log = logging.getLogger(__name__)

MAX_SOUND_SIZE = 2 * 1024 * 1024  # 2 MB
MAX_SOUNDS_PER_DEVICE = 10


def _sounds_dir(request: web.Request, device_id: int) -> str:
    # The panel keeps the add-on config under "cfg", the device-facing API
    # under "config" -- and `dev_sound_get` (handlers/stubs.py) runs on the
    # latter. Reading "cfg" alone raised KeyError there, the never-fail
    # middleware answered the device an empty list, and no uploaded sound ever
    # reached a device.
    cfg = request.app.get("cfg") or request.app.get("config") or {}
    data_dir = cfg.get("data_dir", "/data") if hasattr(cfg, "get") else "/data"
    return os.path.join(data_dir, "sounds", str(device_id))


def _hub_and_bridge(request: web.Request) -> tuple[Any, Any]:
    """The event hub and the MQTT bridge, whichever app this runs on.

    The panel registers its hub as "hub" (`web/appkeys.py::HUB`); "event_hub"
    is the device-facing server's name for it. Play and Select run on the
    panel and looked only for the latter, so every click answered 400
    "no event hub".
    """
    app = request.app
    return (app.get("hub") or app.get("event_hub")), app.get("bridge")


def _sounds_meta_path(request: web.Request, device_id: int) -> str:
    return os.path.join(_sounds_dir(request, device_id), "sounds.json")


def _load_sounds(request: web.Request, device_id: int) -> list[dict[str, Any]]:
    path = _sounds_meta_path(request, device_id)
    if not os.path.isfile(path):
        return []
    try:
        with open(path) as f:
            return json.load(f)
    except (json.JSONDecodeError, OSError):
        return []


def _save_sounds(request: web.Request, device_id: int,
                 sounds: list[dict[str, Any]]) -> None:
    d = _sounds_dir(request, device_id)
    os.makedirs(d, exist_ok=True)
    path = _sounds_meta_path(request, device_id)
    with open(path, "w") as f:
        json.dump(sounds, f, indent=2)


def _next_id(sounds: list[dict[str, Any]]) -> int:
    if not sounds:
        return 1
    return max(s["id"] for s in sounds) + 1


def sound_download_base(cfg: Any, request_host: str = "") -> str:
    """`http://<api host>`: where a device fetches its sounds from.

    The API host, not the bucket: the bucket is HTTPS, and the BusyBox wget
    the firmware downloads with cannot complete a TLS handshake with it on
    the Axera YumShare -- see `handlers/stubs.py::handle_sound_get` for what
    the failed download did to the device. The face photos have always been
    served this way and that download is proven on the same firmware.
    """
    host = urlparse(str(cfg.get("api_url", "") if hasattr(cfg, "get") else "")).netloc or request_host
    return f"http://{host}" if host else ""


def sound_list_for_device(request: web.Request, device_id: int,
                          base_url: str) -> list[dict[str, Any]]:
    """Build the ``dev_sound_get`` result list with download URLs under `base_url`."""
    sounds = _load_sounds(request, device_id)
    if not sounds or not base_url:
        return []
    base = base_url.rstrip("/")
    result = []
    for s in sounds:
        result.append({
            "id": s["id"],
            "name": s.get("name", ""),
            "duration": s.get("duration", 0),
            "url": f"{base}/sounds/{device_id}/{s['filename']}",
            "digest": s.get("digest", ""),
            "size": s.get("size", 0),
            # Read by the firmware alongside digest and duration (`ctrl`
            # strings); milliseconds, as PetKit's own timestamps are.
            "gmtCreate": int(s.get("uploaded_at", 0)) * 1000,
        })
    return result


async def api_sounds_list(request: web.Request) -> web.Response:
    d = _device_or_404(request)
    sounds = _load_sounds(request, d.petkit_id)
    return web.json_response({"sounds": sounds})


async def api_sounds_upload(request: web.Request) -> web.Response:
    d = _device_or_404(request)
    sounds = _load_sounds(request, d.petkit_id)
    if len(sounds) >= MAX_SOUNDS_PER_DEVICE:
        raise _refuse(web.HTTPBadRequest, f"max {MAX_SOUNDS_PER_DEVICE} sounds")

    reader = await request.multipart()
    field = await reader.next()
    if field is None or field.name != "file":
        raise _refuse(web.HTTPBadRequest, "missing file field")

    filename_raw = field.filename or "sound.mp3"
    ext = os.path.splitext(filename_raw)[1].lower() or ".mp3"
    data = await field.read(decode=False)
    if len(data) > MAX_SOUND_SIZE:
        raise _refuse(web.HTTPBadRequest, f"file too large (max {MAX_SOUND_SIZE // 1024}KB)")
    if not data:
        raise _refuse(web.HTTPBadRequest, "empty file")

    sound_id = _next_id(sounds)
    d_dir = _sounds_dir(request, d.petkit_id)
    os.makedirs(d_dir, exist_ok=True)

    # The device keeps a download as `/opt/user_feed_over_<id>.aac` and plays
    # it through the ADTS parser its own prompts use, whatever the upload
    # was -- an .m4a from a phone went down fine and never made a sound. So
    # every upload becomes ADTS AAC-LC 16 kHz mono here (`media/sounds.py`),
    # and the entry carries the duration the device is told.
    try:
        adts = await transcode_to_adts(data, workdir=d_dir)
    except TranscodeError as e:
        raise _refuse(web.HTTPBadRequest, f"could not convert {filename_raw}: {e}") from None
    del ext  # the stored name is always .aac
    filename = f"sound_{sound_id}.aac"
    filepath = os.path.join(d_dir, filename)
    with open(filepath, "wb") as f:
        f.write(adts)

    entry: dict[str, Any] = {
        "id": sound_id,
        "name": os.path.splitext(filename_raw)[0],
        "filename": filename,
        **describe(adts),
        "uploaded_at": int(time.time()),
    }
    sounds.append(entry)
    _save_sounds(request, d.petkit_id, sounds)

    base_url = sound_download_base(request.app["cfg"], request.host)
    if base_url:
        sound_list = sound_list_for_device(request, d.petkit_id, base_url)
        hub, bridge = _hub_and_bridge(request)
        if hub and bridge:
            envelope = make_mqtt_property_set({"soundList": sound_list})
            await _deliver(hub, bridge, d, "property/set", envelope)

    log.info("Uploaded sound %d for device %d: %s (%d bytes in, %d bytes ADTS, %d s)",
             sound_id, d.petkit_id, filename, len(data), entry["size"], entry["duration"])
    return web.json_response({"ok": True, "sound": entry})


async def api_sounds_delete(request: web.Request) -> web.Response:
    d = _device_or_404(request)
    try:
        sound_id = int(request.match_info["sound_id"])
    except (ValueError, KeyError):
        raise _refuse(web.HTTPBadRequest, "bad sound_id") from None

    sounds = _load_sounds(request, d.petkit_id)
    entry = next((s for s in sounds if s["id"] == sound_id), None)
    if entry is None:
        raise _refuse(web.HTTPNotFound, "sound not found")

    filepath = os.path.join(_sounds_dir(request, d.petkit_id), entry["filename"])
    if os.path.isfile(filepath):
        os.remove(filepath)

    sounds = [s for s in sounds if s["id"] != sound_id]
    _save_sounds(request, d.petkit_id, sounds)

    log.info("Deleted sound %d for device %d", sound_id, d.petkit_id)
    return web.json_response({"ok": True})


async def api_sounds_play(request: web.Request) -> web.Response:
    d = _device_or_404(request)
    try:
        sound_id = int(request.match_info["sound_id"])
    except (ValueError, KeyError):
        raise _refuse(web.HTTPBadRequest, "bad sound_id") from None

    hub, bridge = _hub_and_bridge(request)
    if not hub:
        raise _refuse(web.HTTPBadRequest, "no event hub")

    envelope = _envelope("thing.service.play_sound", {"soundId": sound_id})
    return await _deliver(hub, bridge, d, "play_sound", envelope)


async def api_sounds_select(request: web.Request) -> web.Response:
    d = _device_or_404(request)
    try:
        sound_id = int(request.match_info["sound_id"])
    except (ValueError, KeyError):
        raise _refuse(web.HTTPBadRequest, "bad sound_id") from None

    d.config.setdefault("settings", {})["selectedSound"] = sound_id

    hub, bridge = _hub_and_bridge(request)
    if not hub:
        raise _refuse(web.HTTPBadRequest, "no event hub")

    envelope = make_mqtt_property_set({"selectedSound": sound_id})
    return await _deliver(hub, bridge, d, "property/set", envelope)
