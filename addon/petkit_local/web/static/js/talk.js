import { BASE, api, toast } from './core.js';
import { onAction } from './delegate.js';

// ---------------- Two-way talk from the browser ----------------
//
// The server half has existed since 2.1.0 (`web/api/talk.py` and the `talk`
// patcher); this is the browser half it never got. Click to start, click again
// to stop. While a talk is on, the microphone is recorded as webm/opus in
// 250 ms slices and streamed over a WebSocket to `/api/devices/{id}/talk`,
// which transcodes to the speaker's AAC and pushes it to the sink the patcher
// installed.
//
// Why a toggle and not hold-to-talk (which 2.1.9 shipped): the first use asks
// the browser for microphone permission, and answering that prompt means
// releasing the button -- which ended the hold before anything happened, with
// no word about why. A quick click did the same. What hold-to-talk bought,
// that a session cannot outlive the operator's attention, is kept another
// way: every talk ends by itself after TALK_MAX_SECONDS, when the tab is
// hidden, or on Escape. Half-duplex: nothing here mutes the camera audio a
// Home Assistant card may be playing on the same machine.

const TALK_MAX_SECONDS = 20;

let session = null; // the one talk in progress, if any

function status(id, text) {
  const el = document.getElementById('talk-status-' + id);
  if (el) el.textContent = text;
}

async function start(id, btn) {
  if (session) return;
  btn.disabled = true;
  status(id, 'checking…');
  try {
    // Without the patcher's sink the server connects to nothing and drops
    // the audio in silence (its ffmpeg exits at once); say so instead.
    let p = null;
    try {
      p = await api('devices/' + id + '/patcher');
    } catch (e) {
      /* fall through: the server will report the real problem */
    }
    const talk = p && p.patchers && p.patchers.talk;
    if (p && !(talk && talk.applied)) {
      toast('Apply the Two-Way Talk patcher first (Patchers tab).');
      status(id, 'Two-Way Talk patcher not applied');
      return;
    }
    if (!navigator.mediaDevices || !navigator.mediaDevices.getUserMedia) {
      toast('This browser offers no microphone here (needs HTTPS).');
      status(id, 'no microphone access on this page');
      return;
    }
    let stream;
    try {
      status(id, 'asking for the microphone…');
      stream = await navigator.mediaDevices.getUserMedia({ audio: true });
    } catch (e) {
      toast('Microphone not available: ' + e.name);
      status(id, 'microphone refused (' + e.name + ')');
      return;
    }
    const mime = MediaRecorder.isTypeSupported('audio/webm;codecs=opus')
      ? 'audio/webm;codecs=opus'
      : 'audio/webm';
    const proto = location.protocol === 'https:' ? 'wss:' : 'ws:';
    const ws = new WebSocket(proto + '//' + location.host + BASE + 'api/devices/' + id + '/talk');
    const s = {
      id,
      ws,
      stream,
      rec: null,
      btn,
      label: btn.textContent,
      left: TALK_MAX_SECONDS,
      timer: null,
    };
    session = s;
    btn.textContent = '● connecting…';
    status(id, '');
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
      const tick = () => {
        if (session !== s) return;
        btn.textContent = '■ Stop talking (' + s.left + ' s)';
        if (s.left <= 0) {
          stop();
          return;
        }
        s.left -= 1;
        s.timer = setTimeout(tick, 1000);
      };
      tick();
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
        status(id, m.msg || 'talk failed');
        stop();
      }
    };
    ws.onerror = () => {
      toast('Talk connection failed.');
      status(id, 'connection failed');
      stop();
    };
    ws.onclose = () => {
      if (session === s) stop();
    };
  } finally {
    btn.disabled = false;
  }
}

function stop() {
  const s = session;
  if (!s) return;
  session = null;
  if (s.timer) clearTimeout(s.timer);
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
}

onAction('talk-ptt', btn => {
  if (session) {
    stop();
    return;
  }
  start(Number(btn.dataset.id), btn);
});

// A talk must not outlive the page it was started from.
document.addEventListener('visibilitychange', () => {
  if (document.hidden) stop();
});
document.addEventListener('keydown', ev => {
  if (ev.key === 'Escape') stop();
});
