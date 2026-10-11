"""The telnet provisioning path: the file format, the shell, the sequence.

The YumShare Dual-Hopper 2 never answers over Bluetooth but has a root shell,
and its server addresses sit in an AES-encrypted `/opt/user.conf`. The manual
run this automates was done on 2026-10-05; these pin the format and every
check that keeps a wrong device or a bad upload from being written.
"""
import asyncio
import base64
import hashlib
import json
import os

import pytest

from petkit_local.provision import userconf
from petkit_local.provision.telnet import TelnetShell
from petkit_local.provision.telnet_flow import ProvisionError, provision_via_telnet

DOC = {
    "idInfo": {"dev_id": 300039037, "dev_srt": "88c95978117db8d8c782140e134fdc2a"},
    "serverInfo": {"apiServers": ["https://api-eu.petkt.com/6/"], "ipServers": ["https://3.66.36.97:80/6/"],
                   "dns": "", "linked": 1},
    "app_conf": {"feedSound": 1},
}


def test_the_file_format_round_trips_and_is_checked():
    blob = userconf.encrypt(userconf.dump(DOC), iv=b"\x01" * 16)
    assert blob[32:40] == userconf.MAGIC
    assert hashlib.md5(blob[32:]).hexdigest().encode() == blob[:32]
    assert userconf.load(blob) == DOC
    with pytest.raises(userconf.UserConfError):
        userconf.decrypt(b"\x00" * 80)
    bad = bytearray(blob)
    bad[70] ^= 0xFF  # inside the ciphertext: checksum field no longer matches
    with pytest.raises(userconf.UserConfError):
        userconf.decrypt(bytes(bad))


def test_point_at_server_touches_only_the_first_entries():
    doc = json.loads(json.dumps(DOC))
    changes = userconf.point_at_server(doc, "http://192.168.1.5/6/")
    assert doc["serverInfo"]["apiServers"] == ["http://192.168.1.5/6/"]
    assert doc["serverInfo"]["ipServers"] == ["http://192.168.1.5/6/"]
    assert doc["serverInfo"]["linked"] == 1 and doc["app_conf"] == {"feedSound": 1}
    assert len(changes) == 2
    assert userconf.point_at_server(doc, "http://192.168.1.5/6/") == []


class FakeShell:
    """A scripted device: a dict of command -> output, writes captured."""

    def __init__(self, arch="armv7l", conf=None):
        self.conf = conf if conf is not None else userconf.encrypt(userconf.dump(DOC))
        self.arch = arch
        self.files = {"/opt/user.conf": self.conf}
        self.b64 = ""
        self.commands = []
        self.logged_in = None

    async def login(self, user, password):
        self.logged_in = (user, password)

    async def run(self, cmd, timeout=30.0):
        self.commands.append(cmd)
        if cmd == "uname -m":
            return 0, self.arch
        if cmd.startswith("cat /opt/version"):
            return 0, "70c4436485f164024a07fd9c88f21563{\n\t\"current\":\t{\n\t\t\"firmwareVer\":\t\"895\"\n\t}\n}"
        if cmd.startswith("test -f /opt/user.conf"):
            return 0, "yes" if "/opt/user.conf" in self.files else ""
        if cmd.startswith("md5sum "):
            path = cmd.split()[1]
            data = self.files.get(path)
            return (0, f"{hashlib.md5(data).hexdigest()}  {path}") if data is not None else (1, "")
        if cmd.startswith("base64 /opt/user.conf"):
            return 0, base64.b64encode(self.files["/opt/user.conf"]).decode()
        if cmd == "rm -f /tmp/pk_uc.b64":
            self.b64 = ""
            return 0, ""
        if cmd.startswith("echo '") and ">> /tmp/pk_uc.b64" in cmd:
            self.b64 += cmd.split("'")[1]
            return 0, ""
        if cmd.startswith("base64 -d /tmp/pk_uc.b64 > /opt/user.conf.new"):
            self.files["/opt/user.conf.new"] = base64.b64decode(self.b64)
            return 0, ""
        if cmd.startswith("cp /opt/user.conf /opt/user.conf.bak && mv"):
            self.files["/opt/user.conf.bak"] = self.files["/opt/user.conf"]
            self.files["/opt/user.conf"] = self.files.pop("/opt/user.conf.new")
            return 0, ""
        if cmd.startswith("rm -f /opt/user.conf.new"):
            self.files.pop("/opt/user.conf.new", None)
            return 0, ""
        if cmd == "reboot":
            return 0, ""
        return 1, f"unknown: {cmd}"

    async def close(self):
        pass


async def test_the_happy_path_writes_a_verified_file_and_keeps_backups(tmp_path):
    sh = FakeShell()
    lines = []
    res = await provision_via_telnet("192.0.2.9", "root", "pw", "http://192.168.1.5/6/",
                                     data_dir=str(tmp_path), shell=sh, log=lines.append)
    assert sh.logged_in == ("root", "pw")
    assert res["serial"] == "300039037" and res["rebooted"] is True
    assert any("firmware 895" in ln for ln in lines)
    assert len(res["changes"]) == 2
    new_doc = userconf.load(sh.files["/opt/user.conf"])
    assert new_doc["serverInfo"]["apiServers"] == ["http://192.168.1.5/6/"]
    assert new_doc["idInfo"] == DOC["idInfo"] and new_doc["app_conf"] == DOC["app_conf"]
    assert sh.files["/opt/user.conf.bak"] == sh.conf, "the original stays on the device"
    assert sh.commands[-1] == "reboot"
    backup = res["backup"]
    assert open(os.path.join(backup, "user.conf.orig"), "rb").read() == sh.conf
    assert any("upload verified" in ln for ln in lines) and any("replaced" in ln for ln in lines)


async def test_a_device_with_another_layout_is_refused_before_anything_is_read(tmp_path):
    sh = FakeShell(arch="mips")
    with pytest.raises(ProvisionError, match="unsupported device"):
        await provision_via_telnet("h", "root", "pw", "http://192.168.1.5/6/", data_dir=str(tmp_path), shell=sh)
    assert not any(c.startswith("base64") or c.startswith("echo '") for c in sh.commands)


async def test_an_unchanged_device_is_left_alone(tmp_path):
    doc = json.loads(json.dumps(DOC))
    userconf.point_at_server(doc, "http://192.168.1.5/6/")
    sh = FakeShell(conf=userconf.encrypt(userconf.dump(doc)))
    res = await provision_via_telnet("h", "root", "pw", "http://192.168.1.5/6/", data_dir=str(tmp_path), shell=sh)
    assert res["changes"] == [] and res["rebooted"] is False
    assert "/opt/user.conf.bak" not in sh.files and "reboot" not in sh.commands


async def test_a_corrupted_upload_never_replaces_the_config(tmp_path):
    class Flaky(FakeShell):
        async def run(self, cmd, timeout=30.0):
            if cmd.startswith("base64 -d /tmp/pk_uc.b64"):
                self.files["/opt/user.conf.new"] = b"garbage"
                return 0, ""
            return await super().run(cmd, timeout)

    sh = Flaky()
    with pytest.raises(ProvisionError, match="checksum mismatch"):
        await provision_via_telnet("h", "root", "pw", "http://192.168.1.5/6/", data_dir=str(tmp_path), shell=sh)
    assert sh.files["/opt/user.conf"] == sh.conf and "/opt/user.conf.new" not in sh.files


async def test_the_shell_talks_to_a_busybox_style_telnetd():
    """A fake telnetd: negotiation bytes, two prompts, an echoing shell."""

    def ascii_only(raw):
        # The client answers the negotiation on the same byte stream before it
        # types the login name; a real telnetd consumes those bytes separately.
        return bytes(b for b in raw if 32 <= b < 127).strip()

    async def serve(reader, writer):
        try:
            writer.write(bytes([255, 253, 24, 255, 251, 1]))  # IAC DO TTYPE, IAC WILL ECHO
            writer.write(b"\r\nbox login: ")
            await writer.drain()
            user = ascii_only(await reader.readline())
            writer.write(b"Password: ")
            await writer.drain()
            pw = ascii_only(await reader.readline())
            if (user, pw) != (b"root", b"secret"):
                writer.write(b"\r\nLogin incorrect\r\nbox login: ")
                await writer.drain()
                return
            writer.write(b"\r\n[box:~]# ")
            await writer.drain()
            await shell(reader, writer)
        finally:
            writer.close()

    async def shell(reader, writer):
        while True:
            line = await reader.readline()
            if not line:
                return
            text = line.decode().strip()
            writer.write(line)  # the echo
            if text == "exit":
                return
            out = ""
            for part in text.split("; "):
                if part == "uname -m":
                    out += "armv7l\r\n"
                elif part.startswith("echo "):
                    out += part[5:].replace("''", "").replace("$?", "0") + "\r\n"
                elif part == "false":
                    pass
            writer.write(out.encode() + b"[box:~]# ")
            await writer.drain()

    server = await asyncio.start_server(serve, "127.0.0.1", 0)
    port = server.sockets[0].getsockname()[1]
    try:
        sh = TelnetShell("127.0.0.1", port)
        await sh.login("root", "secret")
        rc, out = await sh.run("uname -m")
        assert (rc, out) == (0, "armv7l")
        rc, out = await sh.run("echo hello")
        assert (rc, out) == (0, "hello")
        await sh.close()
        bad = TelnetShell("127.0.0.1", port)
        with pytest.raises(Exception, match="login refused"):
            await bad.login("root", "wrong")
    finally:
        server.close()
        await server.wait_closed()
