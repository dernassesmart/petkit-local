"""`POST /api/provision/telnet` — point a Linux device here over its root shell.

Synchronous on purpose: the whole run is a few seconds of shell traffic and
crypto, well inside one request, and the only slow part -- the device coming
back after its reboot -- is watched by the browser the same way the Bluetooth
path watches for a new signup. The password travels in the request body
only and is never logged.
"""
from __future__ import annotations

import logging

from aiohttp import web

from petkit_local.provision.telnet_flow import ProvisionError, provision_via_telnet
from petkit_local.web.api._common import _json_body, _refuse

log = logging.getLogger(__name__)

DEFAULT_USER = "root"


async def api_provision_telnet(request: web.Request) -> web.Response:
    body = await _json_body(request)
    host = str(body.get("host") or "").strip()
    user = str(body.get("user") or DEFAULT_USER).strip() or DEFAULT_USER
    password = str(body.get("password") or "")
    api_url = str(body.get("server") or "").strip() or str(request.app["cfg"].get("api_url", "")).strip()
    reboot = bool(body.get("reboot", True))
    if not host:
        raise _refuse(web.HTTPBadRequest, "device address is required")
    if not api_url.startswith(("http://", "https://")):
        raise _refuse(web.HTTPBadRequest, "apiServers URL must start with http:// or https://")
    if ".local" in api_url:
        raise _refuse(web.HTTPBadRequest, "the device cannot resolve .local names; use an IP address")
    lines: list[str] = []
    data_dir = request.app["cfg"].get("data_dir", "/data")
    try:
        result = await provision_via_telnet(host, user, password, api_url, data_dir=data_dir,
                                            reboot=reboot, log=lines.append)
    except ProvisionError as e:
        lines.append(f"FAILED: {e}")
        log.warning("Telnet provisioning of %s failed: %s", host, e)
        return web.json_response({"ok": False, "error": str(e), "log": lines}, status=400)
    log.info("Telnet provisioning of %s (serial %s): %s", host, result["serial"],
             "; ".join(result["changes"]) or "no change")
    return web.json_response({"ok": True, "log": lines, **result})
