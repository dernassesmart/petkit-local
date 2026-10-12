"""Media pipeline: takes one `dev_upload_file_info_v2` entry, locates the raw
upload it describes (see http/bucket.py's hidden `.raw` staging dir),
decrypts it if needed, remuxes video to `.mp4`, and moves it to the friendly
`/media/petkit/{Device}/{Role}/...` tree — then records the result in the
EventStore and notifies HA/the panel.

Runs off the request (the caller fire-and-forgets this as a background task)
so `dev_upload_file_info_v2` itself returns fast.
"""
from __future__ import annotations

import asyncio
import logging
import os
from typing import TYPE_CHECKING
from urllib.parse import urlparse

from petkit_local.events import ingest
from petkit_local.events.normalize import event_name_for
from petkit_local.media import crypto, layout, transcode
from petkit_local.utils.jsonio import read_bytes

if TYPE_CHECKING:
    from petkit_local.ai.pets import PetRegistry
    from petkit_local.devices.base import Device
    from petkit_local.events.store import EventStore
    from petkit_local.ha.publisher import HAPublisher
    from petkit_local.web.hub import EventHub

log = logging.getLogger(__name__)


_HEAD_BYTES = 400


def _locate_raw_file(raw_root: str, info: dict) -> str | None:
    """Find the raw upload matching this file_info entry. Primary: the path
    component of file_info's `fileUrl` (the PAR URL the device PUT to) —
    exact and cheap, and matches the per-device/per-capability `pathPrefix`
    from devices/payloads.py::to_oss_sts. Fallback: a recursive filename
    search for `fileId` anywhere under RAW, in case a device names the
    object differently than fileUrl implies.

    `fileUrl` is attacker/device-controlled (any client can POST
    dev_upload_file_info_v2), so its path component is resolved and checked
    against `raw_root` before use — same containment check as
    web/api/media.py::_safe_media_path — to rule out a `../` escape that would
    otherwise let a crafted request read (and then delete, see
    process_file_info) an arbitrary file the process can access. The
    boundary check resolves symlinks (`realpath`) but the *returned* path is
    the plain joined one, so callers keep getting the path they expect
    (realpath can rewrite unrelated leading segments, e.g. macOS's
    `/var` -> `/private/var`, which would otherwise change behavior for
    perfectly legitimate, contained paths too)."""
    root = os.path.realpath(raw_root)

    file_url = info.get("fileUrl") or info.get("file_url") or ""
    if file_url:
        path = urlparse(file_url).path.lstrip("/")
        if path:
            joined = os.path.join(raw_root, path)
            resolved = os.path.realpath(joined)
            if (resolved == root or resolved.startswith(root + os.sep)) and os.path.isfile(joined):
                return joined

    file_id = str(info.get("fileId") or info.get("file_id") or "")
    if not file_id:
        return None
    # `file_id` is device-supplied and validated only as non-empty, and a wrong
    # match here does not merely mis-file: the located file is processed under
    # THIS entry's metadata and then deleted. A bare substring scan is therefore
    # not good enough — an id of "1" would consume an unrelated pending upload.
    #
    # Exact name or stem wins outright: it cannot collide by accident whatever
    # its length. The substring fallback survives for a device that decorated
    # the name, but only when it identifies exactly ONE file; two candidates
    # means the id does not identify anything and guessing would destroy the
    # loser.
    partial: list[str] = []
    for cur_root, _dirs, files in os.walk(raw_root):
        for fname in files:
            path = os.path.join(cur_root, fname)
            if fname == file_id or os.path.splitext(fname)[0] == file_id:
                return path
            if file_id in fname:
                partial.append(path)
    if len(partial) == 1:
        return partial[0]
    if partial:
        log.warning("fileId %r matches %d staged uploads - refusing to guess between them",
                    file_id, len(partial))
    return None


def _claim_unique_path(path: str) -> str:
    """Atomically reserve an unused filename, adding " (2)", " (3)", … on
    collision, and return it.

    The friendly name is derived from a chunk's timestamp + label, which is
    NOT guaranteed unique: two uploads in the same second (or, before
    categories were split, the main stream and the substream) produce the
    same name. Uploads are processed as concurrent fire-and-forget tasks, so
    two of them writing that one name interleave and leave a physically
    corrupt file — observed on a real device as an MP4 containing two moov
    atoms, which then broke stitching. `O_CREAT | O_EXCL` makes claiming the
    name a single atomic step, so a racing task is forced onto the next one.
    """
    os.makedirs(os.path.dirname(path), exist_ok=True)
    base, ext = os.path.splitext(path)
    candidate, n = path, 1
    while True:
        try:
            fd = os.open(candidate, os.O_CREAT | os.O_EXCL | os.O_WRONLY, 0o644)
        except FileExistsError:
            n += 1
            candidate = f"{base} ({n}){ext}"
            continue
        os.close(fd)
        return candidate


def _remove_quietly(path: str) -> None:
    """Unlink, ignoring a file that is already gone (blocking)."""
    try:
        os.remove(path)
    except OSError:
        pass


# Whole-file writes: a clip is a few MB, so this is real blocking I/O and every
# caller hands it to `asyncio.to_thread`.
def _write_bytes(path: str, data: bytes) -> None:
    """Write a whole file, creating its parent directories (blocking — call
    via a thread)."""
    os.makedirs(os.path.dirname(path), exist_ok=True)
    with open(path, "wb") as f:
        f.write(data)


def _resolve_plaintext(data: bytes, row: dict, config: dict, src: str) -> bytes:
    """Decrypt `data` if it looks like it needs it, verifying the result via
    magic bytes rather than trusting the `encrypt` flag either way. Never
    raises — a bad key/IV assumption falls back to the raw bytes so the
    request can't crash, it just produces an unplayable file (logged once)."""
    if crypto.looks_plaintext(data[:_HEAD_BYTES]):
        return data
    if not row.get("encrypted"):
        log.warning("Raw upload %s doesn't look like plaintext media and isn't "
                    "flagged encrypted - keeping as-is", src)
        return data

    key = crypto.resolve_key(config)
    try:
        candidate = crypto.decrypt_aes(data, key, row.get("aes_iv") or "")
    except ValueError as e:
        log.warning("Decrypt failed for %s (%s) - keeping raw ciphertext", src, e)
        return data

    if crypto.looks_plaintext(candidate[:_HEAD_BYTES]):
        return candidate
    log.warning("Decrypted %s doesn't look like valid media - keeping raw ciphertext "
                "(AES key/IV assumption may be wrong, see CLAUDE.md)", src)
    return data


def _is_video(row: dict, src: str) -> bool:
    """Whether this upload should be remuxed to `.mp4` rather than stored as-is.

    The device's `fileType` is trusted when it says anything at all; the
    filename extension is only a fallback, because a wrong answer here is
    expensive (an image handed to ffmpeg, or a clip saved as `.jpg` and
    unplayable in the media browser).
    """
    file_type = (row.get("file_type") or "").lower()
    if "video" in file_type:
        return True
    if "image" in file_type or "photo" in file_type:
        return False
    return src.lower().endswith((".ts", ".mp4", ".m4v"))


# Photo galleries that arrive as a burst under one episode and want an index
# in the filename to keep them distinguishable/ordered.
_GALLERY_CATEGORIES = (ingest.CATEGORY_WASTE_CHECK, ingest.CATEGORY_HEALTH)


async def _gallery_index(store: EventStore, row: dict) -> int | None:
    """The 1-based arrival position of this photo within its episode's
    gallery. NOT a "(n of m)" — the total isn't known at upload time (photos
    stream in), so only the stable index goes in the filename; the true total
    is computed at display time (web/panel.py). Called after `row` is already
    upserted, so it's in its own sibling list."""
    category = row.get("category")
    if category not in _GALLERY_CATEGORIES or not row.get("related_event"):
        return None
    siblings = sorted(
        (m for m in await store.media_for_related_event(row["related_event"])
         if m.get("category") == category),
        key=lambda m: m.get("created_at") or 0,
    )
    ids = [s["file_id"] for s in siblings]
    if row["file_id"] not in ids:
        ids.append(row["file_id"])
    return ids.index(row["file_id"]) + 1


async def process_file_info(device: Device, info: dict, config: dict, store: EventStore,
                            hub: EventHub | None = None,
                            ha_publisher: HAPublisher | None = None,
                            pet_registry: PetRegistry | None = None) -> str | None:
    """Turn one raw upload into a playable file in the friendly media tree.

    Locate -> decrypt -> remux -> name -> move -> record. Returns the final
    path, or None when the file was deliberately skipped (capability disabled)
    or could not be processed (raw upload missing/unreadable); the `media` row
    is updated to `skipped`/`missing`/`error`/`ready` either way, so the panel
    can always say what happened to a `fileId`.

    `hub`, `ha_publisher` and `pet_registry` are pure side effects (panel
    event, HA media-ready entities, pet name in the filename) and are omitted
    wherever those aren't wired up.

    Raises:
        ValueError: `info` carries no `fileId`, so nothing can be recorded
            against it. This is the one failure NOT swallowed into a `media`
            row, because there is no row to write; the caller
            (http/handlers/upload_file_info.py) already rejects such entries
            before dispatching, and its task wrapper logs anything else that
            escapes — this runs detached, where an exception is otherwise
            invisible.
    """
    raw_root = config.get("media_raw_root", "")
    media_root = config.get("media_root", "")

    row = ingest.from_file_info(device, info)
    await store.upsert_media(row)

    # Several media roles share one negotiated STS capability (the substream
    # rides on fullVideo, the waste gallery on eventImage), so resolve the
    # role to its capability instead of assuming the category IS one — a role
    # with no capability simply isn't gated.
    capability = ingest.capability_for_category(row["category"])
    if device.is_camera and capability and capability not in device.enabled_capabilities():
        log.info("Skipping %s: capability %r disabled for device %d",
                 row["file_id"], row["category"], device.petkit_id)
        await store.upsert_media({"file_id": row["file_id"], "status": "skipped"})
        return None

    # Threaded like every other I/O step in this pipeline (see the module
    # header): the fallback path walks the whole `.raw` tree, once per uploaded
    # chunk, and a single visit is 20+ chunks. On the loop that stalled the HTTP
    # server, the MQTT bridge and the broker for the duration, and got worse as
    # `.raw` accumulated.
    src = await asyncio.to_thread(_locate_raw_file, raw_root, info)
    if not src:
        log.warning("file_info for device %d: no matching raw upload found (fileId=%s)",
                   device.petkit_id, row["file_id"])
        await store.upsert_media({"file_id": row["file_id"], "status": "missing"})
        return None

    try:
        data = await asyncio.to_thread(read_bytes, src)
    except OSError as e:
        log.warning("Could not read raw upload %s: %s", src, e)
        await store.upsert_media({"file_id": row["file_id"], "status": "error"})
        return None

    plain = _resolve_plaintext(data, row, config, src)
    is_video = _is_video(row, src)

    pet_name = None
    if pet_registry is not None and row.get("related_event"):
        pet = await pet_registry.pet_for_related_event(row["related_event"])
        if pet:
            pet_name = pet.get("name")

    flags = {k: info.get(k) for k in ("petEvent", "toiletEvent", "cleanEvent", "cvrEvent")}
    paired = await store.event_by_related(row["related_event"]) if row.get("related_event") else None
    event_kind = None
    if not any(flags.values()) and paired:
        event_kind = paired.get("event_kind")
    label = layout.media_filename_label(flags, event_kind)
    index = await _gallery_index(store, row)

    ext = "mp4" if is_video else "jpg"
    friendly = layout.build_media_path(
        media_root, device.device_type, device.petkit_id, row["category"],
        row.get("start_ts"), label, ext, pet_name=pet_name, index=index,
    )
    # Reserve the name before writing — concurrent uploads can compute the
    # same one, and two writers on one path corrupt the file (see
    # `_claim_unique_path`).
    friendly = await asyncio.to_thread(_claim_unique_path, friendly)

    if is_video:
        tmp_path = friendly + ".raw.ts"
        await asyncio.to_thread(_write_bytes, tmp_path, plain)
        # ffmpeg overwrites the placeholder we just claimed (-y).
        ok = await transcode.remux_ts_to_mp4(tmp_path, friendly)
        if ok:
            await asyncio.to_thread(os.remove, tmp_path)
        else:
            fallback = friendly[: -len(".mp4")] + ".ts"
            await asyncio.to_thread(os.replace, tmp_path, fallback)
            await asyncio.to_thread(_remove_quietly, friendly)  # drop the empty placeholder
            friendly = fallback
    else:
        await asyncio.to_thread(_write_bytes, friendly, plain)

    size = await asyncio.to_thread(os.path.getsize, friendly)
    await store.upsert_media({
        "file_id": row["file_id"], "media_path": friendly, "status": "ready",
        "size_bytes": size,
    })

    try:
        await asyncio.to_thread(os.remove, src)
    except OSError as e:
        log.warning("Could not remove raw upload %s after processing: %s", src, e)

    if hub is not None:
        hub.publish("media", device.petkit_id, f"{row['category'] or 'file'} ready: {os.path.basename(friendly)}",
                    detail={"path": friendly, "category": row["category"]})

    if ha_publisher is not None:
        media = await store.get_media_by_file_id(row["file_id"])
        if media is not None and paired:
            # The event this file belongs to, by its MQTT-style name
            # (`eat_start`, ...): the publisher keeps a separate "last eating"
            # snapshot, and the row itself carries only the device's code.
            media["event_name"] = event_name_for(paired.get("event_type"), device.device_type)
        await ha_publisher.publish_media_ready(device, media)

    return friendly
