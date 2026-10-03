#!/usr/bin/env python3
"""Provision a PetKit ESP32 (BLUFI) device from a PC, with bleak.

This is the Provision tab's ESP32 path (provision.js) rewritten for the one
case the browser cannot handle: a device that wants an encrypted link. Chrome
on Windows does not pair, so such a device answers the panel's writes with
"GATT operation not permitted" / "GATT Error Unknown" (Windows: Write Not
Permitted on the CCCD, Access Denied on the data characteristic), while the
PetKit app on a phone pairs silently and gets through. bleak can pair.

The wire format and the key sequence are the panel's, byte for byte:
  110  who are you            -> id, sn, mac, firmware
  151  Wi-Fi + apiServers     -> {"state": 1} is the ack
  112  join status, polled    -> 7 or 10 means joined
  111  Wi-Fi details, once the device reports state 6
  114  language, 101 completion, after it has joined

Usage:
  python blufi_provision.py --scan
  python blufi_provision.py --address AA:BB:.. --identify [--pair]
  python blufi_provision.py --address AA:BB:.. --ssid NAME --password PW \
      --server http://192.168.1.2/6/ [--tz 2.0] [--locale Europe/Berlin] [--pair]
  python blufi_provision.py --address AA:BB:.. --unpair

Requires: pip install bleak   (Windows 10/11, Linux/BlueZ, macOS)
"""

import argparse
import asyncio
import json
import sys
import time
from datetime import datetime

from bleak import BleakClient, BleakScanner

BLUFI_SERVICE = "0000ffff-0000-1000-8000-00805f9b34fb"
BLUFI_P2E = "0000ff01-0000-1000-8000-00805f9b34fb"  # PC -> device, write
BLUFI_E2P = "0000ff02-0000-1000-8000-00805f9b34fb"  # device -> PC, notify

BLUFI_TYPE_DATA = 0x01
BLUFI_DATA_CUSTOM = 0x13
BLUFI_DATA_WIFI_REP = 0x0F
BLUFI_DATA_ERROR_INFO = 0x12
BLUFI_FC_FRAG = 0x10
BLUFI_FRAG_LEN = 12  # PetKit's ESP32 firmware takes custom data in 12-byte chunks

T_IDENT = 5.0
T_ACK = 10.0
T_JOIN = 120.0

JOIN_STATES = {
    0: "starting",
    1: "looking for the network",
    2: "connecting to the network",
    3: "the Wi-Fi password is wrong",
    4: "that Wi-Fi network was not found",
    5: "could not connect to the Wi-Fi",
    6: "on Wi-Fi, connecting to the server",
    7: "connected to the server",
    8: "could not connect to the server",
    9: "connecting to MQTT",
    10: "online",
}
JOIN_FAILED = (4, 5, 8)
JOIN_WARN = (3,)
JOIN_DONE = (7, 10)


def log(msg):
    print(datetime.now().strftime("%H:%M:%S"), msg, flush=True)


class WriteRefused(Exception):
    """A GATT write the device or the OS refused; already logged."""


# ----------------------------------------------------------------- framing
def blufi_frame(seq, data, frag, total_len):
    head = bytes([
        BLUFI_TYPE_DATA | (BLUFI_DATA_CUSTOM << 2),
        BLUFI_FC_FRAG if frag else 0x00,
        seq & 0xFF,
        len(data) + 2 if frag else len(data),
    ])
    body = (bytes([total_len & 0xFF, (total_len >> 8) & 0xFF]) + data) if frag else data
    return head + body


async def blufi_send(write, ctr, data):
    if len(data) <= BLUFI_FRAG_LEN:
        await write(blufi_frame(ctr["seq"], data, False, 0))
        ctr["seq"] += 1
        return
    for off in range(0, len(data), BLUFI_FRAG_LEN):
        chunk = data[off : off + BLUFI_FRAG_LEN]
        # total_len is what REMAINS including this chunk, which is what the
        # device reassembles against, not the length of the whole message.
        remaining = len(data) - off
        last = off + BLUFI_FRAG_LEN >= len(data)
        await write(blufi_frame(ctr["seq"], chunk, not last, remaining))
        ctr["seq"] += 1


def explain(b, ctx):
    """One notification from 0xFF02 -> log text; PetKit documents go to ctx."""
    if len(b) < 4:
        return "short frame (%d B)" % len(b)
    pkt_type = b[0] & 0x03
    subtype = b[0] >> 2
    fc = b[1]
    data = b[4 : 4 + b[3]]
    if pkt_type == BLUFI_TYPE_DATA and subtype == BLUFI_DATA_CUSTOM:
        if fc & BLUFI_FC_FRAG:
            ctx["frag"].append(data[2:])
            return "custom data, partial (%d B)" % (len(data) - 2)
        ctx["frag"].append(data)
        whole = b"".join(ctx["frag"])
        ctx["frag"].clear()
        try:
            msg = json.loads(whole.decode("utf-8", "replace"))
        except ValueError:
            return "custom data, not a PetKit document: %r" % whole
        if not isinstance(msg, dict) or "key" not in msg:
            return "custom data, not a PetKit document: %r" % whole
        ctx["replies"][msg["key"]] = msg.get("payload") or {}
        return "key %s %s" % (msg["key"], json.dumps(msg.get("payload") or {}))
    if pkt_type == BLUFI_TYPE_DATA and subtype == BLUFI_DATA_WIFI_REP:
        return "wifi status: " + ("CONNECTED" if len(data) > 1 and data[1] == 0 else "not connected")
    if pkt_type == BLUFI_TYPE_DATA and subtype == BLUFI_DATA_ERROR_INFO:
        return "BLUFI error report, code %d" % (data[0] if data else -1)
    return "ignored packet type %d subtype 0x%02x" % (pkt_type, subtype)


def join_state(payload):
    if not payload or "state" not in payload:
        return "never reported"
    base = JOIN_STATES.get(payload["state"], "state %s" % payload["state"])
    return base + (" (code %s)" % payload["code"] if "code" in payload else "")


# ----------------------------------------------------------------- actions
async def scan(seconds):
    seen = {}

    def cb(d, adv):
        name = d.name or adv.local_name or ""
        if name.lower().startswith("petkit"):
            seen[d.address] = (name, adv.rssi, list(adv.service_uuids))

    async with BleakScanner(cb):
        await asyncio.sleep(seconds)
    if not seen:
        log("no PetKit device seen in %ds. Hold its Wi-Fi button ~5 s until the light flashes fast." % seconds)
    for addr, (name, rssi, uuids) in seen.items():
        log("%s  %s  rssi %d  adv %s" % (addr, name, rssi, uuids))


async def pairing_info(client):
    try:
        pi = client._backend._requester.device_information.pairing  # Windows only
        return "is_paired=%s can_pair=%s" % (pi.is_paired, pi.can_pair)
    except Exception:
        return "(pairing state not readable on this platform)"


async def run(args):
    log("looking for %s …" % args.address)
    dev = await BleakScanner.find_device_by_address(args.address, timeout=args.scan_timeout)
    if dev is None:
        log("not found. Hold the device's Wi-Fi button ~5 s until the light flashes fast, then retry.")
        return 2

    ctx = {"frag": [], "replies": {}}

    def on_notify(_, data):
        log("device: " + explain(bytes(data), ctx))

    async with BleakClient(dev, timeout=20, winrt={"use_cached_services": False}) as client:
        log("connected, MTU %s, %s" % (client.mtu_size, await pairing_info(client)))

        if args.unpair:
            try:
                await client.unpair()
                log("unpaired")
            except Exception as e:
                log("unpair failed: %s: %s" % (type(e).__name__, e))
            return 0

        if args.pair:
            try:
                ok = await client.pair()
                log("pair() -> %s, now %s" % (ok, await pairing_info(client)))
            except Exception as e:
                log("pair() failed: %s: %s" % (type(e).__name__, e))
            if not client.is_connected:
                # A refused pairing takes the link down with it; one reconnect
                # so the writes below are tried on a live connection.
                log("the device dropped the connection during pairing — reconnecting…")
                try:
                    await client.connect()
                    log("reconnected, %s" % await pairing_info(client))
                except Exception as e:
                    log("reconnect failed: %s: %s" % (type(e).__name__, e))
                    return 8

        svc = client.services.get_service(BLUFI_SERVICE)
        if svc is None:
            log("no BLUFI service (0xFFFF) on this device; services: %s" % [s.uuid for s in client.services])
            return 3
        p2e = svc.get_characteristic(BLUFI_P2E)
        e2p = svc.get_characteristic(BLUFI_E2P)
        log("0xFF01 %s, 0xFF02 %s" % ("+".join(p2e.properties), "+".join(e2p.properties)))

        listening = False
        for attempt in (1, 2):
            try:
                await client.start_notify(e2p, on_notify)
                listening = True
                break
            except Exception as e:
                log("subscribing to 0xFF02 failed: %s: %s%s" % (type(e).__name__, e, " — retrying once…" if attempt == 1 else ""))
                await asyncio.sleep(1)
        if not listening:
            log("no notifications: the device's replies cannot be read. Continuing blind.")

        async def write(frame):
            await client.write_gatt_char(p2e, frame, response=True)

        ctr = {"seq": 0}

        async def send(obj):
            custom = json.dumps(obj, separators=(",", ":")).encode("utf-8")
            log("send: key %s custom data (%d bytes)" % (obj["key"], len(custom)))
            try:
                await blufi_send(write, ctr, custom)
            except Exception as e:
                log("write of key %s refused: %s: %s" % (obj["key"], type(e).__name__, e))
                raise WriteRefused()

        async def wait_for(key, seconds, ok=lambda p: True):
            end = time.monotonic() + seconds
            while time.monotonic() < end:
                p = ctx["replies"].get(key)
                if p is not None and ok(p):
                    return p
                await asyncio.sleep(0.25)
            return None

        # 110: identity
        await send({"key": 110})
        ident = await wait_for(110, 2.0)
        if ident is None and listening:
            log("no identity reply yet — retrying key 110 once…")
            await send({"key": 110})
            ident = await wait_for(110, T_IDENT)
        if ident is None:
            log("no identity reply." if listening else "sent blind; nothing can be read back.")
            if args.identify or not listening:
                return 0 if not listening else 4
        else:
            log("identity: %s" % json.dumps(ident))
        if args.identify:
            return 0

        # 151: credentials + server
        payload = {
            "ssid": args.ssid,
            "pwd": args.password,
            "hide": 1,
            "locale": args.locale,
            "timezone": "%.1f" % args.tz,
            "apiServers": [args.server],
            "ipServers": [args.server],
        }
        await send({"key": 151, "payload": payload})
        ack = await wait_for(151, T_ACK, lambda p: p.get("state") == 1 or p.get("state") in JOIN_FAILED)
        if ack is None:
            log("timed out waiting for the device to accept the credentials.")
            return 5
        if ack.get("state") in JOIN_FAILED:
            log("the device refused the credentials: " + join_state(ack))
            return 5
        log("credentials accepted — waiting for the device to join the network…")

        # 112: join status
        await send({"key": 112})
        shown = None
        asked_wifi = False
        end = time.monotonic() + T_JOIN
        while time.monotonic() < end and client.is_connected:
            await asyncio.sleep(1.0)
            st = ctx["replies"].get(112) or {}
            state = st.get("state")
            if state != shown:
                shown = state
                log("device: " + join_state(st))
                if state in JOIN_WARN:
                    log("warning: wrong Wi-Fi password reported; ESP32 has been seen to recover — still waiting.")
            if state in JOIN_FAILED:
                log("the device gave up: " + join_state(st))
                return 6
            if state in JOIN_DONE:
                await send({"key": 114, "payload": {"language": args.language}})
                log("device joined — sending completion (key 101)…")
                await send({"key": 101})
                log("done. The device should now call the add-on; check its Devices tab.")
                return 0
            if state == 6 and not asked_wifi:
                asked_wifi = True
                await send({"key": 111})
            else:
                await send({"key": 112})
        log("timed out waiting for the device to join (last status: %s)." % join_state(ctx["replies"].get(112)))
        return 7


def main():
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--scan", action="store_true", help="list PetKit devices advertising nearby and exit")
    ap.add_argument("--scan-timeout", type=float, default=15.0)
    ap.add_argument("--address", help="BLE address of the device (from --scan)")
    ap.add_argument("--identify", action="store_true", help="only ask the device who it is (key 110)")
    ap.add_argument("--pair", action="store_true", help="pair/bond with the device before talking to it")
    ap.add_argument("--unpair", action="store_true", help="remove the bond and exit")
    ap.add_argument("--ssid")
    ap.add_argument("--password")
    ap.add_argument("--server", help="apiServers URL, e.g. http://192.168.1.2/6/ (ESP32 wants port 80)")
    ap.add_argument("--tz", type=float, default=None, help="hours east of UTC, default: this PC's offset")
    ap.add_argument("--locale", default=None, help="time zone name, e.g. Europe/Berlin")
    ap.add_argument("--language", default="de_DE")
    args = ap.parse_args()

    if args.scan:
        asyncio.run(scan(args.scan_timeout))
        return 0
    if not args.address:
        ap.error("--address is required (use --scan to find it)")
    if not (args.identify or args.unpair):
        if not (args.ssid and args.password and args.server):
            ap.error("--ssid, --password and --server are required to provision")
    if args.tz is None:
        args.tz = -time.timezone / 3600 + (1 if time.localtime().tm_isdst > 0 else 0)
    if args.locale is None:
        try:
            from zoneinfo import ZoneInfo  # noqa: F401
            import tzlocal  # optional

            args.locale = tzlocal.get_localzone_name()
        except Exception:
            args.locale = "Europe/Berlin"
    try:
        return asyncio.run(run(args))
    except WriteRefused:
        log("stopped: the device does not accept writes on this link. Try --pair, "
            "and make sure the device is in Wi-Fi setup mode (light flashing fast).")
        return 9


if __name__ == "__main__":
    sys.exit(main())
