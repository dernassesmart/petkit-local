"""The Provision tab's telnet path, step by step, with every write verified.

What a run does, in order (each step is a line in the log the panel shows):

 1. log in; identify the device: `armv7l` and `/opt/user.conf` present, or
    stop -- this layout is the one verified on a YumShare Dual-Hopper 2, and
    nothing is written to a device that looks different;
 2. fetch `/opt/user.conf` (base64 over the shell) and check its md5 against
    the device's own `md5sum`;
 3. decrypt, parse, keep a copy under the add-on's data directory;
 4. point `serverInfo` at this server, encrypt;
 5. upload as `/opt/user.conf.new`, verify its md5 on the device, back the
    original up as `/opt/user.conf.bak`, move the new file into place, sync,
    verify again;
 6. reboot, unless told not to.

The device then signs up here on its own; the panel watches for it.
"""
from __future__ import annotations

import base64
import hashlib
import json
import os
import re
import time
from collections.abc import Callable
from typing import Any

from petkit_local.provision import userconf
from petkit_local.provision.telnet import TelnetError, TelnetShell

SUPPORTED_ARCH = {"armv7l"}
CONF_PATH = "/opt/user.conf"
CHUNK = 512


class ProvisionError(RuntimeError):
    pass


def _md5(data: bytes) -> str:
    return hashlib.md5(data).hexdigest()


def _firmware_version(text: str) -> str:
    """`current.firmwareVer` out of `/opt/version`, which is a 32-hex checksum
    followed by a JSON document; "" when it does not look like that."""
    body = text.strip()
    if len(body) > 32 and body[:32].isalnum() and body[32] == "{":
        body = body[32:]
    try:
        return str(json.loads(body).get("current", {}).get("firmwareVer", "") or "")
    except (ValueError, AttributeError):
        return ""


async def provision_via_telnet(host: str, user: str, password: str, api_url: str, *,
                               data_dir: str, reboot: bool = True,
                               shell: TelnetShell | None = None,
                               log: Callable[[str], None] | None = None) -> dict[str, Any]:
    """Run the whole sequence; returns `{serial, changes, backup, rebooted}`.

    Raises `ProvisionError` with a one-line reason at the first check that
    fails. Up to the move in step 5 nothing on the device has changed; the
    move itself is the only non-idempotent moment, and the backup next to it
    is what undoes it.
    """
    say = log or (lambda _msg: None)
    sh = shell or TelnetShell(host)
    try:
        say(f"connecting to {host}:23 …")
        await sh.login(user, password)
        say("logged in")

        rc, arch = await sh.run("uname -m")
        arch = arch.strip()
        if rc != 0 or arch not in SUPPORTED_ARCH:
            raise ProvisionError(f"unsupported device: uname -m says {arch!r} "
                                 f"(this path is verified for {', '.join(sorted(SUPPORTED_ARCH))} only)")
        rc, version = await sh.run("cat /opt/version 2>/dev/null | head -c 600")
        say(f"device: {arch}" + (f", firmware {fw}" if (fw := _firmware_version(version)) else ""))
        rc, present = await sh.run(f"test -f {CONF_PATH} && echo yes")
        if "yes" not in present:
            raise ProvisionError(f"{CONF_PATH} not found on the device: not the layout this path knows")

        rc, remote_sum = await sh.run(f"md5sum {CONF_PATH}")
        remote_md5 = remote_sum.split()[0] if remote_sum.split() else ""
        rc, b64 = await sh.run(f"base64 {CONF_PATH}", timeout=60)
        blob = base64.b64decode("".join(b64.split()))
        if _md5(blob) != remote_md5:
            raise ProvisionError("the fetched user.conf does not match the device's own checksum; nothing written")
        say(f"fetched {CONF_PATH} ({len(blob)} bytes, md5 {remote_md5})")

        doc = userconf.load(blob)
        # `idInfo` carries the device id (`dev_id`) and its secret, not the
        # serial; the id is what the panel shows for the device anyway.
        serial = str((doc.get("idInfo") or {}).get("dev_id") or "").strip() or "unknown"
        backup_dir = os.path.join(data_dir, "provision", re.sub(r"[^A-Za-z0-9_.-]", "_", serial) + "-" + time.strftime("%Y%m%d-%H%M%S"))
        os.makedirs(backup_dir, exist_ok=True)
        with open(os.path.join(backup_dir, "user.conf.orig"), "wb") as f:
            f.write(blob)
        with open(os.path.join(backup_dir, "user.conf.orig.json"), "w", encoding="utf-8") as f:
            json.dump(doc, f, ensure_ascii=False, indent=1)
        say(f"decrypted; serial {serial}; backup under {backup_dir}")
        before = list((doc["serverInfo"].get("apiServers") or [])[:1])
        say(f"current apiServers: {before or '[]'}")

        changes = userconf.point_at_server(doc, api_url)
        if not changes:
            say(f"already pointing at {api_url}; nothing to change")
            return {"serial": serial, "changes": [], "backup": backup_dir, "rebooted": False}
        for c in changes:
            say("  " + c)
        new_blob = userconf.encrypt(userconf.dump(doc))
        if userconf.load(new_blob)["serverInfo"] != doc["serverInfo"]:
            raise ProvisionError("re-encryption check failed; nothing written")
        with open(os.path.join(backup_dir, "user.conf.new"), "wb") as f:
            f.write(new_blob)
        new_md5 = _md5(new_blob)

        say(f"uploading {len(new_blob)} bytes as {CONF_PATH}.new …")
        await sh.run("rm -f /tmp/pk_uc.b64")
        text = base64.b64encode(new_blob).decode("ascii")
        for i in range(0, len(text), CHUNK):
            rc, _ = await sh.run(f"echo '{text[i:i + CHUNK]}' >> /tmp/pk_uc.b64")
            if rc != 0:
                raise ProvisionError("writing the upload to /tmp failed")
        rc, _ = await sh.run(f"base64 -d /tmp/pk_uc.b64 > {CONF_PATH}.new && rm -f /tmp/pk_uc.b64")
        rc, got = await sh.run(f"md5sum {CONF_PATH}.new")
        got_md5 = got.split()[0] if got.split() else ""
        if got_md5 != new_md5:
            await sh.run(f"rm -f {CONF_PATH}.new")
            raise ProvisionError(f"upload checksum mismatch ({got_md5} != {new_md5}); the device's config was not touched")
        say("upload verified")

        rc, _ = await sh.run(f"cp {CONF_PATH} {CONF_PATH}.bak && mv {CONF_PATH}.new {CONF_PATH} && sync")
        if rc != 0:
            raise ProvisionError("could not move the new config into place; the original is still /opt/user.conf")
        rc, after = await sh.run(f"md5sum {CONF_PATH}")
        if (after.split()[0] if after.split() else "") != new_md5:
            raise ProvisionError(f"{CONF_PATH} does not carry the new checksum after the move; restore from {CONF_PATH}.bak")
        say(f"{CONF_PATH} replaced; original kept as {CONF_PATH}.bak")

        if reboot:
            say("rebooting the device …")
            try:
                await sh.run("reboot", timeout=5)
            except TelnetError:
                pass  # the connection dropping is the reboot
        return {"serial": serial, "changes": changes, "backup": backup_dir, "rebooted": reboot}
    except TelnetError as e:
        raise ProvisionError(str(e)) from None
    except userconf.UserConfError as e:
        raise ProvisionError(f"{CONF_PATH}: {e}; nothing written") from None
    finally:
        await sh.close()
