import asyncio
import json
import tempfile
from pathlib import Path

from petkit_local.devices.registry import DeviceRegistry
from petkit_local.ha.publisher import HAPublisher
from tests._fakes import FakeMqttClient


def _setup(media_root):
    reg = DeviceRegistry()
    dev = reg.get_or_create(petkit_id=1, device_type="t5", serial_number="SN")
    pub = HAPublisher(reg, {"media_root": media_root})
    pub._client = FakeMqttClient()
    pub._connected = True
    return reg, dev, pub


async def test_publish_media_ready_noop_without_client():
    reg = DeviceRegistry()
    dev = reg.get_or_create(petkit_id=1, device_type="t5", serial_number="SN")
    pub = HAPublisher(reg, {})
    await pub.publish_media_ready(dev, {"status": "ready", "media_path": "/x.jpg", "category": "eventImage"})
    # no client wired -> nothing to assert on, just must not raise


async def test_publish_media_ready_ignores_none_and_not_ready():
    with tempfile.TemporaryDirectory() as tmp:
        reg, dev, pub = _setup(tmp)
        await pub.publish_media_ready(dev, None)
        await pub.publish_media_ready(dev, {"status": "pending", "media_path": "/x.jpg"})
        assert pub._client.published == []


async def test_publish_media_ready_pushes_snapshot_bytes():
    with tempfile.TemporaryDirectory() as tmp:
        reg, dev, pub = _setup(tmp)
        img_path = Path(tmp) / "Waste" / "photo.jpg"
        img_path.parent.mkdir(parents=True, exist_ok=True)
        img_path.write_bytes(b"\xff\xd8\xff\xe0fakejpeg")

        await pub.publish_media_ready(dev, {
            "status": "ready", "media_path": str(img_path), "category": "eventImage",
        })

        topics = [t for t, _, _ in pub._client.published]
        assert "petkit-local/1/last_snapshot" in topics
        payload = next(p for t, p, _ in pub._client.published if t == "petkit-local/1/last_snapshot")
        assert payload == b"\xff\xd8\xff\xe0fakejpeg"


async def test_publish_media_ready_sets_last_clip_relative_path():
    with tempfile.TemporaryDirectory() as tmp:
        reg, dev, pub = _setup(tmp)
        clip_path = Path(tmp) / "Playback" / "2026-07-22" / "clip.mp4"
        clip_path.parent.mkdir(parents=True, exist_ok=True)
        clip_path.write_bytes(b"not-really-video")

        await pub.publish_media_ready(dev, {
            "status": "ready", "media_path": str(clip_path), "category": "fullVideo",
        })

        assert dev.state["lastClipPath"] == "Playback/2026-07-22/clip.mp4"
        state_topics = [t for t, _, _ in pub._client.published if t == "petkit-local/1/state"]
        assert state_topics
        published_state = json.loads(next(p for t, p, _ in pub._client.published
                                          if t == "petkit-local/1/state"))
        assert published_state["state"]["lastClipPath"] == "Playback/2026-07-22/clip.mp4"


async def test_publish_media_ready_reads_the_snapshot_off_the_event_loop():
    """A whole-file read on the loop stalls every device and both MQTT clients.

    Snapshots run to a few MB and arrive in bursts (the waste gallery is ~5
    photos per cleaning), so the read must yield to other tasks.
    """
    with tempfile.TemporaryDirectory() as tmp:
        reg, dev, pub = _setup(tmp)
        img_path = Path(tmp) / "photo.jpg"
        img_path.write_bytes(b"\xff\xd8\xff\xe0" + b"x" * 500_000)

        order = []

        class RecordingClient(FakeMqttClient):
            async def publish(self, topic, payload, **kw):
                order.append("snapshot-published")
                await super().publish(topic, payload, **kw)

        pub._client = RecordingClient()

        async def other_task():
            order.append("other-task-ran")

        await asyncio.gather(
            pub.publish_media_ready(dev, {
                "status": "ready", "media_path": str(img_path), "category": "eventImage",
            }),
            other_task(),
        )

        assert order == ["other-task-ran", "snapshot-published"], \
            "the snapshot read never yielded — it ran on the event loop"


async def test_publish_media_ready_skips_snapshot_for_video_only_category():
    with tempfile.TemporaryDirectory() as tmp:
        reg, dev, pub = _setup(tmp)
        clip_path = Path(tmp) / "Clips" / "clip.mp4"
        clip_path.parent.mkdir(parents=True, exist_ok=True)
        clip_path.write_bytes(b"video-bytes")

        await pub.publish_media_ready(dev, {
            "status": "ready", "media_path": str(clip_path), "category": "dynamicVideo",
        })

        topics = [t for t, _, _ in pub._client.published]
        assert "petkit-local/1/last_snapshot" not in topics


async def test_an_eating_snapshot_is_also_kept_under_its_own_name():
    with tempfile.TemporaryDirectory() as tmp:
        reg, dev, pub = _setup(tmp)
        img_path = Path(tmp) / "Snapshots" / "photo.jpg"
        img_path.parent.mkdir(parents=True, exist_ok=True)
        img_path.write_bytes(b"\xff\xd8eating")

        await pub.publish_media_ready(dev, {
            "status": "ready", "media_path": str(img_path), "category": "eventImage",
            "event_name": "eat_start",
        })
        topics = [t for t, _, _ in pub._client.published]
        assert topics.count("petkit-local/1/last_snapshot") == 1
        assert topics.count("petkit-local/1/last_eating_snapshot") == 1
        assert next(p for t, p, _ in pub._client.published
                    if t == "petkit-local/1/last_eating_snapshot") == b"\xff\xd8eating"

        pub._client.published.clear()
        await pub.publish_media_ready(dev, {
            "status": "ready", "media_path": str(img_path), "category": "eventImage",
            "event_name": "pet_detect",
        })
        topics = [t for t, _, _ in pub._client.published]
        assert "petkit-local/1/last_snapshot" in topics
        assert "petkit-local/1/last_eating_snapshot" not in topics
