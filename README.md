<p align="center"><img src="assets/icon-512.png" width="128" alt="PetKit Local"></p>

# PetKit Local

PetKit's devices route everything through PetKit's servers: the app, the history, the notifications,
and for the camera models the video from inside your home. This is a stand-in for those servers that
runs on your own Home Assistant. Your device connects to it the same way it connected to them and
behaves the same, except that what it records stays with you. It works with the internet unplugged.

This repository is the build that runs in my house. It started from
[alex-so-3/petkit-local](https://github.com/alex-so-3/petkit-local) and carries the changes it took
to get an ESP32 feeder — a **Fresh Element Solo (D4)** — from "provisioned" to "actually controllable
from Home Assistant". Those changes are in the open [CHANGELOG](addon/CHANGELOG.md), one release at a
time, with the evidence for each.

<details>
<summary><b>📸 Screenshots</b></summary>

<br>

**Devices.** Live state, every entity, editable controls and the named actions.

![The Devices tab](assets/panel-devices.png)

**Provision.** Wi-Fi credentials, the server address and the timezone, handed to a device over
Bluetooth from the browser. No PetKit app involved.

![The Provision tab](assets/panel-provision.png)

**Setup.** Proxy mode for adding models, and the guards that stop the real cloud pushing firmware,
shell commands or a log upload through it.

![The Setup tab](assets/panel-setup.png)

**Timeline, AI / Pets, Patchers.** The camera models get a visit timeline, on-device face
recognition and on-device patching — see those tabs once a Purobot or YumShare is connected.

![The Timeline tab](assets/panel-timeline.png)

</details>

## ✨ What you get

- **One app, nothing to maintain around it.** Python in a single process. No database server, no
  web server, no queue. Install it from the Home Assistant app store, done.
- **Entities appear by themselves** over MQTT discovery, through the Mosquitto app you probably
  already run.
- **Set a device up without the PetKit app.** Wi-Fi, server address and timezone go to the device over
  Bluetooth, straight from your browser.
- **ESP32 models work end-to-end.** The Pura X, Pura Max and the non-camera feeders send part of
  their API traffic as HTTPS to the port their MQTT session dials, and read their broker credentials
  in a shape of their own. Both are handled here, which is what makes their heartbeat — the command
  channel that needs no broker — come alive.
- **The camera models get the full treatment** carried over from upstream: a visit timeline, media
  stored on your disk, on-device face recognition, on-device patchers, RTSP for Home Assistant.
- **Proxy mode** for adding a model nobody has run yet: every request is forwarded to PetKit and
  recorded, with firmware pushes, shell commands and credentials stripped on the way through.

## 🐈 Devices

**Confirmed on this build.** Somebody ran it and the controls worked.

| Product | Codename | Board | Notes |
|---|---|---|---|
| Fresh Element Solo | D4 | ESP32 | Firmware 1.267. Feed, light, child lock, sound, schedule. Needs the three DNS records below. |

**Confirmed upstream**, and nothing here touches their path: Purobot Max Pro 2 (T5), Purobot Ultra
(T6), EverSweet Ultra AI (W7H), EverSweet Max Cordless (CTW3), YumShare Dual-Hopper (D4SH).

**Should now work** for the same reason the D4 does — same board, same firmware family — but
nobody has reported back: Pura X (T3), Pura Max (T4), Feeder D3 / D4S. Upstream issues
[#33](https://github.com/alex-so-3/petkit-local/issues/33) and
[#35](https://github.com/alex-so-3/petkit-local/issues/35) describe exactly the symptoms this build
fixes on those two litter boxes. If you own one, a report is worth an issue here.

Everything else — the remaining feeders, the Bluetooth-only fountains and sprays, the other camera
models — is as upstream lists it: entity definitions exist, expect the common things to work and the
occasional entity to sit at "unknown".

## 📦 Install

**Settings → Apps → App Store → ⋮ → Repositories**, add:

```
https://github.com/dernassesmart/petkit-local
```

Install **PetKit Local**, then open its **Configuration** tab before starting it. If you also have
upstream's repository added, the store shows two apps of that name; this one lists
`dernassesmart` as the repository's maintainer. Keep only one installed — both want ports 80
and 443.

- **With the Mosquitto app** there is nothing to fill in — the Supervisor hands over the broker and
  its credentials.
- **With a broker anywhere else**, set `ha_mqtt_host` (and user / password if it wants them).
- `api_url` stays empty; the app asks the Supervisor for your host's LAN IP.
- Set `log_level` to `INFO` while you set a device up. At `WARNING` the log stays empty, which
  looks exactly like a device that never calls.

Start it and open the panel from the sidebar. Updates arrive through the app store like any other
app; the image is built by this repository's own workflow and published as
`ghcr.io/dernassesmart/petkit-local`. Every option, the ports and troubleshooting are in
[the documentation](addon/DOCS.md).

## 🔀 Point a device at it

A device only ever talks to the server it was provisioned with, so it has to be redirected. This
is the real work; everything else configures itself.

### 1. Bluetooth provisioning

The panel's **Provision** tab hands the device your Wi-Fi, this app's address and a timezone. It
runs in Chrome or Edge on a page served over HTTPS — your Home Assistant behind a reverse proxy
with a real certificate does it, Ingress included.

- Put the device in Wi-Fi setup mode first: hold its Wi-Fi button for about five seconds until
  the light flashes fast. It advertises only briefly after that, so press **Provision** right away.
- Give it `http://<ha-host-ip>/6/` as the server. The ESP32 models reach the API on port 80 and
  nothing else.
- **Not from a company laptop.** A managed Windows PC commonly carries an Intune policy
  (`Bluetooth/ServicesAllowedList`) that blocks every GATT service except audio, keyboards and
  the standard ones. The symptom is `NotSupportedError: GATT operation not permitted` the moment
  the tab touches the device, and it looks exactly like a firmware problem. Use a private PC, an
  Android phone with Chrome, or an iPhone with the Bluefy browser.

### 2. DNS records — required for the ESP32 models

Provisioning tells an ESP32 device where the API is, but its firmware has opinions of its own:
after a reboot it goes back to PetKit's regional server, and for MQTT it ignores the host it was
handed and builds a hostname from its product key. Without these records the device loops through
signup every minute and never reaches the heartbeat, so every command queues forever and the
entities go grey after three minutes.

Add three A records in your DNS server (UDM Pro: *Settings → Routing → DNS*; Pi-hole, AdGuard:
local DNS records), all pointing at your Home Assistant host:

| Hostname | Points to |
|---|---|
| `api.eu-pet.com` | your HA host |
| `api-eu.petkt.com` | your HA host |
| `<productKey>.iot-as-mqtt.eu-central-1.aliyuncs.com` | your HA host |

The product key is this app's, not PetKit's, and it is stable for as long as the device stays in
the app's registry. It appears in the log the first time the device fetches its credentials:

```
IoT device info (flat, ESP32 d4): id=400101393 -> pk=a1dfa77ed1 dn=d_d4_… mqttHost=…
```

so the third record for that device would be `a1dfa77ed1.iot-as-mqtt.eu-central-1.aliyuncs.com`.
Power-cycle the device after adding the records. Outside the EU the first two names differ —
your DNS server's query log will show which ones the device asks for.

### 3. What normal looks like

With `log_level: INFO`, a healthy ESP32 device produces this once a minute, then settles into a
heartbeat every ten seconds:

```
TLS mux: HTTPS from 192.168.1.50 -> 127.0.0.1:80
Signup: d4 id=400101393 sn=… fw=1.267
IoT device info (flat, ESP32 d4): …
POST /6/poll/d4/heartbeat [d4 id=400101393] -> 200
Heartbeat d4 (id=400101393): delivering 1 commands
```

The device also tries MQTT against this app and rejects its self-signed certificate. That is
expected on every ESP32 model, costs nothing, and is why the heartbeat is the command channel.

### Linux models

Purobot, YumShare and EverSweet Ultra AI enforce HTTPS and pin the cloud's CA, so for them the
route is Bluetooth provisioning plus the **Patchers** tab, exactly as upstream documents.

## 🔌 Ports

| Container port | Host port | Purpose |
|---|---|---|
| `80` | `80` | Device HTTP API. The ESP32 models dial it from firmware and cannot be told otherwise. |
| `443` | `443` | One TLS listener for two protocols: the device's HTTPS API calls and its MQTT session, told apart by the first byte. |
| `9000` | `9000` | Media upload bucket for the camera models. |

The web panel is reached through Ingress only. Nothing else needs to be exposed.

## 🩺 Troubleshooting

- **Entities grey, "no contact for >180s".** The device is not calling. With `INFO` logging, no
  `Signup` line means it is at PetKit's cloud: check the first two DNS records and power-cycle.
- **Signups every minute, buttons do nothing.** The third DNS record is missing or names the
  wrong product key. The log line above has the right one.
- **Provision tab fails on the first write.** Device not in setup mode, the PetKit app on your
  phone still holding the connection, or a managed Windows PC — see above.
- **Panel log looks frozen after a restart.** `log_level` is at `WARNING`.
- **Stopping the app takes ten seconds and ends in exit 137.** Fixed in 2.1.5; update.

## 🧰 Tools

- [`tools/blufi_provision.py`](tools/blufi_provision.py) — the Provision tab's ESP32 path as a
  Python script (`pip install bleak`), for a PC whose browser cannot reach the device. It can pair
  with the device, which a browser cannot.
- [`tools/ha_addon.py`](tools/ha_addon.py) — log in to Home Assistant from the command line and read
  this app's state and log through the Supervisor, for diagnosing without the UI.

## 🔧 Under the hood

[ARCHITECTURE.md](ARCHITECTURE.md) maps the packages and traces a device request through them;
[AGENTS.md](AGENTS.md) summarises the protocol invariants a contributor must not break;
[CONTRIBUTING.md](CONTRIBUTING.md) covers captures, adding a model and what a capture records
about your network. The new piece in this build is
[`http/tls_mux.py`](addon/petkit_local/http/tls_mux.py), the TLS front on port 443; its docstring
says why it exists and what it decides.

## 🙌 Origin, credits and license

This is a fork of **[alex-so-3/petkit-local](https://github.com/alex-so-3/petkit-local)** by
alex-so-3, which did the work this builds on: the protocol, the entity model, the media pipeline,
the patchers and the panel. The changes here are the ESP32 path (the TLS front, the flat
credentials, the provisioning fixes), the shutdown behaviour, the published image and the
documentation of the DNS and Bluetooth findings. Upstream's own credits carry over:
[dwyschka/localkit](https://github.com/dwyschka/localkit),
[Jezza34000/homeassistant_petkit](https://github.com/Jezza34000/homeassistant_petkit) and
[aavdberg/ha-petkit](https://github.com/aavdberg/ha-petkit).

[GPL-3.0-or-later](LICENSE), as upstream. Free to use, study, modify and redistribute; a modified
version you distribute stays under the same terms with its source available.
