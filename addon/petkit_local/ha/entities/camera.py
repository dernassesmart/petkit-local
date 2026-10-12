"""Camera entities shared by every camera-equipped device category.

Litter boxes, feeders and fountains that carry a camera all publish the same
live-view entity plus the two "latest media" entities that media/pipeline.py
updates, so these definitions live here once instead of once per category.
"""
from petkit_local.ha.discovery import EntityDef

# Key must NOT be "camera": that collides with the "camera" on/off switch
# (settings.camera) — HA requires unique_id unique across all platforms.
# REMOVED: `camera_feed`, an MQTT camera on `petkit-local/{id}/camera`.
#
# HA's MQTT camera renders image bytes published to a topic, and nothing ever
# published to that one — verified on a live broker: `last_snapshot` carried a
# 54 KB retained JPEG while `/camera` had nothing at all. So the entity existed
# in every install and could never show anything.
#
# Filling it was considered and rejected. It would mean pulling a frame on a
# timer, and the device only emits a keyframe every ~4.3 s, so a snapshot costs
# ~5 s of streaming from hardware already sitting at 82% CPU — permanently, since
# an MQTT camera gives no signal for "somebody is looking". Live view goes
# through go2rtc instead (`media/go2rtc.py`), and the address is the
# `stream_url` sensor below.
CAMERA_ENTITIES = [
    # Updated by media/pipeline.py via HAPublisher.publish_media_ready once a
    # new eventImage/highLight file is decrypted and ready.
    EntityDef(
        component="image", key="last_snapshot", name="Last Snapshot",
        icon="mdi:image",
    ),
    EntityDef(
        component="sensor", key="last_clip", name="Last Clip",
        value_path="state.lastClipPath", icon="mdi:filmstrip",
        entity_category="diagnostic",
    ),
    # The address to paste into go2rtc (which HA already ships), a Generic
    # Camera or VLC. It is a sensor and not a camera entity because MQTT
    # discovery has no camera platform that takes a URL: HA's MQTT camera
    # renders bytes published to a topic and knows nothing of `stream_source`.
    # Filled only while the camera patcher is applied and the device's IP is
    # known — see `patchers/camera.py::stream_urls`.
    EntityDef(
        component="sensor", key="stream_url", name="Camera Stream URL",
        value_path="state.streamUrl", icon="mdi:video-outline",
        entity_category="diagnostic",
    ),
]

#: Feeders with a camera: the still from the latest eating event, kept apart
#: from `last_snapshot` (which the next appearance overwrites). Filled by
#: `HAPublisher.publish_media_ready` for the events in `EATING_EVENTS`.
FEEDER_CAMERA_IMAGES = [
    EntityDef(component="image", key="last_eating_snapshot", name="Last Eating Snapshot",
              icon="mdi:cat"),
]
