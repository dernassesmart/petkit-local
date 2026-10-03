"""One TLS port for two protocols: the device's HTTPS API calls and its MQTT.

The ESP32 models send part of their API traffic as HTTPS to port 443 — the
same port their MQTT session dials. Confirmed on a Pura X (T3, firmware 1.491,
upstream issue #35, where the decrypted first byte was `0x50`, a `POST`) and
seen on a Feeder D4 here with the exact log signature that issue describes:
amqtt alone on that port hands the `P` to an MQTT parser, logs "No data from
client … No more data", and closes. The device then never gets as far as
polling the heartbeat, so nothing queued for it is ever delivered, and after
`offline_timeout` it is shown unavailable.

This listener terminates TLS with the add-on's own certificate, looks at the
first byte the client sends, and hands the decrypted stream to the right
in-process server: `0x10` (MQTT CONNECT, the only packet a client may open a
session with) goes to the broker's plain listener, anything else to the HTTP
API. The device cannot tell the difference — it talks TLS to one port either
way — and amqtt keeps its own plain listener, so nothing about the broker
changes. Whether the device then ACCEPTS the certificate for MQTT is its own
decision (the ESP32 models reject a self-signed one, issue #35); the API path
is what makes the heartbeat work, and the heartbeat is the command channel
that needs no broker.

The HTTP side sees these requests arriving from 127.0.0.1; the handlers that
record a device's IP from `request.remote` skip the loopback address for that
reason, and this module logs the real peer once per connection.
"""
from __future__ import annotations

import asyncio
import logging
import ssl

log = logging.getLogger(__name__)

#: The first byte of an MQTT CONNECT packet: packet type 1 in the high nibble,
#: flags 0 in the low one (MQTT 3.1.1 §3.1.1 — the flags MUST be zero).
MQTT_CONNECT = 0x10

#: How long a client may stay silent after the handshake before the connection
#: is dropped. A device sends its request line or CONNECT immediately; a scanner
#: or a half-open socket does not.
FIRST_BYTE_TIMEOUT = 15.0
HANDSHAKE_TIMEOUT = 15.0


def device_ssl_context(certfile: str, keyfile: str) -> ssl.SSLContext:
    """A server context the device's mbedtls can complete a handshake with.

    Same settings as the broker's listener (`mqtt/broker.py`): the firmware
    offers RSA key-exchange suites only, which Python's default context
    excludes, so the security level is lowered and TLS 1.2 is the floor.
    """
    ctx = ssl.SSLContext(ssl.PROTOCOL_TLS_SERVER)
    ctx.load_cert_chain(certfile, keyfile)
    ctx.set_ciphers("DEFAULT:@SECLEVEL=0")
    ctx.minimum_version = ssl.TLSVersion.TLSv1_2
    return ctx


async def _pipe(reader: asyncio.StreamReader, writer: asyncio.StreamWriter) -> None:
    """Copy one direction until it ends, then close the other side's writer."""
    try:
        while True:
            data = await reader.read(65536)
            if not data:
                break
            writer.write(data)
            await writer.drain()
    except (ConnectionError, asyncio.IncompleteReadError, ssl.SSLError, OSError):
        pass
    finally:
        try:
            writer.close()
        except Exception:  # pragma: no cover - best effort on a dying socket
            pass


#: Every writer currently piped, both the device's and the in-process side's.
#: Shutdown closes them by hand: `Server.wait_closed()` on Python < 3.13 waits
#: for every connection to end on its own, and a device's MQTT session or a
#: long poll never does — the Supervisor would kill the container after 10 s
#: (exit 137) on each restart.
_LIVE: set[asyncio.StreamWriter] = set()


async def _handle(client_r: asyncio.StreamReader, client_w: asyncio.StreamWriter,
                  http_port: int, mqtt_port: int) -> None:
    _LIVE.add(client_w)
    try:
        await _route(client_r, client_w, http_port, mqtt_port)
    finally:
        _LIVE.discard(client_w)


async def _route(client_r: asyncio.StreamReader, client_w: asyncio.StreamWriter,
                 http_port: int, mqtt_port: int) -> None:
    peer = client_w.get_extra_info("peername")
    peer_ip = peer[0] if peer else "?"
    try:
        first = await asyncio.wait_for(client_r.read(1), FIRST_BYTE_TIMEOUT)
    except (asyncio.TimeoutError, ConnectionError, ssl.SSLError, OSError):
        first = b""
    if not first:
        log.debug("TLS mux: %s sent nothing after the handshake", peer_ip)
        client_w.close()
        return

    if first[0] == MQTT_CONNECT:
        target, label = mqtt_port, "MQTT"
    else:
        target, label = http_port, "HTTPS"
    try:
        up_r, up_w = await asyncio.open_connection("127.0.0.1", target)
    except OSError as e:
        log.warning("TLS mux: %s from %s, but nothing listens on 127.0.0.1:%d: %s",
                    label, peer_ip, target, e)
        client_w.close()
        return
    log.info("TLS mux: %s from %s -> 127.0.0.1:%d", label, peer_ip, target)
    _LIVE.add(up_w)
    try:
        up_w.write(first)
        await up_w.drain()
        await asyncio.gather(_pipe(client_r, up_w), _pipe(up_r, client_w))
    finally:
        _LIVE.discard(up_w)


async def start_tls_mux(port: int, certfile: str, keyfile: str,
                        http_port: int, mqtt_port: int,
                        host: str = "0.0.0.0") -> asyncio.base_events.Server:
    """Bind the listener and return the server; the caller owns its lifetime.

    A handshake the client abandons — the ESP32 models reject a self-signed
    certificate for MQTT, after seeing it — raises `ssl.SSLError` inside
    asyncio's transport, which it logs only in debug mode, so a device that
    probes and leaves does not fill the log.
    """
    ctx = device_ssl_context(certfile, keyfile)
    server = await asyncio.start_server(
        lambda r, w: _handle(r, w, http_port, mqtt_port),
        host, port, ssl=ctx, ssl_handshake_timeout=HANDSHAKE_TIMEOUT,
    )
    bound = server.sockets[0].getsockname()[1] if server.sockets else port
    log.info("TLS listener on port %d: HTTPS API -> %d, MQTT -> %d",
             bound, http_port, mqtt_port)
    return server


async def serve_tls_mux(port: int, certfile: str, keyfile: str,
                        http_port: int, mqtt_port: int) -> None:
    """Run the listener until cancelled — the shape `lifecycle._spawn` wants."""
    server = await start_tls_mux(port, certfile, keyfile, http_port, mqtt_port)
    try:
        # Not `serve_forever()`: on cancellation that awaits `wait_closed()`
        # itself, before anything here could run, and on Python < 3.13 that
        # means waiting for every piped connection to end on its own.
        await asyncio.Future()
    finally:
        server.close()
        for w in list(_LIVE):
            try:
                w.close()
            except Exception:  # pragma: no cover - already gone
                pass
        try:
            await asyncio.wait_for(server.wait_closed(), 2.0)
        except (TimeoutError, asyncio.CancelledError):
            pass
