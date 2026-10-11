"""`/opt/user.conf` of the Axera-generation devices: read, point, write.

Format, observed on a YumShare Dual-Hopper 2 (fw 895) and confirmed by
decrypting the file and re-encrypting it to an identical layout:

    md5hex(MAGIC + IV + CT)  32 bytes, ASCII hex
    MAGIC                     8 bytes, 55 aa f1 e2 d3 c4 b5 a6
    IV                       16 bytes
    CT                        AES-256-CBC, PKCS#7, of the JSON document

The key is the literal string the firmware's `ble` binary carries. The JSON
holds `serverInfo.apiServers` and `serverInfo.ipServers`, the lists the
device dials; the first entry of each is what it uses.
"""
from __future__ import annotations

import hashlib
import json
import os
from typing import Any

from cryptography.hazmat.primitives.ciphers import Cipher, algorithms, modes

KEY = b"petkitRootfs_Aes_Encrypt_Keys_32"
MAGIC = bytes.fromhex("55aaf1e2d3c4b5a6")


class UserConfError(ValueError):
    """The bytes are not a user.conf this module understands."""


def decrypt(blob: bytes) -> bytes:
    """The JSON document inside `blob`, or `UserConfError` naming why not."""
    if len(blob) < 32 + 8 + 16 + 16:
        raise UserConfError("file too short to be a user.conf")
    if blob[32:40] != MAGIC:
        raise UserConfError("magic marker missing: not an encrypted user.conf")
    if hashlib.md5(blob[32:]).hexdigest().encode() != blob[:32]:
        raise UserConfError("checksum field does not match the file")
    iv, ct = blob[40:56], blob[56:]
    if len(ct) % 16:
        raise UserConfError("ciphertext is not a whole number of blocks")
    pt = Cipher(algorithms.AES(KEY), modes.CBC(iv)).decryptor().update(ct)
    pad = pt[-1] if pt else 0
    if not 1 <= pad <= 16 or not pt.endswith(bytes([pad]) * pad):
        raise UserConfError("decryption produced no valid padding: wrong key or damaged file")
    return pt[:-pad]


def encrypt(raw: bytes, iv: bytes | None = None) -> bytes:
    """`raw` as the firmware writes it. `iv` is for tests; random otherwise."""
    iv = iv or os.urandom(16)
    padn = 16 - len(raw) % 16
    ct = Cipher(algorithms.AES(KEY), modes.CBC(iv)).encryptor().update(raw + bytes([padn]) * padn)
    return hashlib.md5(MAGIC + iv + ct).hexdigest().encode() + MAGIC + iv + ct


def load(blob: bytes) -> dict[str, Any]:
    """The decrypted document as a dict, or `UserConfError`."""
    try:
        doc = json.loads(decrypt(blob).decode("utf-8"))
    except (UnicodeDecodeError, json.JSONDecodeError) as e:
        raise UserConfError(f"decrypted content is not JSON: {e}") from None
    if not isinstance(doc, dict) or not isinstance(doc.get("serverInfo"), dict):
        raise UserConfError("no serverInfo block: not a device config this path knows")
    return doc


def point_at_server(doc: dict[str, Any], api_url: str) -> list[str]:
    """Point `serverInfo` at `api_url` in place; returns what changed.

    The first `apiServers` entry is what the device dials; `ipServers` is its
    fallback list and gets the same address when it has entries, as the
    manual run that this automates did. Everything else is left exactly as
    the device wrote it.
    """
    info = doc["serverInfo"]
    changes = []
    for key in ("apiServers", "ipServers"):
        lst = info.get(key)
        if not isinstance(lst, list):
            continue
        if not lst:
            if key == "apiServers":
                lst.append(api_url)
                changes.append(f"{key}: [] -> {api_url}")
            continue
        if lst[0] != api_url:
            changes.append(f"{key}[0]: {lst[0]} -> {api_url}")
            lst[0] = api_url
    return changes


def dump(doc: dict[str, Any]) -> bytes:
    """The document as the firmware's cJSON printer would accept it back."""
    return json.dumps(doc, ensure_ascii=False, indent=1).encode("utf-8")
