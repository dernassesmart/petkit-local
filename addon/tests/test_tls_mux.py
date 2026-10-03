"""The TLS multiplexer on the device-facing port (`http/tls_mux.py`).

Two in-process echo servers stand in for the HTTP API and the broker; the
assertion is purely about which one a TLS client's bytes reach, decided by the
first byte. The certificate is the add-on's own self-signed one, generated
into a temporary directory the way the lifecycle does it.
"""
import asyncio
import ssl

from petkit_local.http.tls_mux import start_tls_mux
from petkit_local.mqtt.broker import ensure_self_signed


async def _echo_server(tag: bytes):
    async def handle(reader, writer):
        data = await reader.read(64)
        writer.write(tag + data)
        await writer.drain()
        writer.close()

    server = await asyncio.start_server(handle, "127.0.0.1", 0)
    return server, server.sockets[0].getsockname()[1]


def _client_ctx() -> ssl.SSLContext:
    ctx = ssl.create_default_context()
    ctx.check_hostname = False
    ctx.verify_mode = ssl.CERT_NONE
    return ctx


async def _roundtrip(port: int, payload: bytes) -> bytes:
    reader, writer = await asyncio.open_connection("127.0.0.1", port, ssl=_client_ctx())
    writer.write(payload)
    await writer.drain()
    out = await reader.read(256)
    writer.close()
    return out


async def test_first_byte_decides_between_http_and_mqtt(tmp_path):
    cert = str(tmp_path / "broker.crt")
    key = str(tmp_path / "broker.key")
    assert ensure_self_signed(cert, key)

    http_srv, http_port = await _echo_server(b"HTTP:")
    mqtt_srv, mqtt_port = await _echo_server(b"MQTT:")
    mux = await start_tls_mux(0, cert, key, http_port, mqtt_port, host="127.0.0.1")
    port = mux.sockets[0].getsockname()[1]
    try:
        # A device's API call: the request line reaches the HTTP side, first
        # byte included — the mux must not eat the byte it peeked at.
        out = await _roundtrip(port, b"POST /6/d4/dev_signup HTTP/1.1\r\nHost: x\r\n\r\n")
        assert out.startswith(b"HTTP:POST /6/d4/dev_signup")

        # An MQTT CONNECT (packet type 1, flags 0) goes to the broker side.
        out = await _roundtrip(port, b"\x10\x0c\x00\x04MQTT\x04\x02\x00\x3c")
        assert out.startswith(b"MQTT:\x10\x0c\x00\x04MQTT")
    finally:
        for s in (mux, http_srv, mqtt_srv):
            s.close()
            await s.wait_closed()


async def test_shutdown_does_not_wait_for_a_piped_connection_to_end(tmp_path):
    """A device's MQTT session or long poll never ends on its own; a restart
    that waited for it would be killed by the Supervisor after 10 s."""
    from petkit_local.http.tls_mux import serve_tls_mux

    cert = str(tmp_path / "broker.crt")
    key = str(tmp_path / "broker.key")
    assert ensure_self_signed(cert, key)

    async def hold(reader, writer):
        await reader.read(64)
        await reader.read()  # never answers; returns only when the mux closes on it
        writer.close()

    backend = await asyncio.start_server(hold, "127.0.0.1", 0)
    bport = backend.sockets[0].getsockname()[1]
    # Find a free port the way the OS does, then hand it to the mux.
    probe = await asyncio.start_server(lambda r, w: None, "127.0.0.1", 0)
    port = probe.sockets[0].getsockname()[1]
    probe.close()
    await probe.wait_closed()

    task = asyncio.create_task(serve_tls_mux(port, cert, key, bport, bport))
    await asyncio.sleep(0.3)
    reader, writer = await asyncio.open_connection("127.0.0.1", port, ssl=_client_ctx())
    writer.write(b"POST /6/d4/heartbeat HTTP/1.1\r\n\r\n")
    await writer.drain()
    await asyncio.sleep(0.3)

    task.cancel()
    try:
        await asyncio.wait_for(task, 5.0)
    except asyncio.CancelledError:
        pass
    # The piped connection was closed from the server side.
    assert await reader.read(16) == b""
    writer.close()
    backend.close()
    await backend.wait_closed()


async def test_silent_client_is_dropped_without_touching_either_side(tmp_path, monkeypatch):
    from petkit_local.http import tls_mux

    monkeypatch.setattr(tls_mux, "FIRST_BYTE_TIMEOUT", 0.2)
    cert = str(tmp_path / "broker.crt")
    key = str(tmp_path / "broker.key")
    assert ensure_self_signed(cert, key)

    hits = []

    async def handle(reader, writer):
        hits.append(await reader.read(64))
        writer.close()

    backend = await asyncio.start_server(handle, "127.0.0.1", 0)
    bport = backend.sockets[0].getsockname()[1]
    mux = await start_tls_mux(0, cert, key, bport, bport, host="127.0.0.1")
    port = mux.sockets[0].getsockname()[1]
    try:
        reader, writer = await asyncio.open_connection("127.0.0.1", port, ssl=_client_ctx())
        # Say nothing; the mux must close on us rather than hold a backend slot.
        assert await reader.read(16) == b""
        writer.close()
        await asyncio.sleep(0.1)
        assert hits == []
    finally:
        for s in (mux, backend):
            s.close()
            await s.wait_closed()
