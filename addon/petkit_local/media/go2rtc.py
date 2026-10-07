"""go2rtc as a sidecar: the only safe way to hand a device camera to anything.

The camera patcher gets H.264 out of the device, and then nothing can consume it.
Two faults, both measured on a T5:

* **Home Assistant's Generic Camera segfaults on the FLV.** Its `stream`
  component opens the URL with PyAV and `av.open()` dies inside libav, killing
  the whole Python process — `Fatal Python error: Segmentation fault` in
  `stream/worker.py::try_open_stream`, and HA restarts. Seen twice, with audio
  and without, and NOT reproducible with `av.open` in isolation (single, looped,
  three-concurrent, or with HA's own options), so it needs HA's threading
  context. That makes it an HA/PyAV bug we cannot fix and have to route around.
* **tserver needs seconds between connections.** Opened back to back it refuses
  the second one; the same pair six seconds apart both succeed. Three concurrent
  opens serve one and refuse two. So it is a cooldown, not an alternation — and
  anything that opens a stream per viewer runs into it.

go2rtc fixes both: it reads the FLV, republishes RTSP, and holds exactly ONE
connection to the device however many consumers attach. Verified end to end,
including a 120 s soak with no reconnects.

It does not abolish the cooldown, and cannot: go2rtc drops the producer when the
last consumer leaves, so a viewer arriving immediately after another left still
lands in it and gets a 404. Measured on the deployed add-on — back-to-back opens
fail, six seconds apart all succeed. Normal viewing does not look like that, and
the alternative is holding the device streaming 24/7, which is the thing this
design exists to avoid.

It is deliberately a child process rather than a library. go2rtc is Go, it is
already the thing Home Assistant itself ships for WebRTC, and re-implementing an
RTSP server in Python to avoid one `exec` would be the worse trade.

**The device is only streamed from while someone is watching.** go2rtc dials the
producer on the first consumer and drops it when the last one leaves, which is
why this is a sidecar and not a frame pump — a device sitting at 82% CPU should
not stream 24/7 to fill a thumbnail.
"""
from __future__ import annotations

import asyncio
import contextlib
import json
import logging
import os
import shutil
import socket
import time
from typing import TYPE_CHECKING, Any

from urllib.parse import urlparse

import aiohttp

from petkit_local.media.turn import STUN_URL
from petkit_local.patchers.camera import STREAM_PATHS, stream_urls
from petkit_local.patchers.common import TALK_TCP_PORT

if TYPE_CHECKING:  # pragma: no cover - typing only
    from petkit_local.devices.base import Device
    from petkit_local.devices.registry import DeviceRegistry

log = logging.getLogger(__name__)

#: Where go2rtc listens. RTSP is the only one anything else talks to, and under
#: the Supervisor it does not need a host port at all — Home Assistant reaches
#: add-ons by hostname on the internal network. The API is bound to loopback
#: because nothing outside this container has any business calling it directly;
#: the panel's `web/api/stream.py` proxies the streaming subset of it to the
#: browser. WebRTC is off by default: host-candidate WebRTC only reaches a
#: browser when the container has a LAN-routable address, which is exactly the
#: macvlan / host-network case — behind a bridge NAT it would gather
#: unreachable 172.x candidates and buy nothing (see `lan_ip`).
RTSP_PORT = 8554
API_PORT = 1984
API_ADDR = f"127.0.0.1:{API_PORT}"
#: The basic-auth user when the API is published on the host (render_config).
API_USERNAME = "petkit"
WEBRTC_PORT = 8555
#: What a browser sends over WebRTC and go2rtc hands an `exec:` sink as is:
#: raw A-law at 8 kHz. Browsers offer Opus, PCMA and PCMU; go2rtc writes the
#: RTP payload unchanged to the command's stdin, so the sink is told which.
BACKCHANNEL_AUDIO = "pcma/8000"


def lan_ip(toward: str) -> str:
    """The container's own address on the route to `toward` (a LAN device IP).

    Used as the WebRTC ICE host candidate: on a macvlan / host-network install
    this is the address the browser (same LAN) can reach WebRTC media on. We
    probe toward a DEVICE, not a public IP, on purpose: the container's default
    route is the docker bridge (172.x, not LAN-reachable), while a route to a
    camera on 192.168.x.y goes out the macvlan interface and so reports the
    macvlan source address. The connect() sends nothing — it only asks the
    kernel which source address that route would use.
    """
    if not toward:
        return ""
    s = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
    try:
        s.connect((toward, 9))
        ip = s.getsockname()[0]
    except OSError:
        return ""
    finally:
        s.close()
    # A bridge-NAT (172.16/12) or loopback/link-local source is not reachable
    # from the LAN, so it is no better than nothing for a candidate.
    return "" if ip.startswith(("172.", "127.", "169.254.")) else ip


def backchannel_source(ip: str) -> str:
    """The go2rtc `exec:` producer that carries WebRTC talk to a device.

    go2rtc starts the command when a viewer opens a microphone track and
    pipes the far-end audio to its stdin as raw A-law/8000. ffmpeg (in the
    image) transcodes it to the 16 kHz mono ADTS-AAC the firmware's `media`
    decodes and sends it to the sink the Two-Way Talk patcher installed --
    the same path `web/api/talk.py` drives from the panel's own microphone
    WebSocket, with the same low-latency flags: a talk is only as good as
    its delay. go2rtc ends the command when the microphone track closes.
    """
    return (
        "exec:ffmpeg -hide_banner -nostats -loglevel warning -fflags nobuffer "
        "-flags low_delay -f alaw -ar 8000 -ac 1 -i pipe:0 "
        "-ar 16000 -ac 1 -c:a aac -b:a 48k -rw_timeout 5000000 "
        f"-f adts tcp://{ip}:{TALK_TCP_PORT}#backchannel=1#audio={BACKCHANNEL_AUDIO}"
    )

#: How often the supervisor reconciles. Same shape and the same reasoning as
#: `mqtt/upstream.py`: the panel's only contract is that it mutates shared
#: state, so this is polled rather than subscribed to.
SUPERVISE_INTERVAL_SECONDS = 30.0

#: How long a POSITIVE probe result is trusted. Long on purpose — see
#: `probe_stream`.
PROBE_TTL_SECONDS = 600.0

#: How long a NEGATIVE one is. A camera that answered "no stream" is usually
#: one that is rebooting -- every patcher ends in a reboot -- and trusting that
#: for ten minutes left the stream, the camera in Home Assistant and two-way
#: talk gone for ten minutes after each patch. One connection attempt a minute
#: is cheap; being wrong for ten minutes is not.
PROBE_RETRY_SECONDS = 60.0

#: The first bytes of an FLV file. An open port is NOT evidence of a stream, so
#: this signature is what the probe actually requires.
FLV_SIGNATURE = b"FLV\x01"

#: Give up on a probe quickly: it runs against a device on the LAN, and a slow
#: answer is as good as no answer for a question this cheap.
PROBE_TIMEOUT = 5.0

#: `state` key holding the probe's verdict. Derived, like `lastClipPath` and
#: `streamUrl`, and deliberately in `state` rather than `config`: it describes
#: the device as it is right now, and it must not survive a restart as a fact.
STREAM_AVAILABLE = "streamAvailable"

_have_go2rtc_cache: bool | None = None


def have_go2rtc() -> bool:
    """Whether the go2rtc binary is on PATH.

    Resolved once per process, like `transcode.have_ffmpeg`: the answer cannot
    change inside a container's lifetime, and this is asked on every pass.
    """
    global _have_go2rtc_cache
    if _have_go2rtc_cache is None:
        _have_go2rtc_cache = shutil.which("go2rtc") is not None
    return _have_go2rtc_cache


def stream_name(device: Device) -> str:
    """go2rtc's name for this device's stream — the RTSP path.

    The petkit id, because it is the one identifier that is stable, unique and
    already how every other topic and route addresses a device. A friendly name
    would be nicer to read and would change under the owner's hands.
    """
    return str(device.petkit_id)


def rtsp_url(device: Device, host: str) -> str:
    """The address to hand Home Assistant for this device."""
    return f"rtsp://{host}:{RTSP_PORT}/{stream_name(device)}"


def advertised_host() -> str:
    """The hostname to put in an RTSP URL, as seen from outside this container.

    Read at runtime and never hardcoded, because the name is not ours to predict:
    the Supervisor prefixes an add-on with its REPOSITORY, not its slug. A local
    install is `local-<slug>`; published, it becomes `<url-hash>-<slug>`. So the
    only source that is right on every install is the container itself.

    Falls back to the LAN address when the hostname does not resolve — the
    docker-compose path, which has no internal DNS.
    """
    name = socket.gethostname()
    try:
        socket.getaddrinfo(name, None)
    except OSError:
        return ""
    return name


async def probe_stream(ip: str, timeout: float = PROBE_TIMEOUT) -> bool:
    """Whether `ip` is actually serving the camera stream right now.

    This exists because `config["active_patchers"]` is OUR bookkeeping, not the
    device's state. A factory reset or an app OTA wipes /system and takes the
    patch with it while our JSON still says applied; equally a device could be
    serving a stream nobody recorded. Advertising a URL that then refuses the
    connection is the failure `patchers/camera.py::stream_urls` already warns
    about — it reads as a broken camera rather than an unapplied patch.

    An open port is not enough, so this requires the FLV signature. Never raises:
    every failure is "no stream", which is the safe answer.
    """
    if not ip:
        return False
    url = f"http://{ip}/{STREAM_PATHS['flv']}"
    try:
        async with aiohttp.ClientSession() as session:
            async with session.get(url, timeout=aiohttp.ClientTimeout(total=timeout)) as resp:
                if resp.status != 200:
                    return False
                head = await resp.content.readexactly(len(FLV_SIGNATURE))
    except Exception as e:
        log.debug("go2rtc: probe of %s found no stream: %s", ip, e)
        return False
    return head == FLV_SIGNATURE


def render_config(streams: dict[str, str], log_path: str,
                  webrtc_candidate: str = "",
                  backchannels: dict[str, str] | None = None,
                  api_password: str = "", api_public: bool = False,
                  extra_streams: dict[str, list[str]] | None = None) -> str:
    """The go2rtc YAML for `streams`, as `{name: source url}`.

    Hand-rendered rather than via PyYAML: it is a fixed short document with one
    generated section, the values are URLs we built ourselves, and adding a
    serialiser dependency for it would be the larger change.

    WebRTC stays off unless `webrtc_candidate` (a LAN-reachable `host` or
    `host:port`) is given: the browser needs a host ICE candidate it can reach,
    which only exists on a macvlan / host-network install. With one, go2rtc
    listens on WEBRTC_PORT and advertises exactly that candidate — the browser
    then gets sub-second WebRTC instead of buffered MSE.

    `backchannels` names the streams whose device has the talk sink
    (`{stream name: device ip}`); each gets a third, on-demand producer that
    carries the viewer's microphone to the device (`backchannel_source`).

    With `api_public` and a password the API listens on every interface
    behind basic auth, for a consumer outside the container -- the WebRTC
    Camera integration in Home Assistant. go2rtc exempts loopback from that
    auth (`local_auth` stays off), so the panel's proxy keeps working as is.
    Without a password the API stays on loopback whatever `api_public` says:
    it can register `exec:` sources, which is a shell on the host.

    `extra_streams` (`{name: [source, ...]}`, the `go2rtc_extra_streams`
    option) are rendered after the device streams exactly as given: no Opus
    transcode and no backchannel are added, because the add-on knows nothing
    about what is behind them -- a doorbell's own `doorbird://` source already
    carries its two-way audio. A name that collides with a device stream is
    skipped: the device's stream is the one the panel and the sensor point at.
    """
    if webrtc_candidate:
        cand = webrtc_candidate if ":" in webrtc_candidate else f"{webrtc_candidate}:{WEBRTC_PORT}"
        webrtc_lines = [
            "webrtc:",
            f"  listen: ':{WEBRTC_PORT}'",
            "  candidates:",
            f"    - {cand}",
            # A STUN server so go2rtc also offers a reflexive candidate — its
            # home public address. Off the LAN the browser reaches it via a
            # Cloudflare TURN relay (see media/turn.py); on the LAN the host
            # candidate above wins and this is simply unused. Static and
            # unauthenticated, so nothing to rotate.
            "  ice_servers:",
            f"    - urls: [{STUN_URL!r}]",
        ]
    else:
        webrtc_lines = ["webrtc:", "  listen: ''"]
    if api_public and api_password:
        api_lines = [
            "api:",
            f"  listen: ':{API_PORT}'",
            f"  username: {json.dumps(API_USERNAME)}",
            f"  password: {json.dumps(api_password)}",
        ]
    else:
        api_lines = ["api:", f"  listen: {API_ADDR!r}"]
    lines = [
        "# Generated by petkit-local. Edits are overwritten.",
        *api_lines,
        "rtsp:",
        f"  listen: ':{RTSP_PORT}'",
        *webrtc_lines,
        "log:",
        f"  output: {log_path!r}",
        "streams:",
    ]
    for name, source in sorted(streams.items()):
        # Two producers per stream. The device FLV carries H.264 + AAC — fine
        # for RTSP and MSE, but WebRTC cannot carry AAC, so a WebRTC viewer
        # would get a silent stream. The second producer runs ffmpeg (already in
        # the image) to transcode this stream's audio to Opus, the codec WebRTC
        # does carry. `ffmpeg:{name}` reads go2rtc's OWN stream, not the device
        # again, so it adds no second connection to the one tserver reliably
        # allows — and go2rtc only starts it while a consumer wants Opus.
        lines.append(f"  {name}:")
        lines.append(f"    - {source}")
        lines.append(f"    - ffmpeg:{name}#audio=opus")
        sink_ip = (backchannels or {}).get(name)
        if sink_ip:
            # JSON is valid YAML for a one-line string, and the exec line has
            # spaces, colons and a `#` that YAML would otherwise read into.
            lines.append(f"    - {json.dumps(backchannel_source(sink_ip))}")
    rendered_extra = 0
    for name, sources in sorted((extra_streams or {}).items()):
        if name in streams:
            log.warning("go2rtc: extra stream %r collides with a device stream, "
                        "skipping it", name)
            continue
        # JSON-quoted on both sides: a source URL carries `#`, `:` and `@`,
        # and a name is the user's to choose.
        lines.append(f"  {json.dumps(name)}:")
        for source in sources:
            lines.append(f"    - {json.dumps(source)}")
        rendered_extra += 1
    if not streams and not rendered_extra:
        lines.append("  {}")
    return "\n".join(lines) + "\n"


def _write_config(path: str, config: str) -> None:
    """Write the rendered config, creating its directory (blocking — call via
    a thread)."""
    os.makedirs(os.path.dirname(path) or ".", exist_ok=True)
    with open(path, "w", encoding="utf-8") as f:
        f.write(config)


class Go2rtc:
    """Runs go2rtc for as long as there is a camera worth serving.

    Same supervise/reconcile/stop shape as `mqtt/upstream.py::UpstreamMQTT`, for
    the same reason: what it should be doing is derived from shared state that
    something else mutates, so it is reconciled on a timer rather than driven by
    events.
    """

    def __init__(self, registry: DeviceRegistry, *, data_dir: str,
                 on_change: Any | None = None, host_candidate: str = "",
                 api_password: str = "", api_public: bool = False,
                 extra_streams: dict[str, list[str]] | None = None) -> None:
        """
        Args:
            on_change: Awaited after the child starts or stops, so whatever
                publishes `streamUrl` can re-publish it. Without this the sensor
                in Home Assistant stays empty until the device happens to report
                something — measured: the sidecar came up 21 seconds after the
                last state publish, and nothing republished for minutes.
        """
        self._registry = registry
        self._on_change = on_change
        #: The WebRTC candidate to advertise when the container has no LAN
        #: address of its own: the host and its published port
        #: (`config.webrtc_candidate`). Empty leaves WebRTC off in that case.
        self._host_candidate = host_candidate
        self._api_password = api_password
        self._api_public = api_public
        #: `go2rtc_extra_streams`: served whenever go2rtc runs, and reason
        #: enough to run it with no PetKit camera at all (see `wanted`).
        self._extra_streams = dict(extra_streams or {})
        self._config_path = os.path.join(data_dir, "go2rtc.yaml")
        self._log_path = os.path.join(data_dir, "go2rtc.log")
        self._proc: asyncio.subprocess.Process | None = None
        self._rendered = ""
        #: petkit_id -> (monotonic deadline, verdict, ip probed). Probing costs
        #: one of the device's connections, and tserver only reliably has one.
        #: The ip is part of the entry: a verdict about one address says
        #: nothing about another, and the address seeded from `last_ip` at
        #: startup may be stale.
        self._probes: dict[int, tuple[float, bool, str]] = {}

    @property
    def running(self) -> bool:
        """Whether our go2rtc child is alive right now."""
        return self._proc is not None and self._proc.returncode is None

    def stream_url_for(self, device: Device) -> str:
        """The RTSP URL for `device`, or "" when we are not serving it."""
        if not self.running or not device.state.get(STREAM_AVAILABLE):
            return ""
        host = advertised_host()
        return rtsp_url(device, host) if host else ""

    def desired_streams(self) -> dict[str, str]:
        """`{stream name: device FLV url}` for every confirmed camera."""
        streams = {}
        for device in self._registry.all():
            ip = (device.state or {}).get("ip", "")
            if ip and device.state.get(STREAM_AVAILABLE):
                streams[stream_name(device)] = f"http://{ip}/{STREAM_PATHS['flv']}"
        return streams

    def desired_backchannels(self) -> dict[str, str]:
        """`{stream name: device ip}` for every served camera with the talk sink.

        The sink is what the Two-Way Talk patcher installs, so only a device
        with that patcher active gets a backchannel producer: on any other the
        exec would connect to nothing, and go2rtc would log a dead command per
        microphone click.
        """
        out = {}
        for device in self._registry.all():
            ip = (device.state or {}).get("ip", "")
            if (ip and device.state.get(STREAM_AVAILABLE)
                    and "talk" in (device.config.get("active_patchers") or [])):
                out[stream_name(device)] = ip
        return out

    def wanted(self) -> bool:
        """Whether go2rtc should be running: it exists and has something to
        serve -- a confirmed PetKit camera, or a stream the owner configured."""
        return have_go2rtc() and bool(self.desired_streams() or self._extra_streams)

    async def _watched_streams(self) -> set[str]:
        """Stream names go2rtc currently has a viewer on.

        This is the set whose device connection must not be disturbed. It is
        asked of go2rtc rather than inferred from `self.running`, and that
        distinction is load-bearing: go2rtc stays up for as long as any camera
        is configured, but it only dials a device while somebody is watching.
        Treating "process alive" as "connection held" would suspend probing for
        good, and a device that quietly stopped serving would keep its URL
        advertised forever — the exact staleness the probe exists to prevent.
        """
        try:
            async with aiohttp.ClientSession() as session:
                async with session.get(
                    f"http://{API_ADDR}/api/streams",
                    timeout=aiohttp.ClientTimeout(total=2),
                ) as resp:
                    streams = await resp.json()
        except Exception:
            # Cannot tell — assume every stream is in use. Skipping a probe only
            # delays a verdict; stealing a live viewer's connection breaks it.
            return set(self.desired_streams())
        return {name for name, info in (streams or {}).items() if (info or {}).get("consumers")}

    async def refresh_probes(self) -> None:
        """Re-probe cameras whose verdict has expired.

        A device being watched right now is skipped: go2rtc holds its only
        connection, and a probe would take the slot and come back saying there
        is no stream.
        """
        busy = await self._watched_streams() if self.running else set()
        now = time.monotonic()
        for device in self._registry.all():
            if not device.is_camera or stream_name(device) in busy:
                continue
            ip = (device.state or {}).get("ip", "")
            if not ip:
                device.state.pop(STREAM_AVAILABLE, None)
                self._probes.pop(device.petkit_id, None)
                continue
            deadline, _, probed_ip = self._probes.get(device.petkit_id, (0.0, False, ""))
            if deadline > now and probed_ip == ip:
                continue
            available = await probe_stream(ip)
            ttl = PROBE_TTL_SECONDS if available else PROBE_RETRY_SECONDS
            self._probes[device.petkit_id] = (now + ttl, available, ip)
            if available:
                device.state[STREAM_AVAILABLE] = True
            else:
                device.state.pop(STREAM_AVAILABLE, None)

    async def supervise(self) -> None:
        """Reconcile forever, and take the child down with us.

        The cancel handler wraps the WHOLE loop, sleep included. That is not
        tidiness: this task spends essentially all of its life in the sleep, so
        a handler around only the reconcile body would miss the cancellation
        that actually happens and leave go2rtc running after shutdown — holding
        both the RTSP port and the device's one connection.
        """
        try:
            while True:
                try:
                    await self.refresh_probes()
                    await self.reconcile()
                except asyncio.CancelledError:
                    raise
                except Exception:
                    log.exception("go2rtc supervisor pass failed")
                await asyncio.sleep(SUPERVISE_INTERVAL_SECONDS)
        except asyncio.CancelledError:
            await self.stop()
            raise

    async def reconcile(self) -> None:
        """Start, restart or stop the child to match what the devices need."""
        if not self.wanted():
            if self.running:
                log.info("go2rtc: no camera to serve, stopping")
            await self.stop()
            return

        streams = self.desired_streams()
        # WebRTC candidate: our own LAN address, found by probing the route
        # toward a camera (see lan_ip). Any device IP does — they share the LAN,
        # and so does whatever an extra stream points at.
        device_ip = ""
        for url in [*streams.values(),
                    *(s for srcs in self._extra_streams.values() for s in srcs)]:
            device_ip = urlparse(url).hostname or ""
            if device_ip:
                break
        # A container with a LAN address of its own (macvlan) advertises that;
        # behind the Supervisor's bridge NAT `lan_ip` finds nothing reachable
        # and the host's published port stands in.
        config = render_config(streams, self._log_path,
                               webrtc_candidate=lan_ip(device_ip) or self._host_candidate,
                               backchannels=self.desired_backchannels(),
                               api_password=self._api_password,
                               api_public=self._api_public,
                               extra_streams=self._extra_streams)
        if self.running and config == self._rendered:
            return

        if self.running:
            log.info("go2rtc: stream set changed, restarting")
            await self.stop()

        await asyncio.to_thread(_write_config, self._config_path, config)
        self._rendered = config
        await self._start()

    async def _start(self) -> None:
        """Spawn go2rtc against the config already written.

        Never raises: a camera that cannot be served must not take the add-on
        down with it, so a failure is logged and leaves `running` False.
        """
        try:
            # DEVNULL rather than pipes: this child outlives the call, and one
            # whose output is never drained eventually blocks on a full pipe.
            # go2rtc writes to its own log file instead (see render_config).
            self._proc = await asyncio.create_subprocess_exec(
                "go2rtc", "-c", self._config_path,
                stdout=asyncio.subprocess.DEVNULL,
                stderr=asyncio.subprocess.DEVNULL,
            )
        except OSError as e:
            # Same contract as `transcode.run_ffmpeg`: a missing or unrunnable
            # binary degrades, it does not take the add-on down.
            log.warning("go2rtc could not be started: %s", e)
            self._proc = None
            return
        log.info("go2rtc serving %d stream(s) on RTSP :%d (pid %d)",
                 len(self.desired_streams()) + len(self._extra_streams),
                 RTSP_PORT, self._proc.pid)
        await self._notify()

    async def stop(self) -> None:
        """Kill and reap the child. Idempotent, and safe to call twice."""
        proc, self._proc = self._proc, None
        self._rendered = ""
        if proc is None or proc.returncode is not None:
            return
        with contextlib.suppress(ProcessLookupError):
            proc.kill()
        # Reap, so it cannot become a zombie for the life of the container.
        with contextlib.suppress(asyncio.TimeoutError, Exception):
            await asyncio.wait_for(proc.wait(), 10)
        await self._notify()

    async def _notify(self) -> None:
        """Tell the caller the URLs just changed. Never fatal: this is a courtesy
        re-publish, and failing it must not take the supervisor down."""
        if self._on_change is None:
            return
        try:
            await self._on_change()
        except asyncio.CancelledError:
            raise
        except Exception:
            log.exception("go2rtc: notifying of a stream change failed")


def stream_urls_with_rtsp(device: Device, supervisor: Any | None) -> dict[str, str]:
    """The device's own URLs plus the RTSP one, when there is a supervisor.

    Kept out of `patchers/camera.py` so that module stays about the patch and
    knows nothing about go2rtc.
    """
    urls = dict(stream_urls(device))
    rtsp = supervisor.stream_url_for(device) if supervisor is not None else ""
    if rtsp:
        return {"rtsp": rtsp, **urls}
    return urls
