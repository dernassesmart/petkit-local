#!/usr/bin/env python3
"""Look at (and drive) the PetKit Local app on a Home Assistant host.

Logs in as a Home Assistant user through the login flow, then talks to the
Supervisor through HA's own proxy (`/api/hassio/...`), which an admin user may
use. Password comes from the environment, never from a file.

  set HA_URL=http://192.168.192.134:8123  HA_USER=claude  HA_PASS=...
  python ha_addon.py status            # version, state, update entity
  python ha_addon.py log [lines]       # the app's log (last N lines, default 80)
  python ha_addon.py update            # install the available update
  python ha_addon.py restart
  python ha_addon.py panel-log         # the panel's own request log (via Ingress)
"""
import json
import os
import sys
import urllib.error
import urllib.parse
import urllib.request

sys.stdout.reconfigure(encoding="utf-8")

BASE = os.environ.get("HA_URL", "http://192.168.192.134:8123").rstrip("/")
USER = os.environ.get("HA_USER", "claude")
# `HA_PASS_B64` exists for shells that mangle non-ASCII characters on the way
# into the environment (Git Bash on Windows): base64 of the UTF-8 password.
PW = os.environ.get("HA_PASS", "")
if not PW and os.environ.get("HA_PASS_B64"):
    import base64

    PW = base64.b64decode(os.environ["HA_PASS_B64"]).decode("utf-8")
CLIENT = BASE + "/"


def req(method, path, data=None, token=None, raw=False, text=False):
    headers = {"Content-Type": "application/x-www-form-urlencoded" if raw else "application/json"}
    if token:
        headers["Authorization"] = "Bearer " + token
    body = data if raw else (json.dumps(data).encode("utf-8") if data is not None else None)
    r = urllib.request.Request(BASE + path, data=body, method=method, headers=headers)
    try:
        with urllib.request.urlopen(r, timeout=60) as resp:
            payload = resp.read()
    except urllib.error.HTTPError as e:
        payload = e.read()
        return {"_error": e.code, "_body": payload.decode("utf-8", "replace")[:800]}
    if text:
        return payload.decode("utf-8", "replace")
    try:
        return json.loads(payload.decode("utf-8"))
    except ValueError:
        return {"_body": payload.decode("utf-8", "replace")[:800]}


def login():
    if not PW:
        sys.exit("HA_PASS fehlt")
    f = req("POST", "/auth/login_flow", {"client_id": CLIENT, "handler": ["homeassistant", None], "redirect_uri": CLIENT})
    r = req("POST", "/auth/login_flow/%s" % f["flow_id"], {"username": USER, "password": PW, "client_id": CLIENT})
    if r.get("type") != "create_entry":
        sys.exit("Login fehlgeschlagen: %s" % {k: r.get(k) for k in ("type", "errors", "_error", "_body")})
    body = "grant_type=authorization_code&code=%s&client_id=%s" % (r["result"], urllib.parse.quote(CLIENT, safe=""))
    t = req("POST", "/auth/token", body.encode(), raw=True)
    return t["access_token"]


def find_addon(tok):
    info = req("GET", "/api/hassio/addons", token=tok)
    if "_error" in info:
        sys.exit("Supervisor-API nicht erreichbar: %s" % info)
    addons = info.get("data", {}).get("addons", [])
    hits = [a for a in addons if "petkit" in (a.get("slug", "") + a.get("name", "")).lower()]
    return hits


def cmd_status(tok):
    for a in find_addon(tok):
        print("App: %s | slug %s | installiert %s | verfügbar %s | Zustand %s | Update %s | Repo %s" % (
            a.get("name"), a.get("slug"), a.get("version"), a.get("version_latest"),
            a.get("state"), a.get("update_available"), a.get("repository")))
    states = req("GET", "/api/states", token=tok)
    for s in states if isinstance(states, list) else []:
        if s["entity_id"].startswith("update.") and "petkit" in s["entity_id"]:
            at = s.get("attributes", {})
            print("Update-Entität: %s = %s | installed %s | latest %s" % (
                s["entity_id"], s["state"], at.get("installed_version"), at.get("latest_version")))


def cmd_log(tok, lines=80):
    for a in find_addon(tok):
        print("===== Log %s (%s) =====" % (a.get("name"), a.get("slug")))
        log = req("GET", "/api/hassio/addons/%s/logs" % a["slug"], token=tok, text=True)
        if isinstance(log, dict):
            print(log)
            continue
        out = log.splitlines()
        print("\n".join(out[-int(lines):]))


def cmd_update(tok):
    for a in find_addon(tok):
        print("Update %s: %s -> %s …" % (a.get("slug"), a.get("version"), a.get("version_latest")))
        r = req("POST", "/api/hassio/addons/%s/update" % a["slug"], {}, token=tok)
        print(r)


def cmd_restart(tok):
    for a in find_addon(tok):
        print("Neustart %s …" % a.get("slug"))
        print(req("POST", "/api/hassio/addons/%s/restart" % a["slug"], {}, token=tok))


def cmd_reload_store(tok):
    print(req("POST", "/api/hassio/store/reload", {}, token=tok))


def cmd_panel_log(tok):
    for a in find_addon(tok):
        info = req("GET", "/api/hassio/addons/%s/info" % a["slug"], token=tok)
        ingress = info.get("data", {}).get("ingress_url")
        print("Ingress-URL:", ingress)
        if not ingress:
            continue
        sess = req("POST", "/api/hassio/ingress/session", {}, token=tok)
        print("Ingress-Session:", {k: v for k, v in sess.items() if k != "data"} if isinstance(sess, dict) else sess)


def main():
    if len(sys.argv) < 2:
        sys.exit(__doc__)
    tok = login()
    print("Login ok")
    cmd = sys.argv[1]
    if cmd == "status":
        cmd_status(tok)
    elif cmd == "log":
        cmd_log(tok, sys.argv[2] if len(sys.argv) > 2 else 80)
    elif cmd == "update":
        cmd_update(tok)
    elif cmd == "restart":
        cmd_restart(tok)
    elif cmd == "reload":
        cmd_reload_store(tok)
    elif cmd == "panel-log":
        cmd_panel_log(tok)
    else:
        sys.exit("unbekanntes Kommando")


if __name__ == "__main__":
    main()
