import { BASE, api, toast } from './core.js';

// ---------------- Two-way talk: push-to-talk from the browser ----------------
//
// The server half has existed since 2.1.0 (`web/api/talk.py` and the `talk`
// patcher); this is the browser half it never got. Hold the button, speak,
// release. While held, the microphone is recorded as webm/opus in 250 ms slices
// and streamed over a WebSocket to `/api/devices/{id}/talk`, which transcodes
// to the speaker's AAC and pushes it to the sink the patcher installed.
//
// Why not click-to-toggle: the sink plays whatever arrives and nothing else
// stops it, so a talk session that outlives the operator's attention would be
// an open microphone into somebody's kitchen. Releasing the pointer ends it,
// and so does losing the pointer (capture is requested so a release outside
// the button still counts). Half-duplex: nothing here mutes the camera audio
// a Home Assistant card may be playing on the same machine.

let session = null; // the one talk in progress, if any
let pressed = false; // pointer still down? checked after each await in start()

function status(id, text) {
  const el = document.getElementById('talk-status-' + id);
  if (el) el.textContent = text;
}

async function start(id, btn) {
  if (session) return;
  pressed = true;
  // Without the patcher's sink the server connects to nothing and drops the
  // audio in silence (its ffmpeg exits at once); say so instead.
  let p = null;
  try {
    p = await api('devices/' + id + '/patcher');
  } catch (e) {
    /* fall through: the server will report the real problem */
  }
  if (!pressed) return;
  const talk = p && p.patchers && p.patchers.talk;
  if (p && !(talk && talk.applied)) {
    toast('Apply the Two-Way Talk patcher first (Patchers tab).');
    return;
  }
  let stream;
  try {
    stream = await navigator.mediaDevices.getUserMedia({ audio: true });
  } catch (e) {
    toast('Microphone not available: ' + e.name);
    return;
  }
  if (!pressed) {
    stream.getTracks().forEach(t => t.stop());
    return;
  }
  const mime = MediaRecorder.isTypeSupported('audio/webm;codecs=opus')
    ? 'audio/webm;codecs=opus'
    : 'audio/webm';
  const proto = location.protocol === 'https:' ? 'wss:' : 'ws:';
  const ws = new WebSocket(proto + '//' + location.host + BASE + 'api/devices/' + id + '/talk');
  const s = { id, ws, stream, rec: null, btn, label: btn.textContent };
  session = s;
  btn.textContent = '● connecting…';
  ws.onopen = () => {
    if (session !== s) return;
    ws.send(JSON.stringify({ type: 'talk_start' }));
    s.rec = new MediaRecorder(stream, { mimeType: mime, audioBitsPerSecond: 32000 });
    s.rec.ondataavailable = ev => {
      if (ev.data.size && ws.readyState === WebSocket.OPEN) {
        ev.data.arrayBuffer().then(buf => {
          if (ws.readyState === WebSocket.OPEN) ws.send(buf);
        });
      }
    };
    s.rec.start(250);
    btn.textContent = '● talking — release to stop';
    status(id, '');
  };
  ws.onmessage = ev => {
    let m;
    try {
      m = JSON.parse(ev.data);
    } catch (e) {
      return;
    }
    if (m.type === 'error') {
      toast(m.msg || 'talk failed');
      stop();
    }
  };
  ws.onerror = () => {
    toast('Talk connection failed.');
    stop();
  };
  ws.onclose = () => {
    if (session === s) stop();
  };
}

function stop() {
  pressed = false;
  const s = session;
  if (!s) return;
  session = null;
  try {
    if (s.rec && s.rec.state !== 'inactive') s.rec.stop();
  } catch (e) {
    /* already stopped */
  }
  s.stream.getTracks().forEach(t => t.stop());
  try {
    if (s.ws.readyState === WebSocket.OPEN) {
      s.ws.send(JSON.stringify({ type: 'talk_stop' }));
      // Give the stop a moment to reach the server before the socket goes.
      setTimeout(() => s.ws.close(), 300);
    } else {
      s.ws.close();
    }
  } catch (e) {
    /* socket already gone */
  }
  s.btn.textContent = s.label;
  status(s.id, '');
}

document.addEventListener('pointerdown', ev => {
  const btn = ev.target.closest('[data-action="talk-ptt"]');
  if (!btn) return;
  ev.preventDefault();
  if (btn.setPointerCapture) {
    try {
      btn.setPointerCapture(ev.pointerId);
    } catch (e) {
      /* capture is best effort */
    }
  }
  start(Number(btn.dataset.id), btn);
});
for (const type of ['pointerup', 'pointercancel']) {
  document.addEventListener(type, () => {
    if (pressed || session) stop();
  });
}
// A page that disappears mid-talk (tab switch, navigation) must not leave the
// microphone or the sink open.
document.addEventListener('visibilitychange', () => {
  if (document.hidden) stop();
});
