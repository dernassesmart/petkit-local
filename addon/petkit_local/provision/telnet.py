"""A shell on a device's telnet port, enough to run commands and read output.

Written against BusyBox telnetd + ash on the YumShare Dual-Hopper 2. The
standard library's telnetlib is gone in Python 3.13, and this needs almost
nothing of it: option negotiation is refused wholesale (DO -> WONT, WILL ->
DONT), the login is two prompts, and every command is followed by an echo of
a marker that carries the exit status, so output can be cut out of the echoed
stream without guessing at the prompt.
"""
from __future__ import annotations

import asyncio
import re

IAC, DONT, DO, WONT, WILL, SB, SE = 255, 254, 253, 252, 251, 250, 240

LOGIN_TIMEOUT = 15.0
COMMAND_TIMEOUT = 30.0


class TelnetError(RuntimeError):
    pass


class TelnetShell:
    def __init__(self, host: str, port: int = 23) -> None:
        self.host = host
        self.port = port
        self._reader: asyncio.StreamReader | None = None
        self._writer: asyncio.StreamWriter | None = None
        self._buf = b""
        self._seq = 0

    async def login(self, user: str, password: str) -> None:
        try:
            self._reader, self._writer = await asyncio.wait_for(
                asyncio.open_connection(self.host, self.port), LOGIN_TIMEOUT)
        except (OSError, asyncio.TimeoutError) as e:
            raise TelnetError(f"cannot reach {self.host}:{self.port}: {e}") from None
        await self._read_until(b"login:", LOGIN_TIMEOUT, "login prompt")
        await self._send(user + "\n")
        await self._read_until(b"assword:", LOGIN_TIMEOUT, "password prompt")
        await self._send(password + "\n")
        # The prompt, or the login being refused; both end with a known tail.
        got = await self._read_until_any((b"# ", b"$ ", b"login:", b"incorrect"), LOGIN_TIMEOUT, "shell prompt")
        if got in (b"login:", b"incorrect"):
            raise TelnetError("login refused: wrong user or password")
        self._buf = b""

    async def run(self, cmd: str, timeout: float = COMMAND_TIMEOUT) -> tuple[int, str]:
        """Run `cmd`; returns `(exit status, output)`."""
        if self._writer is None:
            raise TelnetError("not connected")
        self._seq += 1
        tag = f"__PK_END_{self._seq}__"
        # The echoed command line must not contain the tag itself, so the
        # shell is handed it in two halves it joins at run time.
        await self._send(f"{cmd}; echo {tag[:5]}''{tag[5:]}:$?\n")
        data = await self._read_until(tag.encode() + b":", timeout, f"end of `{cmd[:40]}`")
        rc_text = await self._read_until(b"\n", timeout, "exit status")
        m = re.match(rb"\s*(\d+)", rc_text)
        rc = int(m.group(1)) if m else -1
        text = data.decode("utf-8", "replace").replace("\r", "")
        # Drop the echoed command line: everything up to and including the
        # line that carries the split tag.
        split_tag = f"{tag[:5]}''{tag[5:]}"
        idx = text.find(split_tag)
        if idx >= 0:
            nl = text.find("\n", idx)
            text = text[nl + 1:] if nl >= 0 else ""
        # And the tag the shell printed at the end.
        end = text.rfind(tag)
        if end >= 0:
            text = text[:end]
        return rc, text.strip("\n")

    async def close(self) -> None:
        if self._writer is not None:
            try:
                await self._send("exit\n")
            except Exception:  # noqa: BLE001
                pass
            self._writer.close()
            self._writer = None

    async def _send(self, text: str) -> None:
        assert self._writer is not None
        self._writer.write(text.encode("utf-8"))
        await self._writer.drain()

    async def _read_until(self, marker: bytes, timeout: float, what: str) -> bytes:
        got = await self._read_until_any((marker,), timeout, what)
        return got

    async def _read_until_any(self, markers: tuple[bytes, ...], timeout: float, what: str) -> bytes:
        """Read, stripping telnet negotiation, until one marker shows up.

        Returns the data BEFORE the marker for a single marker, or the marker
        itself when several are offered (login outcome).
        """
        assert self._reader is not None
        loop = asyncio.get_running_loop()
        deadline = loop.time() + timeout
        while True:
            for mk in markers:
                i = self._buf.find(mk)
                if i >= 0:
                    before = self._buf[:i]
                    self._buf = self._buf[i + len(mk):]
                    return before if len(markers) == 1 else mk
            remaining = deadline - loop.time()
            if remaining <= 0:
                raise TelnetError(f"timeout waiting for {what}")
            try:
                chunk = await asyncio.wait_for(self._reader.read(4096), remaining)
            except asyncio.TimeoutError:
                raise TelnetError(f"timeout waiting for {what}") from None
            if not chunk:
                raise TelnetError(f"connection closed while waiting for {what}")
            self._buf += await self._negotiate(chunk)

    async def _negotiate(self, chunk: bytes) -> bytes:
        """Strip IAC sequences from `chunk`, refusing every option offered."""
        out = bytearray()
        i = 0
        replies = bytearray()
        while i < len(chunk):
            b = chunk[i]
            if b != IAC:
                out.append(b)
                i += 1
                continue
            if i + 1 >= len(chunk):
                break
            cmd = chunk[i + 1]
            if cmd == IAC:
                out.append(IAC)
                i += 2
            elif cmd in (DO, DONT, WILL, WONT) and i + 2 < len(chunk):
                opt = chunk[i + 2]
                if cmd == DO:
                    replies += bytes([IAC, WONT, opt])
                elif cmd == WILL:
                    replies += bytes([IAC, DONT, opt])
                i += 3
            elif cmd == SB:
                end = chunk.find(bytes([IAC, SE]), i)
                i = len(chunk) if end < 0 else end + 2
            else:
                i += 2
        if replies and self._writer is not None:
            self._writer.write(bytes(replies))
            await self._writer.drain()
        return bytes(out)
