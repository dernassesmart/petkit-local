"""Custom feeding sounds in the one format the feeder can play.

A camera feeder downloads an uploaded sound from the bucket and keeps it as
`/opt/user_feed_over_<id>.aac` (the name is the firmware's, `ctrl` strings:
`%s%s_%d.aac`, `rm /opt/user_feed_over*.aac`), then plays it through the same
ADTS parser as its own voice prompts. Those prompts are ADTS AAC-LC, 16 kHz,
mono, made with ffmpeg -- so that is the only format a download can be. An
.m4a from a phone or an .mp3 is downloaded without complaint and fails
silently at playback. Every upload is therefore transcoded here, and the
duration the device is handed in `dev_sound_get` is read from the result.
"""
from __future__ import annotations

import hashlib
import json
import logging
import os
import tempfile

from petkit_local.media.transcode import have_ffmpeg, run_ffmpeg

log = logging.getLogger(__name__)

#: A transcode of a two-megabyte upload is seconds; this bounds a hung child.
SOUND_TIMEOUT = 120.0

ADTS_SAMPLE_RATES = (96000, 88200, 64000, 48000, 44100, 32000, 24000, 22050,
                     16000, 12000, 11025, 8000, 7350)


class TranscodeError(RuntimeError):
    """ffmpeg could not turn the upload into ADTS AAC (or is not installed)."""


def is_adts(data: bytes) -> bool:
    """Whether `data` starts with an ADTS frame: 12 sync bits, then a header
    whose frame length fits in the buffer."""
    if len(data) < 7 or data[0] != 0xFF or (data[1] & 0xF0) != 0xF0:
        return False
    length = ((data[3] & 0x03) << 11) | (data[4] << 3) | (data[5] >> 5)
    return 7 <= length <= len(data)


def adts_duration_seconds(data: bytes) -> float:
    """Frames x 1024 samples / sample rate, walked frame by frame."""
    i, frames, rate = 0, 0, 0
    while i + 7 <= len(data):
        if data[i] != 0xFF or (data[i + 1] & 0xF0) != 0xF0:
            i += 1
            continue
        rate = ADTS_SAMPLE_RATES[(data[i + 2] >> 2) & 0x0F] if ((data[i + 2] >> 2) & 0x0F) < len(ADTS_SAMPLE_RATES) else 0
        length = ((data[i + 3] & 0x03) << 11) | (data[i + 4] << 3) | (data[i + 5] >> 5)
        if length < 7:
            break
        frames += 1
        i += length
    return frames * 1024 / rate if rate else 0.0


async def transcode_to_adts(data: bytes, workdir: str | None = None) -> bytes:
    """`data` (any container/codec ffmpeg reads) as ADTS AAC-LC 16 kHz mono.

    The input goes through a temp file, not a pipe: an .m4a whose `moov` atom
    sits at the end (most phone recordings) cannot be parsed from a
    non-seekable stdin. The output is streamed back on stdout, ADTS being
    exactly the kind of format that can be.
    """
    if not have_ffmpeg():
        raise TranscodeError("ffmpeg is not installed in this image")
    fd, src = tempfile.mkstemp(prefix="sound_in_", dir=workdir)
    try:
        with os.fdopen(fd, "wb") as f:
            f.write(data)
        rc, out, err = await run_ffmpeg(
            ["ffmpeg", "-hide_banner", "-loglevel", "error", "-i", src, "-vn",
             "-ar", "16000", "-ac", "1", "-c:a", "aac", "-b:a", "48k",
             "-f", "adts", "pipe:1"],
            timeout=SOUND_TIMEOUT, what="sound upload")
    finally:
        try:
            os.remove(src)
        except OSError:
            pass
    if rc != 0 or not is_adts(out):
        why = err.decode("utf-8", "replace").strip().splitlines()
        raise TranscodeError(why[-1][:200] if why else f"ffmpeg exit {rc}")
    return out


def describe(adts: bytes) -> dict:
    """The entry fields that depend on the file: size, digest, duration."""
    return {
        "size": len(adts),
        "digest": hashlib.md5(adts).hexdigest(),
        "duration": int(round(adts_duration_seconds(adts))),
    }


async def migrate_sounds(data_dir: str) -> int:
    """Convert sounds uploaded before 2.1.21 (stored as received) to ADTS.

    Walks `{data_dir}/sounds/<device>/sounds.json`; an entry whose file is not
    ADTS is transcoded in place under a `.aac` name and its size, digest and
    duration rewritten. The device then sees a new digest in `dev_sound_get`
    and downloads the playable file. Returns how many were converted; a file
    that cannot be converted is left as it was and logged.
    """
    root = os.path.join(data_dir, "sounds")
    if not os.path.isdir(root):
        return 0
    converted = 0
    for device_id in os.listdir(root):
        meta = os.path.join(root, device_id, "sounds.json")
        if not os.path.isfile(meta):
            continue
        try:
            with open(meta, encoding="utf-8") as f:
                sounds = json.load(f)
        except (OSError, ValueError):
            continue
        changed = False
        for entry in sounds:
            path = os.path.join(root, device_id, str(entry.get("filename", "")))
            if not os.path.isfile(path):
                continue
            with open(path, "rb") as f:
                data = f.read()
            if is_adts(data):
                if not entry.get("duration"):
                    entry["duration"] = int(round(adts_duration_seconds(data)))
                    changed = True
                continue
            try:
                adts = await transcode_to_adts(data, workdir=os.path.join(root, device_id))
            except TranscodeError as e:
                log.warning("Sound %s of device %s stays unconverted (%s); re-upload it",
                            entry.get("name"), device_id, e)
                continue
            new_name = f"sound_{entry['id']}.aac"
            with open(os.path.join(root, device_id, new_name), "wb") as f:
                f.write(adts)
            if new_name != entry["filename"]:
                try:
                    os.remove(path)
                except OSError:
                    pass
            entry["filename"] = new_name
            entry.update(describe(adts))
            converted += 1
            changed = True
            log.info("Converted sound %s of device %s to ADTS AAC (%d s)",
                     entry.get("name"), device_id, entry["duration"])
        if changed:
            with open(meta, "w", encoding="utf-8") as f:
                json.dump(sounds, f, indent=2)
    return converted
