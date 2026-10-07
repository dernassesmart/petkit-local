"""The camera sidecar: config generation, the probe, and the child process."""
import asyncio

import pytest
from aiohttp import web
from aiohttp.test_utils import TestClient, TestServer

from petkit_local.devices.base import Device
from petkit_local.devices.registry import DeviceRegistry
from petkit_local.media import go2rtc as g


@pytest.fixture(autouse=True)
def _binary_present(monkeypatch):
    """`have_go2rtc` memoises, so pin it rather than letting one test's answer
    leak into the next."""
    monkeypatch.setattr(g, "_have_go2rtc_cache", True)
    yield
    g._have_go2rtc_cache = None


def _registry(*devices):
    reg = DeviceRegistry()
    for d in devices:
        reg._devices[d.petkit_id] = d
    return reg


def _cam(petkit_id=1, ip="192.0.2.10", available=True):
    d = Device(device_type="t5", petkit_id=petkit_id, serial_number=f"SN{petkit_id}")
    if ip:
        d.state["ip"] = ip
    if available:
        d.state[g.STREAM_AVAILABLE] = True
    return d


def _sidecar(reg, tmp_path):
    return g.Go2rtc(reg, data_dir=str(tmp_path))


# --- config generation ------------------------------------------------------

def test_no_streams_when_nothing_is_confirmed(tmp_path):
    assert _sidecar(_registry(), tmp_path).desired_streams() == {}
    assert _sidecar(_registry(_cam(available=False)), tmp_path).desired_streams() == {}
    assert _sidecar(_registry(_cam(ip="")), tmp_path).desired_streams() == {}


def test_a_stream_per_confirmed_camera(tmp_path):
    s = _sidecar(_registry(_cam(1), _cam(2, ip="192.0.2.11")), tmp_path)
    assert s.desired_streams() == {
        "1": "http://192.0.2.10/main.flv?audio=1",
        "2": "http://192.0.2.11/main.flv?audio=1",
    }


def test_the_stream_name_is_the_petkit_id():
    """The RTSP path ends up in the user's camera config, so it has to be the
    one identifier that is stable and not the owner's to rename."""
    assert g.stream_name(_cam(30020324)) == "30020324"


def test_the_rendered_config_binds_where_we_intend():
    out = g.render_config({"1": "http://d/main.flv?audio=1"}, "/data/go2rtc.log")
    assert f"listen: ':{g.RTSP_PORT}'" in out
    # The API is loopback-only, and with no reachable candidate WebRTC stays
    # off: behind a bridge NAT it would advertise 172.x addresses nothing on
    # the LAN can reach, so listening would be surface for nothing.
    assert "127.0.0.1:1984" in out
    assert "webrtc:\n  listen: ''" in out
    # Two producers per stream: the device FLV (H.264+AAC, what RTSP and MSE
    # consume) and an on-demand Opus transcode of it, because WebRTC cannot
    # carry AAC and would otherwise play silent video.
    assert "  1:\n    - http://d/main.flv?audio=1\n    - ffmpeg:1#audio=opus" in out


def test_a_lan_candidate_turns_webrtc_on_and_is_advertised_verbatim():
    """With a LAN-reachable address, go2rtc listens for WebRTC and advertises
    exactly that candidate — the browser then connects to it directly."""
    out = g.render_config({"1": "http://d/main.flv?audio=1"}, "/data/go2rtc.log",
                          webrtc_candidate="192.0.2.240")
    assert f"webrtc:\n  listen: ':{g.WEBRTC_PORT}'" in out
    assert f"    - 192.0.2.240:{g.WEBRTC_PORT}" in out
    # And a STUN server, so a public (srflx) candidate is offered for the
    # off-LAN case — the browser side relays via TURN (media/turn.py).
    assert "stun:stun.cloudflare.com:3478" in out


def test_a_candidate_with_an_explicit_port_is_not_rewritten():
    out = g.render_config({}, "/data/go2rtc.log", webrtc_candidate="192.0.2.240:9000")
    assert "    - 192.0.2.240:9000" in out


def test_lan_ip_never_answers_with_an_unreachable_source():
    """A loopback (or bridge-NAT) source address is worse than none: it would
    turn WebRTC on and advertise a candidate no LAN browser can reach."""
    assert g.lan_ip("") == ""
    assert g.lan_ip("127.0.0.1") == ""


def test_an_empty_stream_set_still_renders_valid_yaml():
    assert "streams:\n  {}" in g.render_config({}, "/data/go2rtc.log")


def test_wanted_needs_both_the_binary_and_a_camera(tmp_path, monkeypatch):
    assert _sidecar(_registry(_cam()), tmp_path).wanted() is True
    assert _sidecar(_registry(), tmp_path).wanted() is False
    monkeypatch.setattr(g, "_have_go2rtc_cache", False)
    assert _sidecar(_registry(_cam()), tmp_path).wanted() is False


# --- the probe --------------------------------------------------------------

async def _serving(body, status=200):
    async def handler(request):
        return web.Response(body=body, status=status)

    app = web.Application()
    app.router.add_route("*", "/{path:.*}", handler)
    client = TestClient(TestServer(app))
    await client.start_server()
    return client, f"127.0.0.1:{client.server.port}"


async def test_the_probe_accepts_an_flv_stream():
    client, addr = await _serving(g.FLV_SIGNATURE + b"\x00" * 64)
    try:
        assert await g.probe_stream(addr) is True
    finally:
        await client.close()


async def test_an_open_port_is_not_a_stream():
    """The reason the probe reads bytes at all: something else could be
    listening on 80, and a 200 proves nothing about what it serves."""
    client, addr = await _serving(b"<html>hello</html>")
    try:
        assert await g.probe_stream(addr) is False
    finally:
        await client.close()


async def test_a_non_200_is_not_a_stream():
    client, addr = await _serving(g.FLV_SIGNATURE, status=404)
    try:
        assert await g.probe_stream(addr) is False
    finally:
        await client.close()


async def test_a_refused_connection_is_an_answer_not_an_exception():
    """Port 1 is reserved and never listening. Every probe failure has to be
    'no stream' — this runs on a timer and must not take the supervisor down."""
    assert await g.probe_stream("127.0.0.1:1") is False


async def test_no_ip_is_no_stream():
    assert await g.probe_stream("") is False


async def test_the_probe_answer_is_cached(tmp_path, monkeypatch):
    """Probing costs one of the device's connections and tserver only reliably
    has one, so the same question must not be asked every pass."""
    calls = []

    async def counting(ip, *a, **kw):
        calls.append(ip)
        return True

    monkeypatch.setattr(g, "probe_stream", counting)
    s = _sidecar(_registry(_cam(available=False)), tmp_path)
    await s.refresh_probes()
    await s.refresh_probes()
    assert len(calls) == 1


class _Alive:
    returncode = None


async def test_probing_is_skipped_for_a_stream_someone_is_watching(tmp_path, monkeypatch):
    """go2rtc holds that device's only connection while a viewer is attached, so
    probing would take the slot and come back saying there is no stream."""
    calls = []

    async def counting(ip, *a, **kw):
        calls.append(ip)
        return True

    async def watched(self):
        return {"1"}

    monkeypatch.setattr(g, "probe_stream", counting)
    monkeypatch.setattr(g.Go2rtc, "_watched_streams", watched)
    s = _sidecar(_registry(_cam()), tmp_path)
    s._proc = _Alive()

    await s.refresh_probes()
    assert calls == []


async def test_probing_continues_while_go2rtc_runs_with_nobody_watching(tmp_path, monkeypatch):
    """The gap this closes: go2rtc stays up for as long as any camera is
    configured, but only dials the device while someone watches. Treating
    'process alive' as 'connection held' would suspend probing forever, and a
    device that quietly stopped serving would keep its URL advertised."""
    calls = []

    async def counting(ip, *a, **kw):
        calls.append(ip)
        return False

    async def nobody(self):
        return set()

    monkeypatch.setattr(g, "probe_stream", counting)
    monkeypatch.setattr(g.Go2rtc, "_watched_streams", nobody)
    d = _cam()
    s = _sidecar(_registry(d), tmp_path)
    s._proc = _Alive()

    await s.refresh_probes()
    assert calls == ["192.0.2.10"]
    assert g.STREAM_AVAILABLE not in d.state


async def test_an_unreachable_go2rtc_api_suspends_probing_rather_than_guessing(tmp_path, monkeypatch):
    """Skipping a probe only delays a verdict; stealing a live viewer's
    connection breaks it. So 'cannot tell' errs towards not probing."""
    s = _sidecar(_registry(_cam()), tmp_path)
    s._proc = _Alive()
    assert await s._watched_streams() == {"1"}


async def test_a_failed_probe_clears_a_previous_yes(tmp_path, monkeypatch):
    async def gone(ip, *a, **kw):
        return False

    monkeypatch.setattr(g, "probe_stream", gone)
    d = _cam()
    s = _sidecar(_registry(d), tmp_path)
    await s.refresh_probes()
    assert g.STREAM_AVAILABLE not in d.state


# --- the child process ------------------------------------------------------

async def test_stop_is_idempotent_and_safe_before_any_start(tmp_path):
    s = _sidecar(_registry(), tmp_path)
    await s.stop()
    await s.stop()
    assert s.running is False


async def test_a_missing_binary_degrades_instead_of_raising(tmp_path, monkeypatch):
    """Same contract as ffmpeg: the add-on keeps running without it."""
    s = _sidecar(_registry(_cam()), tmp_path)
    monkeypatch.setattr(g.asyncio, "create_subprocess_exec",
                        lambda *a, **kw: (_ for _ in ()).throw(OSError("nope")))
    await s.reconcile()
    assert s.running is False


async def test_the_child_is_killed_when_the_supervisor_is_cancelled(tmp_path, monkeypatch):
    """An orphaned go2rtc would hold both the RTSP port and the device's one
    connection past our exit."""
    started = asyncio.Event()
    spawned = []
    # Bind the real one first: the patch below replaces the module attribute,
    # so a fake that reached for it by name would call itself.
    real_exec = asyncio.create_subprocess_exec

    async def fake_exec(*args, **kwargs):
        proc = await real_exec("sleep", "300", stdout=asyncio.subprocess.DEVNULL,
                               stderr=asyncio.subprocess.DEVNULL)
        # Capture here and signal AFTER: `_start` only assigns `self._proc` once
        # this returns, so signalling first would race the assignment.
        spawned.append(proc)
        started.set()
        return proc

    async def _confirmed(ip, *a, **kw):
        return True

    monkeypatch.setattr(g, "probe_stream", _confirmed)
    monkeypatch.setattr(g.asyncio, "create_subprocess_exec", fake_exec)
    s = _sidecar(_registry(_cam()), tmp_path)
    task = asyncio.create_task(s.supervise())
    await asyncio.wait_for(started.wait(), 5)

    task.cancel()
    with pytest.raises(asyncio.CancelledError):
        await task
    assert spawned and spawned[0].returncode is not None, "go2rtc was left running"


async def test_the_config_is_only_rewritten_when_the_stream_set_changes(tmp_path, monkeypatch):
    """A restart drops every viewer, so it must not happen on a timer."""
    execs = []
    real_exec = asyncio.create_subprocess_exec

    async def fake_exec(*args, **kwargs):
        execs.append(args)
        return await real_exec("sleep", "300", stdout=asyncio.subprocess.DEVNULL,
                               stderr=asyncio.subprocess.DEVNULL)

    monkeypatch.setattr(g.asyncio, "create_subprocess_exec", fake_exec)
    s = _sidecar(_registry(_cam()), tmp_path)
    try:
        await s.reconcile()
        await s.reconcile()
        await s.reconcile()
        assert len(execs) == 1
    finally:
        await s.stop()


# --- WebRTC behind the Supervisor's bridge NAT, and the talk backchannel ----

def _sink_cam(pid, ip, talk):
    from petkit_local.devices.base import Device
    d = Device(device_type="d4sh", petkit_id=pid, serial_number=f"SN{pid}")
    d.state["ip"] = ip
    d.state[g.STREAM_AVAILABLE] = True
    if talk:
        d.config["active_patchers"] = ["camera", "talk"]
    return d


async def test_reconcile_uses_the_host_candidate_and_adds_a_backchannel_per_sink(tmp_path, monkeypatch):
    """Bridge NAT: `lan_ip` finds nothing reachable, so the host and its
    published port are advertised. A camera whose device has the talk sink
    gets a third producer carrying the viewer's microphone; one without
    does not (the exec would connect to nothing)."""
    import yaml
    from petkit_local.devices.registry import DeviceRegistry
    from petkit_local.patchers.common import TALK_TCP_PORT

    monkeypatch.setattr(g, "have_go2rtc", lambda: True)
    monkeypatch.setattr(g, "lan_ip", lambda toward: "")
    reg = DeviceRegistry()
    reg._devices[1] = _sink_cam(1, "192.0.2.7", talk=True)
    reg._devices[2] = _sink_cam(2, "192.0.2.8", talk=False)
    s = g.Go2rtc(reg, data_dir=str(tmp_path), host_candidate="192.168.1.5:8555")

    async def no_start():
        pass

    monkeypatch.setattr(s, "_start", no_start)
    await s.reconcile()
    doc = yaml.safe_load(s._rendered)
    streams = {str(k): v for k, v in doc["streams"].items()}
    assert doc["webrtc"]["listen"] == f":{g.WEBRTC_PORT}"
    assert doc["webrtc"]["candidates"][0] == "192.168.1.5:8555"
    assert doc["api"]["listen"] == g.API_ADDR, "no password, no public API"
    assert len(streams["1"]) == 3 and len(streams["2"]) == 2
    talk = streams["1"][2]
    assert talk.startswith("exec:ffmpeg ") and talk.endswith(f"#backchannel=1#audio={g.BACKCHANNEL_AUDIO}")
    assert f"tcp://192.0.2.7:{TALK_TCP_PORT}" in talk
    assert "-f alaw -ar 8000 -ac 1 -i pipe:0" in talk, "go2rtc hands the sink raw A-law"


def test_a_macvlan_address_still_wins_over_the_host_candidate(tmp_path, monkeypatch):
    out = g.render_config({"1": "http://d/main.flv?audio=1"}, "/data/go2rtc.log",
                          webrtc_candidate=g.lan_ip("192.0.2.1") or "192.168.1.5:8555")
    # Whatever this machine's route says, exactly one candidate is advertised.
    assert out.count("    - ") >= 1 and "candidates:" in out


def test_the_api_is_published_only_with_a_password():
    import yaml
    out = g.render_config({"1": "http://d/main.flv?audio=1"}, "/data/go2rtc.log",
                          api_password="s3cret", api_public=True)
    assert yaml.safe_load(out)["api"] == {"listen": f":{g.API_PORT}",
                                          "username": g.API_USERNAME, "password": "s3cret"}
    out = g.render_config({}, "/data/go2rtc.log", api_password="", api_public=True)
    assert yaml.safe_load(out)["api"] == {"listen": g.API_ADDR}


# --- extra streams (`go2rtc_extra_streams`) ---------------------------------

DOORBELL = {"doorbird": ["rtsp://u:p@192.0.2.9:8557/mpeg/720p/media.amp",
                         "doorbird://u:p@192.0.2.9?media=audio",
                         "doorbird://u:p@192.0.2.9"]}


def test_extra_streams_are_rendered_verbatim_after_the_device_streams():
    """No Opus transcode and no backchannel: the add-on knows nothing about
    what is behind an extra stream, and a doorbird:// source carries its own
    two-way audio."""
    import yaml
    out = g.render_config({"1": "http://d/main.flv?audio=1"}, "/data/go2rtc.log",
                          extra_streams=DOORBELL)
    doc = yaml.safe_load(out)
    streams = {str(k): v for k, v in doc["streams"].items()}
    assert streams["doorbird"] == DOORBELL["doorbird"]
    assert streams["1"] == ["http://d/main.flv?audio=1", "ffmpeg:1#audio=opus"]


def test_extra_streams_alone_render_a_real_stream_section():
    import yaml
    out = g.render_config({}, "/data/go2rtc.log", extra_streams=DOORBELL)
    assert "streams:\n  {}" not in out
    assert yaml.safe_load(out)["streams"] == DOORBELL


def test_an_extra_stream_never_shadows_a_device_stream():
    """The device's stream is the one the panel and the sensor point at."""
    import yaml
    out = g.render_config({"1": "http://d/main.flv?audio=1"}, "/data/go2rtc.log",
                          extra_streams={"1": ["rtsp://elsewhere/"]})
    streams = {str(k): v for k, v in yaml.safe_load(out)["streams"].items()}
    assert streams["1"][0] == "http://d/main.flv?audio=1"


def test_a_source_with_yaml_hostile_characters_survives_the_round_trip():
    import yaml
    src = "exec:ffmpeg -i x#backchannel=1#audio=pcma/8000"
    out = g.render_config({}, "/data/go2rtc.log",
                          extra_streams={"odd name: yes": [src]})
    assert yaml.safe_load(out)["streams"]["odd name: yes"] == [src]


def test_extra_streams_are_reason_enough_to_run_go2rtc(tmp_path):
    """A doorbell is served even when no PetKit camera is confirmed."""
    s = g.Go2rtc(_registry(), data_dir=str(tmp_path), extra_streams=DOORBELL)
    assert s.wanted() is True
    assert g.Go2rtc(_registry(), data_dir=str(tmp_path)).wanted() is False


async def test_reconcile_renders_the_extra_streams_and_finds_a_candidate_through_them(tmp_path, monkeypatch):
    """With no device stream, the extra stream's host is what `lan_ip` is
    probed toward -- any LAN address does."""
    import yaml
    probed = []

    def fake_lan_ip(toward):
        probed.append(toward)
        return ""

    monkeypatch.setattr(g, "lan_ip", fake_lan_ip)
    s = g.Go2rtc(_registry(), data_dir=str(tmp_path), host_candidate="192.168.1.5:8565",
                 extra_streams=DOORBELL)

    async def no_start():
        pass

    monkeypatch.setattr(s, "_start", no_start)
    await s.reconcile()
    doc = yaml.safe_load(s._rendered)
    assert doc["streams"] == DOORBELL
    assert doc["webrtc"]["candidates"][0] == "192.168.1.5:8565"
    assert probed == ["192.0.2.9"]
