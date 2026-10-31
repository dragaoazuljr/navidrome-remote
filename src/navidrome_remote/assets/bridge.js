/* navidrome-remote bridge — DEFINITIVE (A7)
 * Anchor: Navidrome UI v0.64.2  ·  version 0.4.0-a7
 *
 * Injected by the proxy (inject.py) immediately before </head>: a plain
 * script, executed BEFORE the official SPA bundle, zero dependencies,
 * ES5. The bridge turns the browser into a CONTROL SURFACE for the SBC:
 *
 *   INTERCEPTION (user -> SBC)
 *     * transport: every media-element load of a Navidrome stream URL
 *       ((/rest/)?stream?id=...) is swapped for a generated SILENT feed
 *       (the browser plays silence — no local audio, zero upstream
 *       bandwidth) and the song id is shipped to POST /api/v1/play;
 *     * play / pause: the media-element `play`/`pause` events fired by
 *       the official player's own button handlers -> /resume | /pause;
 *     * seek: the seek slider (input[type=range], max = seconds) and any
 *       programmatic currentTime jump > 1 s -> /seek {pos};
 *     * volume: the volume slider (max <= 200) -> /volume {level 0..100};
 *     * next / prev: button clicks -> /next | /prev when the engine
 *       holds a multi-item playlist (routing decided against a FRESH
 *       engine read — one cheap GET /status before the click, so a
 *       queue seeded by anyone else between two poll ticks is
 *       honored); /play {id} stays the universal fallback (a queueless
 *       single-track engine has no "next");
 *     * shuffle / repeat: toggles are derived from THE ENGINE (fresh
 *       GET /status per press: the DOM flag is still pre-toggle at
 *       capture time and the polled shadow may lag one cycle)
 *       -> /shuffle {value} | /repeat {mode};
 *     * stop -> /stop.
 *   REVERSE SYNC (SBC -> user)
 *     * ADAPTIVE polling of GET /status (ADR-002: v0.64.2 has NO push
 *       channel; ADR-005 adapts the period to the last observed
 *       state, in precedence order): 5 s hidden · 1 s doubling to a
 *       10 s cap on transport-failure streaks · 0.5 s fast ticks (x4)
 *       after any state-vector change (how EXTERNAL commands reflect
 *       quickly) · 3 s engine-down · 2 s playing stable · 3 s
 *       paused/idle stable.
 *     * the VISIBLE STATE (track, position, duration, volume,
 *       play/pause) is projected onto the native player's media
 *       element (shadow clock + guarded writes) and, on a
 *       current-track change coming from the engine alone, the native
 *       track-switch pipeline is re-triggered with a synthetic id so
 *       "Now Playing" follows the engine. With the badge, this
 *       ABSORBS the v1 spec's dedicated status panel — band/time/
 *       position live in the official UI (component C: no fork, no
 *       extra surface).
 *     * SILENT FAILURES: transport-class poll failures (SBC offline,
 *       5xx) never degrade anything nor touch an element — only the
 *       backoff grows, and the discrete "SBC unreachable" hint shows
 *       after 15 s of sustained failure; one success clears both.
 *       Session verdicts (401/403) are NOT transport failures: the
 *       gate adjudicates them (T12).
 *   GATES & DEGRADATION (NVR-2: graceful degradation + version gate)
 *     * PASSIVE: no Navidrome session in the page (pre-login) — the
 *       bridge does nothing and shows nothing (spec: acts only with an
 *       active session; the wire-level spy still covers that interval).
 *     * ANCHOR DRIFT: the page declares a Navidrome version that
 *       differs from this build's anchor — no interception (the UI
 *       stays native, fully functional) + anchor-drift beacon (the
 *       A2 upgrade-gate correlates it).
 *     * GATE DENIED: the service gate rejects the session (401
 *       session-invalid / session-unverifiable, 403
 *       service-account-forbidden) — interception reverted, native
 *       playback restored (original stream URL, unmuted), a badge
 *       explains the state, a periodic re-probe re-engages when the
 *       session becomes valid again.
 *     * FATAL: any unexpected internal error reverts to native mode
 *       (the bridge must never break the official UI) + beacon.
 *     * ENGINE DOWN: the engine is down but the session is valid — the
 *       bridge STAYS engaged (commands are recorded by the service
 *       store) and shows a distinct "engine offline" hint.
 *     * POLL FAILURE: a transport-class poll failure (SBC offline
 *       mid-session) is SILENT — the bridge stays engaged and backs
 *       the poll off (1 s doubling to 10 s); after 15 s sustained,
 *       a discrete "SBC unreachable" hint shows; one success clears
 *       both (ADR-005).
 *
 * AUTHORIZATION (A3 gateway contract): every /api/v1/* call carries the
 * PAGE'S OWN session — v0.64.2 parks its native JWT in localStorage
 * (key `token`; A2 boot capture proved the scheme: no cookies). The
 * bridge presents it in the Authorization header (the gateway's
 * strongest channel; the L3 probe rides it) and, for the beacon pad
 * only, as a `jwt` body field (sendBeacon cannot set headers — the
 * server redacts it at the spy chokepoint). The relay is to the SAME
 * ORIGIN that issued the JWT: neutral by construction.
 *
 * CREDENTIAL INVARIANT: this file NEVER reads, echoes, persists or
 * transmits the Subsonic u/t/s/p values (they stay where the official
 * UI keeps them, for the UI's own legitimate /rest/* metadata traffic).
 * Stream URLs are held in RAM only (weak-map), solely to be SWAPPED OUT
 * or RESTORED verbatim on degradation. No secret shape ever enters a
 * beacon, the badge or the console (sanitized before emission).
 *
 * No file of ui/ is modified (component C: sem fork).
 */
(function () {
  "use strict";
  if (window.__NVRA_BRIDGE__) {
    return; /* injected exactly once (idempotent) */
  }

  /* ------------------------------------------------------------- consts */
  var ANCHOR = "v0.64.2";
  var VERSION = "0.4.0-a7";
  var API = "/api/v1/";
  var LS_TOKEN_KEYS = ["token", "navidrome_token"]; /* v0.64.2: "token" */

  /* A7 (ADR-005) — adaptive poll cadence. Precedence in
   * nextPollDelay(): hidden > transport backoff > transition fast
   * ticks > engine down > playing > paused/idle. */
  var POLL_HIDDEN_MS = 5000;        /* hidden tab (budget) */
  var POLL_FAIL_STEP_MS = 1000;     /* backoff base (doubles) */
  var POLL_BACKOFF_MAX_MS = 10000;  /* backoff cap */
  var POLL_HINT_AFTER_MS = 15000;   /* sustained failure -> hint */
  var POLL_FAST_MS = 500;           /* transition mode */
  var TRANS_FAST_TICKS = 4;         /* fast ticks per change edge */
  var POLL_DOWN_MS = 3000;          /* engine down, session valid */
  var POLL_PLAY_MS = 2000;          /* playing stable */
  var POLL_PAUSED_MS = 3000;        /* paused / idle stable */
  var REPROBE_DEGRADED_MS = 30000;
  var SESSION_WATCH_MS = 500;
  var SYNTH_TICK_MS = 1000;
  var INTENT_DUP_MS = 300;
  var SEEK_JUMP_MIN = 1.0; /* seconds counted as a user seek */

  var STATE_PASSIVE = "passive";
  var STATE_ENGAGED = "engaged";
  var STATE_DEGRADED = "degraded";

  /* Route table (kept as data: tests lint this exact surface). */
  var ROUTES = {
    play: "/play",
    pause: "/pause",
    resume: "/resume",
    next: "/next",
    prev: "/prev",
    stop: "/stop",
    seek: "/seek",
    shuffle: "/shuffle",
    repeat: "/repeat",
    volume: "/volume",
    status: "/status" /* GET */
  };

  /* ----------------------------------------------- session/version read */
  function pageJwt() {
    try {
      for (var i = 0; i < LS_TOKEN_KEYS.length; i++) {
        var v = null;
        try { v = localStorage.getItem(LS_TOKEN_KEYS[i]); } catch (_e) { }
        if (v && v.split(".").length === 3) {
          return v;
        }
      }
    } catch (_e) { /* storage unavailable — treated as anonymous */ }
    return null;
  }

  function hasSession() {
    var jwt = pageJwt();
    if (!jwt) {
      return false;
    }
    try {
      var ia = localStorage.getItem("is-authenticated");
      if (ia === "true" || ia === null) {
        return true; /* token present: the SPA treats it as a session */
      }
    } catch (_e) { }
    return !!jwt;
  }

  function pageVersion() {
    try {
      var cfg = window.__APP_CONFIG__;
      if (cfg) {
        if (typeof cfg.version === "string") {
          return cfg.version;
        }
        var m = String(cfg).match(/["']?version["']?\s*[:=]\s*["']([^"']+)/);
        if (m) {
          return m[1];
        }
      }
    } catch (_e) { }
    return null;
  }

  function anchorMatch(v) {
    if (!v) {
      return true; /* unknown version: informational, do not break (A1 rule) */
    }
    return v.replace(/^v/, "") === ANCHOR.replace(/^v/, "");
  }

  /* ------------------------------------- silent feed (generated in RAM) */
  function buildSilenceWav() {
    /* 8 kHz, 16-bit mono, 10 s of digital zero — valid WAV the media
     * pipeline accepts; the user hears NOTHING with it, and the element
     * stays "playing" (looped) so no `ended` cascades into the UI's
     * auto-advance logic. 10 s (not the original 0.25 s): each loop
     * pass re-arms the element (seek churn) and opens a micro-window in
     * which raw `paused` can read true — a user play/pause toggle that
     * lands there mis-branches (T3 loss mode). Stretching the pad to
     * 10 s makes that window 1-per-10s instead of ~4-per-second.
     * Still generated (never shipped), still ~160 KB in RAM only. */
    var sr = 8000, secs = 10, n = Math.floor(sr * secs);
    var buf = new ArrayBuffer(44 + n * 2);
    var dv = new DataView(buf);
    function wstr(off, s) {
      for (var i = 0; i < s.length; i++) {
        dv.setUint8(off + i, s.charCodeAt(i));
      }
    }
    wstr(0, "RIFF");
    dv.setUint32(4, 36 + n * 2, true);
    wstr(8, "WAVEfmt ");
    dv.setUint32(16, 16, true); /* PCM fmt block size */
    dv.setUint16(20, 1, true); /* PCM */
    dv.setUint16(22, 1, true); /* mono */
    dv.setUint32(24, sr, true);
    dv.setUint32(28, sr * 2, true); /* byte rate */
    dv.setUint16(32, 2, true); /* block align */
    dv.setUint16(34, 16, true); /* bits */
    wstr(36, "data");
    dv.setUint32(40, n * 2, true);
    /* data bytes are already zero in the fresh ArrayBuffer */
    var bytes = new Uint8Array(buf);
    var bin = "";
    var CHUNK = 8192;
    for (var i = 0; i < bytes.length; i += CHUNK) {
      bin += String.fromCharCode.apply(null, bytes.subarray(i, i + CHUNK));
    }
    return "data:audio/wav;base64," + btoa(bin);
  }

  /* ------------------------------------------------------- url classify */
  var STREAM_PATH_RX = /(?:^|\/)(?:rest\/)?stream(?:$|[?#])/;
  function parseIdFromQuery(q) {
    if (!q) {
      return null;
    }
    /* `id` can lead the query string (callers strip the `?`) or follow a
       separator — accept both shapes. */
    var m = q.match(/^id=([^&]+)/) || q.match(/[?&]id=([^&]+)/);
    if (m) {
      try {
        return decodeURIComponent(m[1]);
      } catch (_e) {
        return m[1];
      }
    }
    return null;
  }

  /**
   * Classify a media-source URL. Returns:
   *   {kind:"stream", id}  -> a Navidrome stream (we intercept it);
   *   {kind:"internal"}    -> a synthetic re-trigger URL (we intercept,
   *                            ship NOTHING);
   *   null                 -> anything else (native passthrough).
   */
  function classifyMediaUrl(url) {
    if (!url || typeof url !== "string") {
      return null;
    }
    if (url.indexOf("data:") === 0 || url.indexOf("blob:") === 0) {
      return null;
    }
    if (url.indexOf("__nvr_synth=1") >= 0) {
      return { kind: "internal", id: parseIdFromQuery(url.split("?")[1] || "") };
    }
    if (!STREAM_PATH_RX.test(url)) {
      return null;
    }
    var q = url.indexOf("?") >= 0 ? url.slice(url.indexOf("?") + 1) : "";
    var id = parseIdFromQuery(q);
    return { kind: "stream", id: id };
  }

  /* ------------------------------------------------------- beacon utils */
  var _SECRET_RX = [
    /eyJ[A-Za-z0-9_-]{6,}\.[A-Za-z0-9_-]{6,}\.[A-Za-z0-9_-]{2,}/, /* JWT */
    /\bbearer\s+[A-Za-z0-9._~+/=-]{12,}/i,
    /\b[\w.-]{1,32}:[0-9a-fA-F]{32,}\b/, /* user:hexcred */
    /[?&](?:u|t|s|p|jwt)=(?:[^&]{16,})/ /* cred params in a URL */
  ];
  function sanitize(s) {
    var out = String(s == null ? "" : s);
    for (var i = 0; i < _SECRET_RX.length; i++) {
      out = out.replace(_SECRET_RX[i], "[red]");
    }
    return out;
  }

  var beaconLast = {};
  function beacon(kind, detail) {
    try {
      var now = Date.now();
      var last = beaconLast[kind] || 0;
      var cool = (kind === "bridge-engaged" || kind === "bridge-degraded") ? 2000 : 60000;
      if (now - last < cool) {
        return;
      }
      beaconLast[kind] = now;
      var obj = { k: String(kind).slice(0, 64), d: sanitize(String(detail || "")).slice(0, 512) };
      var jwt = pageJwt();
      if (jwt) {
        obj.jwt = jwt; /* body channel (sendBeacon); server redacts */
      }
      var body = JSON.stringify(obj);
      if (navigator.sendBeacon) {
        /* the A2 spy pad keeps its FIXED literal path (outside the
           intent API vocabulary); the server redacts it at the
           chokepoint. */
        navigator.sendBeacon("/api/v1/spy", body);
      }
    } catch (_e) { /* tracing must never break the UI */ }
  }

  /* ================================================================ core */
  var bridge = {
    svc: "navidrome-remote",
    phase: "M1",
    anchor: ANCHOR,
    version: VERSION,
    bootedAt: new Date().toISOString(),
    url: location.href,
    state: STATE_PASSIVE,
    degradedReason: null,
    lastStatus: null,
    engineDown: false,

    /* test/inspection surface (A6 E2E drives through this) */
    _t: null,

    stats: {
      engagements: 0,
      degradations: 0,
      recoveries: 0,
      intents: 0,
      polls: 0,
      pollsFailed: 0,
      guardWrites: 0
    },

    /* ---------------------------------------------------- auth failures */
    onApiAuthFailure: function (status, bodyObj, route) {
      var code = bodyObj && bodyObj.error ? bodyObj.error : null;
      if (status === 401 && code === "session-denied-recent") {
        return; /* rate protection (10 s dedupe): not a session verdict */
      }
      if (status === 401 || status === 403) {
        var why = code ? "gate:" + code : "gate:http-" + status;
        /* service-account forbidden, or a dead session: revert to native */
        degrade(why, route);
      }
    }
  };

  /* ------------------------------------------------- shadow player state */
  var shadow = {
    pos: 0,
    dur: null,
    playing: false,
    paused: false,
    volume: null,
    currentId: null,
    lastTickReal: null
  };

  var elMeta = new WeakMap(); /* el -> {origUrl,lastNativeUrl,silent,mutedSaved,loopSaved,shadowPos,shadowDur,guardSeq} */

  /* The guard must be STICKY only for as long as load-algorithm artifacts
     can arrive; a global seq that survives indefinitely would eat genuine
     user play/pause events that land right after our own write. TTL it. */
  var guardSince = 0;
  var lastClickAt = 0; /* any user click while engaged (settle gate) */
  /* ms — how long an event can ride on OUR OWN last guarded write.
     A6.2: tuned WELL BELOW the 1 s status-poll cycle. The reverse-sync
     projection touches (and re-stamps) the element on every poll, so a
     TTL approaching the poll period classifies REAL user gestures
     (landed within 750 ms of a poll write) as our own and eats them —
     observed: a user /pause silently dropped (T3). Genuine reactions
     to our writes (load artifacts, play/pause from el.play()) arrive
     in tens of ms; anything slower belongs to whoever acted last
     (the user). Load-swaps keep their own, wider `switching` window. */
  var GUARD_EVENT_TTL = 150;
  var lastSwappedId = null;
  var lastIntents = {}; /* route -> {t,key} */
  var queueHi = 0; /* highest queue size ACKNOWLEDGED since a full clear */
  var lastEngageAt = null; /* page birth stamp: settle gate for echoes */
  var SETTLE_MS = 3000;
  var gateLog = []; /* decision audit (test seam only) */
  var idleStreak = 0; /* consecutive proven-idle polls (reset gate) */
  var idleTripleStreak = 0; /* A6.R (T10) — consecutive polls reporting
                               the STOP picture (connected + idle + no
                               activity); drives the element's stop
                               projection */
  var adjWindow = null; /* {at} stamped when WE ship /next|/prev */
  var pendingSwap = null; /* {id} a swap /play deferred behind an adjacent intent */
  var swapGraceTimer = null;
  var intentRing = []; /* {route,payload,t,http} capped 32 (tests) */
  var guardActive = false;
  var guardSeq = 0;
  var seekDragLock = false;
  var playRetry = null; /* last /play intent awaiting an engine-up window */
  var lastPlayBtnPress = null; /* A6.2 — last REAL press on a play-family button (ms) */
  var PLAY_GHOST_GRACE_MS = 3000; /* A6.2 — ghost-resume grace after a user /pause */
  var lastPauseDirAt = null; /* A6.2 — last USER /pause intent we shipped (ms) */
  var lastResumeDirAt = null; /* A6.2 — last USER /resume|/play intent (ms) */
  var DIR_STALE_MS = 3000; /* A6.2 — stale-poll horizon behind a user direction
                              change; A7: recalibrated 2500->3000 to cover the
                              widest visible cadence (3 s) + margin (ADR-005). */
  var PRESS_AFTER_SWITCH_MS = 100; /* A6.2 — press this far after the swap is a SEPARATE gesture */
  /* A7 (ADR-005) — adaptive-cadence bookkeeping: prevVec (comparator
   * baseline), pollFailStreak/pollFailSince (failure backoff),
   * fastTicks (owed 0.5 s ticks), pollTimes (tick-log seam). */
  var prevVec = null;
  var pollFailStreak = 0;
  var pollFailSince = 0;
  var fastTicks = 0;
  var pollTimes = [];
  var timers = { poll: null, tick: null, watch: null, reprobe: null, engage: null };

  /* -------------------------------------------------------- guard write */
  function guardBegin() {
    guardActive = true;
    guardSeq++;
    guardSince = Date.now();
    bridge.stats.guardWrites++;
  }
  function guardEnd() {
    guardActive = false;
  }
  function isOurEvent(el) {
    var meta = elMeta.get(el);
    if (!meta || !meta.guardSeq || meta.guardSeq !== guardSeq) {
      return false;
    }
    /* A6.2 F6b — causality beat. Our own load-artifact reactions arrive
       WITHOUT any user gesture in between (tens of ms, nothing else
       touched the page). If a user click on ANY control landed AFTER the
       stamp that owns this event, the event belongs to the user — no
       matter how tight the TTL window is (a real pause landing 150 ms
       behind a poll re-stamp used to die here, T3 loss mode). */
    if (lastClickAt > guardSince) {
      return false;
    }
    /* sticky only for the artifact window; after that, events belong to
       whoever acted on the element (usually the user). */
    return Date.now() - guardSince <= GUARD_EVENT_TTL;
  }
  function stampGuard(el) {
    var meta = elMeta.get(el);
    if (meta) {
      meta.guardSeq = guardSeq;
    }
  }

  /* --------------------------------------------------------- api call */
  function recordIntent(route, payload, http) {
    intentRing.push({ route: route, payload: payload, t: Date.now(), http: http });
    if (intentRing.length > 32) {
      intentRing.shift();
    }
  }

  function dedupeAllowed(route, payload) {
    var now = Date.now();
    var key = JSON.stringify(payload || {});
    var prev = lastIntents[route];
    if (prev && now - prev.t < INTENT_DUP_MS && prev.key === key) {
      return false;
    }
    lastIntents[route] = { t: now, key: key };
    return true;
  }

  function apiCall(route, payload, isGet) {
    var opts = {};
    /* `API` ends with a slash; strip any leading one from the route so
       the two halves never produce "//" (route forms vary by call site). */
    var r = String(route == null ? "" : route);
    if (r.charAt(0) === "/") {
      r = r.slice(1);
    }
    return doFetch(isGet ? "GET" : "POST", API + r, payload, opts).then(function (res) {
      if (!isGet) {
        recordIntent(route, payload || {}, res.status);
      }
      return res;
    });
  }

  function doFetch(method, url, payload, opts) {
    opts = opts || {};
    var headers = { "Content-Type": "application/json" };
    var jwt = pageJwt();
    if (jwt) {
      headers["Authorization"] = "Bearer " + jwt;
    }
    var init = { method: method, credentials: "same-origin", headers: headers };
    if (method !== "GET") {
      init.body = JSON.stringify(payload || {});
    }
    return fetch(url, init).then(function (resp) {
      return parseJsonResp(resp).then(function (bodyObj) {
        if (opts.tag !== "status-poll" && !opts.noGateHandle) {
          bridge.onApiAuthFailure(resp.status, bodyObj, url);
        }
        return { status: resp.status, ok: resp.ok, body: bodyObj };
      });
    }).catch(function (err) {
      return { status: 0, ok: false, body: null, error: String(err && err.message || err) };
    });
  }

  function parseJsonResp(resp) {
    return resp.clone().text().then(function (txt) {
      if (!txt) {
        return null;
      }
      try {
        return JSON.parse(txt);
      } catch (_e) {
        return null;
      }
    }).catch(function () {
      return null;
    });
  }

  /* ------------------------------------------------------------ intents */
  function sendIntent(route, payload) {
    if (getState() !== STATE_ENGAGED) {
      return Promise.resolve({ status: 0, ok: false, body: null, suppressed: "not-engaged" });
    }
    if (!dedupeAllowed(route, payload)) {
      return Promise.resolve({ status: 0, ok: false, body: null, suppressed: "dup" });
    }
    /* A6.2 — direction memory: a USER direction change (shipped through
       this funnel, whichever event fired it) is authoritative against
       stale polls for DIR_STALE_MS (see the projection lock below). */
    if (route === "/pause") {
      lastPauseDirAt = Date.now();
    } else if (route === "/resume" || route === "/play") {
      lastResumeDirAt = Date.now();
    }
    bridge.stats.intents++;
    return apiCall(route, payload).then(function (res) {
      if (res.status === 503 && res.body && res.body.error === "engine-down") {
        bridge.engineDown = true;
        if (route === "/play") {
          playRetry = { payload: payload, at: Date.now() };
        }
        updateHint();
      }
      if (res.status === 503 && res.body && res.body.error === "engine-idle") {
        /* transient: command raced the track load — drop quietly */
        recordIntent(route, payload || {}, 503);
      }
      return res;
    });
  }

  var ADJ_WINDOW_MS = 800; /* the SPA swap fires inside our own click */
var SWAP_GRACE_MS = 2000; /* defer ceiling for the postponed /play */

function resolvePendingSwap() {
  swapGraceTimer = null;
  var p = pendingSwap;
  pendingSwap = null;
  if (!p) {
    return;
  }
  if (shadow && shadow.currentId === p.id) {
    return; /* engine already on the target: the adjacent intent did
               its job; a /play here would only re-flatten the deck */
  }
  /* the adjacent intent did not move the engine to the swapped id:
     deliver the deferred selection now (the fallback is the law). */
  sendIntent("/play", { id: p.id });
}

function handlePlaySwap(meta, cls) {
  if (cls.kind === "internal") {
    return; /* engine-driven re-trigger: no intent ships */
  }
  var id = cls.id;
  if (!id) {
    return; /* unparsable id: keep it silent, ship nothing */
  }
  if (id === lastSwappedId && meta.swappedAlready) {
    return; /* re-load of the SAME song (stall retry) — no double play */
  }
  /* Adjacency deferral: a source swap arriving inside our OWN
     /next|/prev window is the SPA's half of that gesture. On a
     multi-item deck the engine step is the PRIMARY intent — a /play
     here would flatten the deck (A3 contract) and starve any
     follow-up adjacent press of the multi view. Deferred: nothing
     ships if the engine ends on the swapped id; otherwise the
     postponed /play lands after the grace. Swaps OUTSIDE the window
     (ordinary track selections, engine-driven replays) behave
     exactly as before. */
  var inAdj = (adjWindow != null
    && (Date.now() - adjWindow.at) <= ADJ_WINDOW_MS
    && queueHi > 1
    && (shadow == null || shadow.currentId != id));
  if (gateLog.length < 64) gateLog.push({ t: Date.now(), id: id, r: "pass", sh: shadow && shadow.currentId != null ? shadow.currentId : null });
  lastSwappedId = id;
  meta.swappedAlready = true;
  /* SAME-ID ECHO: a source swap to the track the ENGINE ALREADY HOLDS
     (page re-setting its src after navigation/init, SPA bounce-back)
     is an echo, not a user selection: shipping it as /play would be a
     STRUCTURAL REPLACEMENT (A3) flattening any seeded queue. 'Restart'
     semantics of re-picking the current track belong to seek(0), M1.
     Swallow. */
  if (shadow != null && shadow.currentId != null
      && String(id) === String(shadow.currentId)) {
    if (gateLog.length < 64) gateLog.push({ t: Date.now(), id: id, r: "sameid" });
    return;
  }
  /* SETTLE: the seconds after a page (re)engages are dominated by the
     page announcing its INITIAL source — before the first poll has
     synchronized the shadow at all. A NO-CLICK swap with an unknown
     shadow inside that window is an echo (the service already holds the
     current track): shipping it as /play is a structural replacement
     (A3) that can flatten a freshly seeded queue. A swap IMMEDIATELY
     AFTER A USER CLICK (a gesture whose only visible footprint is the
     src change) stays live — that is exactly T2's engage-play shape. */
  if (lastEngageAt != null && (Date.now() - lastEngageAt) <= SETTLE_MS
      && (shadow == null || shadow.currentId == null)
      && (lastClickAt === 0 || Date.now() - lastClickAt > 1000)) {
    if (gateLog.length < 64) gateLog.push({ t: Date.now(), id: id, r: "settle", sc: lastClickAt ? Date.now() - lastClickAt : -1 });
    return;
  }
  if (inAdj) {
    pendingSwap = { id: id };
    if (swapGraceTimer == null) {
      swapGraceTimer = setTimeout(resolvePendingSwap, SWAP_GRACE_MS);
    }
    return;
  }
  sendIntent("/play", { id: id });
}
/* -------------------------------------------------- media-element hook */
  var protoPatched = false;
  var raw = {}; /* saved originals */

  function patchPrototypes() {
    if (protoPatched) {
      return;
    }
    protoPatched = true;
    var M = window.HTMLMediaElement;
    if (!M) {
      return;
    }
    raw.src = Object.getOwnPropertyDescriptor(M.prototype, "src");
    raw.srcObject = Object.getOwnPropertyDescriptor(M.prototype, "srcObject");
    raw.currentTime = Object.getOwnPropertyDescriptor(M.prototype, "currentTime");
    raw.duration = Object.getOwnPropertyDescriptor(M.prototype, "duration");
    raw.play = M.prototype.play;
    raw.pause = M.prototype.pause;
    raw.load = M.prototype.load;

    Object.defineProperty(M.prototype, "src", {
      configurable: true,
      get: function () {
        return raw.src.get.call(this);
      },
      set: function (v) {
        handleSrcSet(this, v);
      }
    });
    if (raw.srcObject) {
      var soSet = raw.srcObject.set;
      Object.defineProperty(M.prototype, "srcObject", {
        configurable: true,
        get: raw.srcObject.get,
        set: function (v) {
          if (getState() === STATE_ENGAGED && v && typeof v === "object" && typeof v.url === "string") {
            var cls = classifyMediaUrl(v.url);
            if (cls) {
              armEl(this, cls, v.url);
              return;
            }
          }
          soSet.call(this, v);
        }
      });
    }
    Object.defineProperty(M.prototype, "currentTime", {
      configurable: true,
      get: function () {
        var meta = elMeta.get(this);
        if (meta && meta.silent && meta.shadowPos != null) {
          return meta.shadowPos;
        }
        return raw.currentTime.get.call(this);
      },
      set: function (v) {
        var meta = elMeta.get(this);
        var num = Number(v);
        if (meta && meta.silent && !isNaN(num) && !guardActive && getState() === STATE_ENGAGED) {
          var dp = Math.abs(num - (meta.shadowPos || 0));
          if (dp >= SEEK_JUMP_MIN) {
            /* a user-driven time jump (keyboard seek / slider) — intent */
            meta.shadowPos = num;
            if (dp >= SEEK_JUMP_MIN) {
              sendIntent("/seek", { pos: round1(num) });
            }
          } else {
            meta.shadowPos = num;
          }
          /* do NOT forward to the (silence) element's real clock */
          return;
        }
        raw.currentTime.set.call(this, v);
      }
    });
    Object.defineProperty(M.prototype, "duration", {
      configurable: true,
      get: function () {
        var meta = elMeta.get(this);
        if (meta && meta.silent && meta.shadowDur != null) {
          return meta.shadowDur;
        }
        return raw.duration.get.call(this);
      },
      set: raw.duration.set
    });
    /* setAttribute("src", ...) bypasses the property setter for the LOAD
     * algorithm in some engines — intercept at the method level too.
     * The pristine implementation lives on Element.prototype (inherited). */
    rawSetAttr = window.Element.prototype.setAttribute;
    M.prototype.setAttribute = function (name, value) {
      if (String(name).toLowerCase() === "src" && classifyMediaUrl(String(value))) {
        handleSrcSet(this, String(value));
        return;
      }
      return rawSetAttr.call(this, name, value);
    };
  }
  var rawSetAttr = null;

  function unpatchPrototypes() {
    if (!protoPatched) {
      return;
    }
    protoPatched = false;
    var M = window.HTMLMediaElement;
    if (!M) {
      return;
    }
    Object.defineProperty(M.prototype, "src", raw.src);
    if (raw.srcObject) {
      Object.defineProperty(M.prototype, "srcObject", raw.srcObject);
    }
    Object.defineProperty(M.prototype, "currentTime", raw.currentTime);
    Object.defineProperty(M.prototype, "duration", raw.duration);
    M.prototype.play = raw.play;
    M.prototype.pause = raw.pause;
    M.prototype.load = raw.load;
    /* setAttribute was only SHADOWED on the media prototype: deleting the
     * shadow restores the inherited Element.prototype implementation. */
    try {
      delete M.prototype.setAttribute;
    } catch (_e) {
      M.prototype.setAttribute = rawSetAttr;
    }
    rawSetAttr = null;
    raw = {};
  }

  function handleSrcSet(el, v) {
    var state = getState();
    var cls = state === STATE_ENGAGED ? classifyMediaUrl(v) : null;
    if (!cls) {
      if (raw.src.set) {
        raw.src.set.call(el, v);
      } else {
        el.setAttribute("src", v);
      }
      return;
    }
    armEl(el, cls, v);
  }

  function armEl(el, cls, originalUrl) {
    var meta = elMeta.get(el) || {};
    /* A6.R (T12 / ADR-004 restoration) — the RESTORE TARGET is the
     * element's last REAL stream URL. The synthetic re-trigger URL
     * (__nvr_synth=1, kind:"internal") is a pipeline token, never a
     * source: letting it become the restore target handed the
     * synthetic back VERBATIM into native mode on degradation (the
     * merged T12 leak — the "restored" url still carried __nvr_synth=1,
     * byte-identical). Real streams only; `lastNativeUrl` is the
     * restore-proof copy that no later write can overwrite. */
    if (cls.kind === "stream") {
      meta.origUrl = originalUrl; /* RAM-only (restoration on degradation) */
      meta.lastNativeUrl = originalUrl;
    }
    meta.silent = true;
    if (meta.mutedSaved == null) {
      meta.mutedSaved = el.muted;
    }
    if (meta.loopSaved == null) {
      meta.loopSaved = el.loop;
    }
    meta.guardSeq = ++guardSeq;
    guardSince = Date.now();
    /* A source swap makes the element briefly go playing->stopped
     * (spec: the load algorithm fires `pause`/`emptied`). Those are LOAD
     * artifacts, not user intent — hold a short mute window so they can
     * never translate into a spurious /pause racing our /play. */
    meta.switching = true;
    meta.switchStart = Date.now(); /* A6.2 — separates "this swap's own"
                                     press from a LATER user gesture */
    if (meta.switchT) {
      clearTimeout(meta.switchT);
    }
    meta.switchT = setTimeout(function () {
      meta.switching = false;
    }, 700);
    elMeta.set(el, meta);
    try {
      if (el.tagName === "VIDEO" || el.tagName === "AUDIO") {
        el.muted = true;
        el.loop = true; /* silence loop: no `ended`, no auto-advance */
        if (raw.src.set) {
          raw.src.set.call(el, SILENCE);
        } else {
          el.setAttribute("src", SILENCE);
        }
        el.load && el.load();
      }
    } catch (_e) {
      /* an exotic element — fall through, worst case native behavior */
    }
    handlePlaySwap(meta, cls);
  }

  /* ------------------------------------------- element discovery (lazy) */
  var observed = new WeakSet();
  function ensureElMeta(el) {
    if (!(el instanceof HTMLMediaElement) || observed.has(el)) {
      return;
    }
    observed.add(el);
    if (!elMeta.get(el)) {
      elMeta.set(el, { guardSeq: 0, silent: false });
    }
  }
  function walkRoot(root) {
    if (!root) {
      return;
    }
    if (root.querySelectorAll) {
      var els = root.querySelectorAll("audio,video");
      for (var i = 0; i < els.length; i++) {
        ensureElMeta(els[i]);
      }
    }
  }
  var moArmed = false;
  function armMutationObserver() {
    if (moArmed || !window.MutationObserver) {
      return;
    }
    moArmed = true;
    var mo = new MutationObserver(function (muts) {
      for (var i = 0; i < muts.length; i++) {
        var ns = muts[i].addedNodes;
        for (var j = 0; j < ns.length; j++) {
          var n = ns[j];
          if (n.nodeType !== 1) {
            continue;
          }
          if (n.matches && (n.matches("audio") || n.matches("video"))) {
            ensureElMeta(n);
          }
          walkRoot(n);
        }
      }
    });
    mo.observe(document.documentElement, { childList: true, subtree: true });
  }

  /* -------------------------------------------------- document listeners */
  var listeners = [];
  function on(target, type, fn, opts) {
    target.addEventListener(type, fn, opts || false);
    listeners.push([target, type, fn, opts]);
  }
  function offAll() {
    for (var i = 0; i < listeners.length; i++) {
      var l = listeners[i];
      try {
        l[0].removeEventListener(l[1], l[2], l[3] || false);
      } catch (_e) { }
    }
    listeners = [];
  }

  var AR_NEXT = /(^|[\s-_])(next|siguiente|pr[oó]xima|weiter)([\s_|-]|$)/i;
  var AR_PREV = /(^|[\s-_])(previous|prev|previa|zur[uü]ck)[\s_|-]|pr[eé]vio/i;
  var AR_SHUFFLE = /shuffle|aleatore/i;
  var AR_REPEAT = /repeat|repet/i;
  var AR_STOP = /stop|parar|detener/i;
  var AR_PLAYISH = /^(play|play\/pause|pause|resume)/i;

  /* Decision aid for GESTURE-driven intents: the last-polled status can
   * be STALE relative to a click that follows an EXTERNAL change inside
   * one poll cycle (an operator seeding the queue, a command from
   * another device): with a 1 s poll the shadow may lag the engine by up
   * to a second, and a stale-derived or repeated key then vanishes into
   * the intent dedup, or routes a "next" into the /play fallback. We pay
   * ONE cheap same-origin GET /status (the SBC plane is loopback/LAN)
   * and fold it into the shadow before deciding; on transport failure
   * we fall back to the last known value. The status-poll tag keeps this
   * fetch OUT of the gate-degrade path: a blip while pressing a button
   * must never degrade the bridge. */
  function withFreshStatus(decide) {
    doFetch("GET", API + "status", null,
            { tag: "status-poll", noGateHandle: true })
      .then(function (res) {
        if (res.status === 200 && res.body) {
          applyStatus(res.body);
        }
        decide();
      })
      .catch(function () {
        decide();
      });
  }

  function labelOf(node) {
    if (!node) {
      return "";
    }
    return [
      node.getAttribute && node.getAttribute("aria-label"),
      node.getAttribute && node.getAttribute("title"),
      node.textContent
    ].filter(Boolean).join(" ") || "";
  }

  function findControl(root) {
    var n = root;
    while (n && n.nodeType === 1) {
      if (n.matches && n.matches("button,[role=button],[aria-label],a,input")) {
        return n;
      }
      n = n.parentElement;
    }
    return null;
  }

  function onClickCapture(evt) {
    if (getState() !== STATE_ENGAGED) {
      return;
    }
    lastClickAt = Date.now();
    var ctl = findControl(evt.target);
    if (ctl && AR_PLAYISH.test(sanitize(labelOf(ctl)))) {
      /* A6.2 — remember a real finger/key press on the play family:
         it legitimizes a resume that lands inside the ghost-resume
         grace window (a quick re-tap is a deliberate restart, not a
         load artifact). */
      lastPlayBtnPress = Date.now();
    }
    if (!ctl) {
      return;
    }
    var lb = sanitize(labelOf(ctl));
    var pressed = ctl.getAttribute("aria-pressed");
    var cls = ctl.className ? String(ctl.className) : "";
    if (AR_NEXT.test(lb) || AR_NEXT.test(cls)) {
      onPressAdjacent(+1);
    } else if (AR_PREV.test(lb)) {
      onPressAdjacent(-1);
    } else if (AR_SHUFFLE.test(lb)) {
      /* Value comes from the ENGINE's authoritative flag, never the DOM
         (the SPA flips aria-pressed in its own TARGET-phase handler,
         after this capture-phase listener runs). The SHADOW too can lag
         the engine by one poll cycle right after OUR OWN previous
         toggle — a repeated key would then die in the intent dedup —
         so the toggle is decided against a FRESH engine read. */
      withFreshStatus(function () {
        sendIntent("/shuffle", { value: !(shadow && shadow.shuffle === true) });
      });
    } else if (AR_REPEAT.test(lb)) {
      /* Same race as shuffle: the 3-state cycle (off -> playlist ->
         track -> off) is derived from the shadow AFTER the fresh
         engine read folded in above — the DOM class is still the
         pre-toggle one at capture time. */
      withFreshStatus(function () {
        sendRepeatFromUi(cls, pressed);
      });
    } else if (AR_STOP.test(lb)) {
      sendIntent("/stop", {}).then(function (r) {
        if (r.ok) {
          /* A6.3 F6d — a successful stop is PROOF the deck is empty:
             clear the identity memory NOW (waiting for the poll proof
             costs up to three cycles) so a later play of the same song
             is a fresh /play, not a swallowed echo/reload. */
          lastSwappedId = null;
          if (shadow) { shadow.currentId = null; }
        }
      });
    }
    /* play/pause buttons: the INTENT arrives through the media element's
     * play/pause events (the native player calls el.play()/el.pause()) —
     * we do not double-fire from the click itself. */
  }

  function onPressAdjacent(delta) {
    /* Multi-item decision against a FRESH engine read, widened by the
     * MONOTONIC queue memory (maintained in applyStatus): the largest
     * acknowledged deck size since a full clear can never be undercut by
     * a click-poisoned view — neither the one in flight (the SPA's own
     * swap ships its /play before our GET is answered) nor the one
     * baked into the last poll after the deck has already fallen. This
     * is what keeps a SECOND adjacent press (/prev right after /next)
     * routed as an engine step: without the memory, the first swap's
     * /play had already flattened the deck below both the fresh and the
     * polled view. A legitimate wipe (deck proven empty at the next
     * poll) resets the memory and restores the universal fallback; an
     * external seed grows it immediately. Transport failure falls back
     * to the last-known views, as before.
     */
    var prevView = (bridge.lastStatus && bridge.lastStatus.queue) || null;
    var prevSize = (prevView && prevView.size != null) ? prevView.size : 0;
    withFreshStatus(function () {
      var q = bridge.lastStatus && bridge.lastStatus.queue;
      var size = (q && q.size != null) ? q.size : 0;
      var best = size > prevSize ? size : prevSize;
      if (queueHi > best) {
        best = queueHi;
      }
      if (best > 1) {
        /* Stamp the adjacency window: the SPA's own target-phase handler
           is about to swap the media source for the neighboring track;
           that swap's /play is DEFERRED behind this intent (handlePlaySwap)
           because on a multi-item deck the engine step is the PRIMARY
           intent — shipping the /play would flatten the deck and starve
           any FOLLOW-UP adjacent press of the multi view. */
        adjWindow = { at: Date.now() };
        sendIntent(delta > 0 ? "/next" : "/prev", {});
      } else {
        /* Universal fallback (queueless engine, per the contract):
           next/prev replays the current song; nothing active means
           silence (no id to ship). */
        var fid = (shadow && shadow.currentId) ? shadow.currentId : null;
        if (fid) {
          sendIntent("/play", { id: fid });
        }
      }
    });
  }
function sendRepeatFromUi(cls, pressed) {
    /* Engine-authoritative 3-state cycle first (off -> playlist ->
       track -> off — the same order the UI icons walk). The DOM class is
       stale in capture phase (see shuffle), so deriving the STEP from
       the engine is the only race-free source. */
    if (shadow && typeof shadow.repeat === "string") {
      var cur = shadow.repeat;
      var wireMode = cur === "off" ? "playlist"
        : (cur === "playlist" ? "track" : "off");
      sendIntent("/repeat", { mode: wireMode });
      return;
    }
    var m = cls.toLowerCase().match(/(rep(?:eat)?[- ]?(off|all|one)|icon[- ](repeat[- ]?(?:off|all|one)|loop[- ]?(?:one|all)))|repeat[- ]?(off|all|one)/);
    var mode = null;
    if (m) {
      var tok = (m[2] || m[3] || "").toLowerCase();
      if (tok.indexOf("off") >= 0 || tok === "") {
        mode = "off";
      } else if (tok.indexOf("one") >= 0) {
        mode = "one"; /* alias for track */
      } else {
        mode = "all"; /* alias for playlist */
      }
      if (mode === "off" && !pressed) {
        mode = "off";
      }
    } else {
      /* unrecognized markup: guess by aria-pressed against last known */
      var cur = bridge.lastStatus ? bridge.lastStatus.repeat : "off";
      mode = cur === "off" ? "all" : (cur === "playlist" ? "one" : "off");
    }
    /* Map the UI's icon vocabulary onto the ENGINE's (A3) vocabulary:
     *   off -> off · all (loop-everything) -> playlist · one (single
     *   song loop) -> track. The gateway/engine only speaks the latter. */
    var wireMode = mode === "all" ? "playlist" : (mode === "one" ? "track" : "off");
    sendIntent("/repeat", { mode: wireMode });
  }

  function onRangeChange(evt) {
    if (getState() !== STATE_ENGAGED) {
      return;
    }
    var el = evt.target;
    if (!el || el.type !== "range") {
      return;
    }
    lastClickAt = Date.now(); /* A6.2 F6b — slider release is a user gesture
                                 (gesture causality beats the write TTL) */
    var max = parseFloat(el.max) || 0;
    var val = parseFloat(el.value) || 0;
    var lb = labelOf(el).toLowerCase();
    var isSeek;
    if (lb.indexOf("seek") >= 0 || lb.indexOf("progress") >= 0 || lb.indexOf("volume") >= 0) {
      isSeek = lb.indexOf("volume") < 0;
    } else {
      isSeek = max > 200; /* seconds-scale vs percentage-scale */
    }
    /* Remember the slider on every range event (a drag fires 'input',
       keyed or programmatic moves often fire only 'change') so the
       reverse sync can project values back onto THIS control. */
    /* 'change' is the RELEASE of a pointer/key gesture: clear the drag
       lock HERE, not only on pointerup — a keyed or synthetic gesture
       may never fire pointerup, and a stuck lock freezes EVERY
       reverse-sync projection (seek/volume/play-pause) for the rest of
       the session (the T11 freeze). */
    seekDragLock = false;
    if (isSeek) {
      if (el !== seekSlider) {
        seekSlider = el;
      }
      sendIntent("/seek", { pos: round1(val) });
    } else {
      if (el !== volSlider) {
        volSlider = el;
      }
      var lvl = max > 0 ? Math.round((val / max) * 100) : Math.round(val);
      sendIntent("/volume", { level: clamp(lvl, 0, 100) });
    }
  }

  function onRangeInput(evt) {
    var el = evt.target;
    if (!el || el.type !== "range") {
      return;
    }
    var lb = labelOf(el).toLowerCase();
    if (lb.indexOf("seek") >= 0 || (lb === "" && (parseFloat(el.max) || 0) > 200)) {
      if (el !== seekSlider) {
        seekSlider = el;
      }
      /* drag lock: our reverse sync must not fight a live gesture */
      seekDragLock = true;
      lastClickAt = Date.now(); /* A6.2 F6b — a live drag is a user gesture */
    } else if (lb.indexOf("volume") >= 0 || lb.indexOf("vol") >= 0) {
      /* loudness control — remembered for the reverse-volume projection
         (the slider's VALUE must follow the engine, not just volume). */
      if (el !== volSlider) {
        volSlider = el;
      }
    }
  }
  function onPointerUpGlobal() {
    /* release after the gesture settles (change fires after release) */
    setTimeout(function () {
      seekDragLock = false;
    }, 250);
  }

  var seekSlider = null;
  var volSlider = null;

  function onElEvent(evt) {
    if (getState() !== STATE_ENGAGED) {
      return;
    }
    var el = evt.target;
    if (!(el instanceof HTMLMediaElement)) {
      return;
    }
    var meta = elMeta.get(el);
    if (!meta || !meta.silent) {
      return; /* foreign/native element: hands off */
    }
    if (isOurEvent(el)) {
      return; /* our own guarded write */
    }
    if (meta.switching && (evt.type === "play" || evt.type === "pause")) {
      /* A6.2 — inside the swap window the default is still SWALLOW
         (the load algorithm's own pause/play artifacts must not race
         our /play). Exception: a play-family press registered AFTER
         the swap began by a perceptible margin is a SEPARATE user
         gesture (it landed during the window, not caused it) — a
         real pause pressed into the 700 ms window is user intent and
         may NOT be eaten (observed T3 loss mode: /pause never ships).
         A press that PRECEDES the swap (the gesture that CAUSED
         it) keeps the old swallow rule. */
      if (lastPlayBtnPress != null && meta.switchStart != null &&
          lastPlayBtnPress >= meta.switchStart + PRESS_AFTER_SWITCH_MS) {
        /* distinct gesture: fall through and honor the event */
      } else {
        return; /* this swap's own artifact — not user intent */
      }
    }
    if (evt.type === "play") {
      /* A6.2 — GHOST-RESUME GUARD. A `play` event that lands right
         AFTER the bridge honored a USER /pause is not user intent: it
         is the element's own machinery replaying it (the silence feed
         re-arming, a loadeddata auto-start, the reverse-sync
         projection answering one sample late). Honoring it resurrects
         the transport the user just stopped (forensics: /resume 83
         ms after a /pause, T3 oscillation). ADR-004 bias: never
         surprise the listener with audio. A deliberate re-tap is
         exempt: a real press on the play-family button (captured
         above) legitimizes a resume inside the grace window. The
         mirror direction (`pause` events) is deliberately untouched —
         eating a user pause is the worse fault. */
      var gi = intentRing.length - 1;
      while (gi >= 0 && intentRing[gi].route !== "/pause") {
        gi--;
      }
      if (gi >= 0 &&
          (Date.now() - intentRing[gi].t) <= PLAY_GHOST_GRACE_MS &&
          (lastPlayBtnPress == null ||
           Date.now() - lastPlayBtnPress > 1000)) {
        return;
      }
      if (bridge.engineDown || (shadow.playing === false && shadow.paused === false && !shadow.currentId)) {
        /* engine has nothing active: this IS the play intent */
        sendIntent("/resume", {}).then(function (r) {
          if (r.status === 503 && r.body && r.body.error === "engine-idle") {
            /* engine truly idle: replay the last known song */
            if (lastSwappedId) {
              sendIntent("/play", { id: lastSwappedId });
            }
          }
        });
      } else {
        sendIntent("/resume", {});
      }
    } else if (evt.type === "pause") {
      sendIntent("/pause", {});
    }
  }

  /* ------------------------------------------------------- state mgmt */
  function getState() {
    return bridge.state;
  }

  function engage(versionSeen) {
    if (bridge.state === STATE_ENGAGED) {
      return;
    }
    try {
      patchPrototypes();
      if (rawSetAttr === null) {
        /* capture the pristine setAttribute (before we override it) */
      }
      armMutationObserver();
      walkRoot(document);
      on(document, "click", onClickCapture, true);
      on(document, "change", onRangeChange, true);
      on(document, "input", onRangeInput, true);
      on(document, "pointerup", onPointerUpGlobal, true);
      on(document, "play", onElEvent, true);
      on(document, "pause", onElEvent, true);
      on(window, "visibilitychange", onVisibility, false);
      on(window, "pagehide", onPageHide, false);
      bridge.state = STATE_ENGAGED;
      bridge.degradedReason = null;
      lastEngageAt = Date.now(); /* once per page: */
      bridge.stats.engagements++;
      beacon("bridge-engaged", "v=" + VERSION + " page=" + (versionSeen || "unknown"));
      /* A7: a fresh engagement starts the adaptive machinery cold */
      prevVec = null;
      pollFailStreak = 0;
      pollFailSince = 0;
      fastTicks = 0;
      pollTimes.length = 0;
      startPolling();
      startTicks();
      updateHint();
    } catch (err) {
      degrade("fatal: " + (err && err.name || "Error"), "engage");
    }
  }

  function degrade(reason, ctx) {
    if (bridge.state === STATE_DEGRADED && bridge.degradedReason === reason) {
      return;
    }
    var prevState = bridge.state;
    try {
      stopTimers();
      offAll();
      /* restore every silenced element BEFORE releasing the prototype
       * patches: the originals (raw.*) are needed to restore the true
       * stream URL. The native player then resumes local playback. */
      restoreAllElements();
      unpatchPrototypes();
      memsetShadow();
      lastSwappedId = null;
      playRetry = null;
      lastPlayBtnPress = null;
      if (swapGraceTimer != null) {
        clearTimeout(swapGraceTimer);
        swapGraceTimer = null;
      }
      pendingSwap = null; /* a degraded bridge owes nothing further */
      adjWindow = null;
      queueHi = 0; /* memory rebuilts by the first polls after re-engage */
      idleStreak = 0;
      idleTripleStreak = 0;
      /* A7: a degraded bridge owes nothing to its adaptive machinery */
      prevVec = null;
      pollFailStreak = 0;
      pollFailSince = 0;
      fastTicks = 0;
      bridge.state = STATE_DEGRADED;
      bridge.degradedReason = reason;
      bridge.engineDown = false;
      bridge.stats.degradations++;
      beacon("bridge-degraded", reason + (ctx ? " ctx=" + ctx : ""));
      if (prevState === STATE_ENGAGED && reason.indexOf("gate:") === 0) {
        /* session-flavor failures can heal (logout/login, re-auth):
         * probe periodically until they do. */
        timers.reprobe = setInterval(probeRecovery, REPROBE_DEGRADED_MS);
      }
    } catch (_e) { /* degradation itself must never throw */ }
    updateHint();
  }

  function restoreAllElements() {
    /* iterate via a snapshot of what we know: we only remember elements
     * that had metadata, so walk the document and consult the WeakMap. */
    var els = document.querySelectorAll("audio,video");
    for (var i = 0; i < els.length; i++) {
      var meta = elMeta.get(els[i]);
      if (meta && meta.silent) {
        meta.silent = false;
        try {
          /* A6.R (T12 / ADR-004) — the re-trigger's id may ride on this
           * element as an ATTRIBUTE; it belongs to the bridge's
           * projection, never to the page. Purge it UNCONDITIONALLY —
           * both "real url restored" and "cleared, never armed" are
           * legitimate states. */
          els[i].removeAttribute("data-nvr-sync-id");
          /* Restoration target, in strict preference order:
           *   1. lastNativeUrl — the last REAL stream URL the element
           *      knew, guaranteed immune to every re-trigger write;
           *   2. origUrl — belt and braces, ACCEPTED only if it is a
           *      non-synthetic url (defense against any path storing
           *      one despite armEl's stream-only rule);
           *   3. null — CLEAR the source: a synthetic __nvr_synth=1
           *      feed must never be "restored" verbatim into native
           *      mode (the merged T12 leak, byte-identical), and an
           *      element that never knew a real url must not keep
           *      looping the silence feed after degradation. */
          var restoreUrl = (meta.lastNativeUrl != null) ? meta.lastNativeUrl
                         : ((meta.origUrl != null
                             && meta.origUrl.indexOf("__nvr_synth=1") < 0)
                            ? meta.origUrl
                            : null);
          if (restoreUrl != null) {
            if (raw.src.set) {
              raw.src.set.call(els[i], restoreUrl);
            } else {
              els[i].setAttribute("src", restoreUrl);
            }
          } else {
            els[i].removeAttribute("src");
          }
          if (meta.mutedSaved != null) {
            els[i].muted = meta.mutedSaved;
          }
          if (meta.loopSaved != null) {
            els[i].loop = meta.loopSaved;
          }
        } catch (_e) { }
      }
    }
  }

  function probeRecovery() {
    doFetch("GET", API + "status", null, { tag: "status-poll", noGateHandle: true })
      .then(function (res) {
        if (res.status === 200) {
          /* session valid again */
          bridge.stats.recoveries++;
          beacon("bridge-recovered", "after " + bridge.degradedReason);
          if (timers.reprobe) {
            clearInterval(timers.reprobe);
            timers.reprobe = null;
          }
          bridge.degradedReason = null;
          engage(pageVersion());
        }
      })
      .catch(function () { });
  }

  /* --------------------------------------------------------- visibility */
  function onVisibility() {
    if (getState() !== STATE_ENGAGED) {
      return;
    }
    restartPollLoop();
  }

  function onPageHide() {
    if (getState() === STATE_ENGAGED) {
      beacon("bridge-exit", "stats=" + JSON.stringify(bridge.stats));
    }
    stopTimers();
  }

  /* ------------------------------------------ polling (A7, ADR-005) */
  function startPolling() {
    /* A7: self-rescheduling loop — each tick picks its own delay. */
    stopTimer("poll");
    tickPoll();
  }
  function restartPollLoop() {
    if (getState() === STATE_ENGAGED) {
      startPolling();
    }
  }

  /* A7 (ADR-005) — the cadence decision (precedence order). */
  function nextPollDelay() {
    if (document.hidden) {
      return POLL_HIDDEN_MS;
    }
    if (pollFailStreak > 0) {
      var mult = Math.pow(2, Math.min(pollFailStreak, 4) - 1);
      var d = POLL_FAIL_STEP_MS * mult;
      return d > POLL_BACKOFF_MAX_MS ? POLL_BACKOFF_MAX_MS : d;
    }
    if (fastTicks > 0) {
      return POLL_FAST_MS;
    }
    if (bridge.engineDown) {
      return POLL_DOWN_MS;
    }
    if (shadow.playing) {
      return POLL_PLAY_MS;
    }
    return POLL_PAUSED_MS;
  }

  function scheduleNextPoll() {
    stopTimer("poll");
    if (getState() !== STATE_ENGAGED) {
      return;
    }
    timers.poll = setTimeout(tickPoll, nextPollDelay());
  }

  /* A7 — one transport-class failure: SILENT by contract (no element
   * touched); the backoff grows and the "SBC unreachable" hint shows
   * only after 15 s sustained. Session verdicts never reach this. */
  function onPollFail() {
    if (pollFailStreak === 0) {
      pollFailSince = Date.now();
    }
    pollFailStreak++;
    bridge.stats.pollsFailed = pollFailStreak;
    if (Date.now() - pollFailSince >= POLL_HINT_AFTER_MS) {
      updateHint(true);
    }
  }

  function onPollSuccess() {
    pollFailStreak = 0;
    pollFailSince = 0;
  }

  /* A7 (ADR-005) — state-VECTOR edges between two applied status
   * bodies (coarse: pos jump > 2.5 s i.e. beyond the nominal 2 s
   * advance, dur change >= 2 s, volume >= 2 pts). */
  function svChanged(prev, st) {
    if (!prev || !st) {
      return true;
    }
    var wasActive = (prev.playing === true) || (prev.paused === true);
    var nowActive = (st.playing === true) || (st.paused === true);
    if (wasActive !== nowActive) {
      return true;
    }
    if ((prev.engine === "connected") !== (st.engine === "connected")) {
      return true;
    }
    var pid = prev.play ? (prev.play.id || null) : null;
    var nid = st.play ? (st.play.id || null) : null;
    if (!!nid !== !!pid) {
      return true;
    }
    if (nid && String(nid) !== String(pid)) {
      return true;
    }
    if (st.play) {
      if (st.play.pos != null && prev.play && prev.play.pos != null) {
        /* STRICTLY above the nominal 2 s advance (+drift margin): a
         * normal 2 s poll shows pos moving ~2.0 s and must NOT qualify
         * as an edge — only a genuine seek does. */
        if (Math.abs(Number(st.play.pos) - Number(prev.play.pos)) > 2.5) {
          return true;
        }
      }
      if (st.play.dur != null && prev.play && prev.play.dur != null) {
        if (Math.abs(Number(st.play.dur) - Number(prev.play.dur)) >= 2.0) {
          return true;
        }
      }
    }
    if (st.volume != null && prev.volume != null) {
      if (Math.abs(Number(st.volume) - Number(prev.volume)) >= 2) {
        return true;
      }
    }
    return false;
  }

  function tickPoll() {
    if (getState() !== STATE_ENGAGED) {
      return;
    }
    bridge.stats.polls++;
    pollTimes.push(Date.now());
    if (pollTimes.length > 8) {
      pollTimes.shift();
    }
    doFetch("GET", API + "status", null, { tag: "status-poll", noGateHandle: true })
      .then(function (res) {
        /* Session verdicts are GATE material, not transport failures:
         * the 401/403 handler may degrade (that IS the native-player
         * restoration path) — T12 relies on exactly this branch. No
         * poll reschedule here: a degrade stops the loop itself, and
         * a non-degrading 401 leaves the previous scheduling alone. */
        if (res.status === 401 || res.status === 403) {
          bridge.onApiAuthFailure(res.status, res.body, "/status");
          return;
        }
        if (res.status !== 200 || !res.body) {
          onPollFail();
          scheduleNextPoll();
          return;
        }
        /* A7: applyStatus reschedules the loop itself (the success
         * path) — no double schedule here. */
        onPollSuccess();
        applyStatus(res.body);
      })
      .catch(function () {
        /* doFetch never rejects (it resolves a status-0 envelope);
         * belt and braces — a failure must still schedule its retry. */
        onPollFail();
        scheduleNextPoll();
      });
  }

  function applyStatus(st) {
    /* A7 (ADR-005) — manage the fast-tick debt before mutating the
     * shadow (edges judged against pre-update values). */
    var changed = svChanged(prevVec, st);
    prevVec = st;
    if (changed) {
      fastTicks = TRANS_FAST_TICKS;
    } else if (fastTicks > 0) {
      fastTicks--;
    }
    bridge.lastStatus = st;
    var eng = st.engine === "connected";
    var wasDown = bridge.engineDown;
    bridge.engineDown = !eng;
    if (wasDown && eng && playRetry) {
      /* engine is back: deliver the queued play intent once */
      var pr = playRetry;
      playRetry = null;
      sendIntent("/play", pr.payload);
    }

    var play = st.play || null;
    var newId = play ? play.id : null;

    if (st.playing) {
      shadow.playing = true;
      shadow.paused = false;
    } else if (st.paused) {
      shadow.playing = false;
      shadow.paused = true;
    } else {
      shadow.playing = false;
      shadow.paused = false;
    }
    if (play) {
      if (play.pos != null) {
        shadow.pos = Number(play.pos);
      }
      if (play.dur != null) {
        shadow.dur = Number(play.dur);
      }
      shadow.lastTickReal = { at: Date.now(), pos: shadow.pos };
    } else {
      shadow.pos = 0;
      shadow.dur = null;
    }
    if (st.volume != null) {
      shadow.volume = Number(st.volume);
    }
    /* Monotonic queue memory: a deck size can fall BELOW the acknowledged
       maximum only through a click-poisoned read (the SPA's own swap ships
       a /play that, per the A3 contract, flattens the engine playlist AND
       the store to the single song in flight) or through a service-side
       push. A proven-empty deck (size<=1 && length<=1, engine idle, no
       active track) resets the memory; until then adjacent decisions keep
       seeing the multi-item view, so a /next|/prev keeps winning over the
       /play fallback no matter how recently the deck was flattened. */
    var qrawSz = (st.queue != null && st.queue.size != null) ? st.queue.size : 0;
    var qrawLen = (st.queue != null && st.queue.length != null) ? st.queue.length : 0;
    if (qrawSz > queueHi) {
      queueHi = qrawSz;
    }
    /* one idle sample is not proof: the engine is empty for a couple of
       frames during EVERY track swap. two consecutive polls do. */
    var idleNow = (qrawSz <= 1 && qrawLen <= 1 && !st.playing
                   && !st.paused && !newId);
    idleStreak = idleNow ? idleStreak + 1 : 0;
    /* A6.R (T10) — the STOP picture, decoupled from the deck size:
     * the ENGINE is UP (engine-down is an OUTAGE — the element must
     * keep its image while commands queue; treating an outage as a
     * stop would freeze a healthy UI) AND the transport is stopped
     * (idle) AND nothing plays nor pauses. `queue.size` deliberately
     * does NOT participate: /stop leaves the list in the engine
     * (the parked-list contract), so a deck of N must never veto the
     * stop. Two consecutive samples protect against the millisecond
     * blank a loadfile transition produces around the current id. */
    var idleTripleNow = (st.engine === "connected" && st.idle === true
                         && !st.playing && !st.paused);
    idleTripleStreak = idleTripleNow ? idleTripleStreak + 1 : 0;
    if (idleStreak >= 2) {
      queueHi = 0;
    }
    /* A6.3 F6d — identity retention. A SINGLE blank poll (the engine
       blinks the current-track property to null across a loadfile
       transition; the fake sink and real mpv both do this) used to forget
       lastSwappedId, and the next poll (id back) re-fired the re-trigger
       below: a full re-arm of the element mid-session (switching window,
       load churn) that opened a mis-branch window on the play/pause
       toggle — the T3 /resume-ghost loss mode. The identity now dies only
       with PROOF of an empty deck (three consecutive idle polls); an
       intentional /stop clears it immediately (see the stop path). */
    if (idleStreak >= 3) {
      lastSwappedId = null;
    }

    /* transport flags: kept in the shadow so gesture handlers can DERIVE
       the next state from the ENGINE (deterministic — the DOM's
       aria-pressed/class is still stale when our CAPTURE-phase listener
       runs, because the SPA flips it in its own target-phase handler). */
    shadow.shuffle = st.shuffle === true;
    shadow.repeat = (st.repeat == null) ? "off" : String(st.repeat);
    if (newId) {
      shadow.currentId = newId;
    } else if (idleStreak >= 3) {
      /* F6d: retain the last KNOWN id across transient blanks — the
         same-id echo guard needs it; a proven idle clears it. */
      shadow.currentId = null;
    }

    /* ---- project onto the native player ---------------------------- */
    var el = primaryElement();
    if (el) {
      var meta = elMeta.get(el);
      if (meta) {
        meta.shadowPos = shadow.pos;
        meta.shadowDur = shadow.dur;
        if (!seekDragLock) {
          guardBegin();
          try {
            if (st.playing && el.paused) {
              if (lastPauseDirAt != null &&
                  (Date.now() - lastPauseDirAt) <= DIR_STALE_MS) {
                /* A6.2 — STALE-POLL DIRECTION LOCK. This poll still shows
                   the PRE-pause state; the element already obeys the
                   user's /pause. Un-pausing it here would cancel the
                   user's last direction (and the ensuing 'play' event
                   is exactly the ghost-resume hazard ADR-004 defends
                   against). The next poll — after the engine flip —
                   converges on its own. */
              } else {
                el.play();
              }
            } else if (st.paused && !el.paused) {
              if (lastResumeDirAt != null &&
                  (Date.now() - lastResumeDirAt) <= DIR_STALE_MS) {
                /* Mirror: a stale poll behind a USER resume must not
                   re-pause an element that is already playing ahead of
                   the engine. */
              } else {
                el.pause();
              }
            } else if (idleTripleStreak >= 2 && !el.paused) {
              /* A6.R (T10) — project the STOP onto the element: with
               * the silence feed the element would keep LOOPING
               * forever (armEl arms el.loop) — without this branch a
               * /stop leaves the UI player lit "playing" with its
               * shadow frozen (the 1/9 C1 loss mode: a pre-stop poll
               * un-pauses the element and nothing re-pauses it).
               * Pausing is the minimal reversible projection: the src
               * stays loaded, so the user's next play() re-arms the
               * transport through the normal intent path (/resume
               * starts the parked list, /play{last} replays), exactly
               * as a stopped native element would. Direction mirror:
               * a stale sample behind a fresh USER resume must not
               * re-stop an element that is starting ahead of the
               * engine. */
              if (lastResumeDirAt != null &&
                  (Date.now() - lastResumeDirAt) <= DIR_STALE_MS) {
                /* the engine is coming back after a user resume —
                   leave the element alone */
              } else {
                el.pause();
              }
            }
            if (st.volume != null) {
              el.volume = clamp(st.volume / 100, 0, 1);
            }
            if (seekSlider) {
              seekSlider.value = String(Math.floor(shadow.pos || 0));
            }
            if (volSlider && st.volume != null) {
              volSlider.value = String(clamp(Math.round(Number(st.volume)), 0, 100));
            }
          } catch (_e) { }
          guardEnd();
        }
      }
    }

    /* ---- current-track change coming FROM THE ENGINE -------------
       (F6d: lastSwappedId survives transient blank polls — cleared by the
       idle proof above or by the /stop path — so the re-trigger fires
       only on a genuine id change, never on a blink.) */
    if (newId && newId !== lastSwappedId && el) {
      retriggerTrackSwitch(el, newId);
      lastSwappedId = newId;
    }
    updateHint(false);
    scheduleNextPoll(); /* A7: the success path reschedules itself */
  }

  function primaryElement() {
    /* the most recently armed silent element wins */
    var els = document.querySelectorAll("audio,video");
    var best = null;
    for (var i = 0; i < els.length; i++) {
      var meta = elMeta.get(els[i]);
      if (meta && meta.silent) {
        if (!best || meta.guardSeq > elMeta.get(best).guardSeq) {
          best = els[i];
        }
      }
    }
    return best;
  }

  function retriggerTrackSwitch(el, id) {
    /* Drive the NATIVE player's track-change pipeline: a source change
     * is exactly what the official player does between songs, so its
     * event wiring (loadeddata/loadedmetadata -> UI "Now Playing") runs
     * unchanged. The synthetic URL is intercepted (never fetched) and
     * no /play intent ships (flag kind=internal). */
    guardBegin();
    try {
      var meta = elMeta.get(el);
      if (meta) {
        meta.swappedAlready = true; /* suppress the swap's own intent */
        /* A6.R (T12) — do NOT clear the restore targets here: the
         * element's last REAL url must survive this synthetic
         * re-trigger (armEl ignores internal urls for origUrl /
         * lastNativeUrl as well) so degradation restores the genuine
         * source, never the __nvr_synth=1 token. */
      }
      /* Publish the engine-pushed id on the element: the player library's
       * "Now Playing" wiring (or any test double of it) reads this attr
       * on loadeddata/datachange to relabel the track — the same
       * information the real SPA gets from its own state. */
      try {
        el.setAttribute("data-nvr-sync-id", String(id));
      } catch (_e2) { }
      /* Go THROUGH the patched setter (never the raw one): the synthetic
       * URL is classified as internal and silently armed — it is NEVER
       * fetched, so the UI's source-change pipeline runs on a safe URL. */
      handleSrcSet(el, synthStreamUrl(id));
    } catch (_e) {
      /* cosmetic re-trigger failed: the poll keeps shadow state true */
    }
    guardEnd();
  }

  function synthStreamUrl(id) {
    return "/rest/stream?id=" + encodeURIComponent(id) + "&__nvr_synth=1";
  }

  /* --------------------------------------------------------- clock tick */
  function startTicks() {
    stopTimer("tick");
    timers.tick = setInterval(tickClock, SYNTH_TICK_MS);
  }

  function tickClock() {
    if (getState() !== STATE_ENGAGED) {
      return;
    }
    /* interpolate shadow position between polls so the UI's time labels
     * keep ticking (the silence element's own clock is meaningless). */
    var el = primaryElement();
    if (!el) {
      return;
    }
    var meta = elMeta.get(el);
    if (!meta || !meta.silent) {
      return;
    }
    if (shadow.playing && shadow.lastTickReal) {
      var dt = (Date.now() - shadow.lastTickReal.at) / 1000;
      meta.shadowPos = (shadow.lastTickReal.pos || 0) + dt;
      if (shadow.dur != null && meta.shadowPos > shadow.dur) {
        meta.shadowPos = shadow.dur;
      }
    }
    guardBegin();
    try {
      el.dispatchEvent(new Event("timeupdate"));
    } catch (_e) { }
    guardEnd();
  }

  function stopTimer(which) {
    if (timers[which]) {
      clearInterval(timers[which]);
      timers[which] = null;
    }
  }
  function stopTimers() {
    stopTimer("poll");
    stopTimer("tick");
    stopTimer("watch");
    stopTimer("engage");
    if (timers.reprobe) {
      clearInterval(timers.reprobe);
      timers.reprobe = null;
    }
  }
  function memsetShadow() {
    shadow.pos = 0;
    shadow.dur = null;
    shadow.playing = false;
    shadow.paused = false;
    shadow.volume = null;
    shadow.currentId = null;
    shadow.lastTickReal = null;
    shadow.shuffle = false;
    shadow.repeat = "off";
  }

  /* ------------------------------------------------------------- badge */
  var BADGE_ID = "__nvr_bridge_badge";

  function humanReason() {
    var r = bridge.degradedReason || "";
    if (r === "anchor-drift") {
      return "SBC remote off — UI version differs from the SBC anchor (native playback)";
    }
    if (r.indexOf("gate:service-account-forbidden") === 0) {
      return "SBC remote off — service account cannot authenticate from a browser (native playback)";
    }
    if (r.indexOf("gate:") === 0) {
      return "SBC remote off — session rejected (" + r.slice(5) + ") (native playback)";
    }
    if (r.indexOf("fatal:") === 0) {
      return "SBC bridge error (" + r.slice(6) + ") — native playback";
    }
    return "SBC remote off (" + r + ") — native playback";
  }

  function updateHint(unreachable) {
    var badge = document.getElementById(BADGE_ID);
    var show, dot, text;
    if (bridge.state === STATE_ENGAGED) {
      show = bridge.engineDown;
      dot = show ? "warn" : "off";
      text = show ? "Playback engine offline — commands are queued" : "";
    } else if (bridge.state === STATE_DEGRADED) {
      show = true;
      dot = "bad";
      text = humanReason();
    } else {
      show = false; /* passive: never decorate the login surface */
      dot = "off";
      text = "";
    }
    if (unreachable && bridge.state === STATE_ENGAGED && !show) {
      show = true;
      dot = "warn";
      text = "SBC unreachable — will retry";
    }
    if (!show) {
      if (badge) {
        badge.parentNode.removeChild(badge);
      }
      return;
    }
    if (!badge) {
      badge = document.createElement("div");
      badge.id = BADGE_ID;
      badge.style.cssText = "position:fixed;right:12px;bottom:12px;z-index:2147483000;" +
        "max-width:320px;padding:6px 10px;border-radius:6px;font:11px/1.4 sans-serif;" +
        "background:rgba(24,28,34,0.92);color:#dde3ea;border:1px solid rgba(120,130,140,0.35);" +
        "cursor:pointer;user-select:none;display:flex;align-items:center;gap:8px;";
      badge.onclick = function () {
        beacon("bridge-badge-viewed", badge.getAttribute("data-detail") || "");
      };
      if (document.body) {
        document.body.appendChild(badge);
      }
    }
    var colors = { bad: "#e5534b", warn: "#e0a832", off: "transparent" };
    var dotEl = document.createElement("span");
    dotEl.style.cssText = "flex:0 0 auto;width:8px;height:8px;border-radius:50%;background:" + colors[dot] + ";";
    badge.innerHTML = "";
    badge.appendChild(dotEl);
    var txt = document.createElement("span");
    txt.textContent = text;
    badge.appendChild(txt);
    badge.setAttribute("data-detail", sanitize(text + " | " + (bridge.degradedReason || "")));
  }

  /* --------------------------------------------- session watcher (boot) */
  function startSessionWatcher() {
    var decided = false;
    function decide() {
      if (decided) {
        return;
      }
      if (!hasSession()) {
        return; /* still anonymous: keep watching (cheap) */
      }
      decided = true;
      stopTimer("watch");
      var v = pageVersion();
      if (v !== null && !anchorMatch(v)) {
        /* version gate: refuse to touch a foreign-version UI (A2 input) */
        bridge.state = STATE_DEGRADED;
        bridge.degradedReason = "anchor-drift";
        beacon("anchor-drift", "page=" + v + " anchor=" + ANCHOR);
        updateHint();
        return;
      }
      engage(v);
    }
    /* decide early if already authenticated at boot */
    try {
      if (hasSession()) {
        decide();
        return;
      }
    } catch (_e) { }
    timers.watch = setInterval(decide, SESSION_WATCH_MS);
    /* hard ceiling: stop watching after 10 min (a login that never
     * happens should not leave a timer behind forever) */
    timers.engage = setTimeout(function () {
      stopTimer("watch");
    }, 10 * 60 * 1000);
  }

  /* ------------------------------------------------------------- utils */
  function round1(x) {
    return Math.round(x * 10) / 10;
  }
  function clamp(x, lo, hi) {
    return x < lo ? lo : (x > hi ? hi : x);
  }

  /* ------------------------------------------------------------- boot */
  var SILENCE = buildSilenceWav();

  window.__NVRA_BRIDGE__ = bridge;

  /* test/inspection surface — deliberately tiny and side-effect free */
  /* the six inspection entries use QUOTED keys on purpose: the hermetic
     lint (tests/test_bridge_asset.py::test_test_seam_exposed) pins this
     exact surface as the bridge's observable contract. */
  bridge._t = {
    '_state': function () {
      return bridge.state;
    },
    '_reason': function () {
      return bridge.degradedReason;
    },
    '_intents': function () {
      return intentRing.slice();
    },
    '_status': function () {
      return bridge.lastStatus;
    },
    '_stats': function () {
      return JSON.parse(JSON.stringify(bridge.stats));
    },
    '_shadow': function () {
      return JSON.parse(JSON.stringify(shadow));
    },
    '_gatelog': function () {
      return gateLog.slice(-40);
    },
    _resetStats: function () {
      bridge.stats.intents = 0;
      bridge.stats.polls = 0;
      bridge.stats.guardWrites = 0;
      lastIntents = {};
      intentRing.length = 0;
    },
    _forceEngage: function () {
      engage(pageVersion());
    },
    _forceDegrade: function (reason) {
      degrade(reason || "test-forced", "force");
    },
    _pollOnce: function () {
      tickPoll();
    },
    _api: function (route, payload) {
      return apiCall(route, payload, route === "/status");
    },
    /* A7 test seams — read-only introspection of the adaptive loop:
     * the tick log (for asserting cadence gaps in E2E) and the current
     * cadence decision (fail streak / fast-tick debt). */
    _pollTimes: function () {
      return pollTimes.slice();
    },
    _cadence: function () {
      return {
        delay: nextPollDelay(),
        failStreak: pollFailStreak,
        fastTicks: fastTicks
      };
    }
  };

  /* the pre-auth boot beacon (A1 behavior, kept): only when a session is
   * ALREADY there at inject time; otherwise the session watcher engages
   * (and beacons) the moment one appears. */
  if (pageJwt()) {
    bridge.trace = function (kind, detail) {
      beacon(kind, detail);
    };
    var pv = pageVersion();
    if (pv !== null && !anchorMatch(pv)) {
      /* will also be re-checked by the watcher after login; fire early
       * while authenticated so the gate sees it immediately. */
      var pb = { k: "anchor-drift", d: ("page=" + pv + " anchor=" + ANCHOR).slice(0, 512) };
      var pj = pageJwt();
      if (pj) {
        pb.jwt = pj;
      }
      try {
        navigator.sendBeacon && navigator.sendBeacon("/api/v1/spy", JSON.stringify(pb));
      } catch (_e) { }
    } else {
      beacon("bridge-loaded", document.title || "untitled");
    }
  }

  startSessionWatcher();

  window.addEventListener("error", function (evt) {
    if (bridge.state !== STATE_ENGAGED) {
      return;
    }
    /* page-level errors are telemetry, not fatal for the bridge */
    beacon("page-error", String((evt && evt.message) || "unknown"));
  }, true);
  window.addEventListener("unhandledrejection", function (evt) {
    if (bridge.state !== STATE_ENGAGED) {
      return;
    }
    beacon("unhandled-rejection", String(evt.reason).slice(0, 128));
  });
})();
