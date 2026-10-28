/* navidrome-remote bridge — M1 (anchor: Navidrome UI v0.64.2)
 *
 * Injected by the proxy into the HTML head of the official Navidrome UI.
 *
 * M1 (A3): the bridge is now GATE-AWARE.
 *   * Every call toward /api/v1/* carries the page session: the UI parks
 *     its native JWT in localStorage (`token`) in v0.64.2 (A2 boot
 *     capture); this bridge forwards it in the REQUEST BODY (the beacon
 *     channel is sendBeacon — headers impossible; the server's gate
 *     extracts `jwt` from the body). When the session is absent (login
 *     screen, pre-auth boot) the beacon is simply not sent: the wire-level
 *     spy (proxy req/res events) still covers that interval.
 *   * Anchor check: if the page exposes its Navidrome version and it
 *     diverges from this build's anchor, a `anchor-drift` beacon fires
 *     (input to the upgrade gate — A2/run_gate domain).
 *
 * It does NOT intercept transport yet — that is A6 ("bridge.js
 * definitivo": play/pause/seek/next listeners redirected to /api/v1/*
 * so the SBC performs the playback while the browser stays a control
 * surface).
 *
 * Credential invariant: this file never PERSISTS or ECHOS any credential
 * or stream URL. It only relays, in transit, a JWT the PAGE ALREADY HOLDS
 * (it was created by the upstream for this very session — relay to the
 * same origin is neutral; to any other destination it is not sent).
 */
(function () {
  "use strict";
  if (window.__NVRA_BRIDGE__) {
    return; /* injected exactly once (idempotent) */
  }

  var ANCHOR = "v0.64.2";
  var LS_TOKEN_KEYS = ["token", "navidrome_token"]; // v0.64.2: "token"

  function pageJwt() {
    try {
      for (var i = 0; i < LS_TOKEN_KEYS.length; i++) {
        var v = localStorage.getItem(LS_TOKEN_KEYS[i]);
        if (v && v.indexOf(".") >= 0) {
          return v;
        }
      }
    } catch (_e) {
      /* storage unavailable (private-mode edge) — beacon simply unauthed */
    }
    return null;
  }

  function pageVersion() {
    try {
      if (window.__APP_CONFIG__ && typeof __APP_CONFIG__.version === "string") {
        return __APP_CONFIG__.version;
      }
    } catch (_e) {}
    return null;
  }

  var bridge = {
    svc: "navidrome-remote",
    phase: "M1",
    anchor: ANCHOR,
    version: "0.2.0-m1",
    bootedAt: new Date().toISOString(),
    url: location.href,
    // `withAuth:true` attaches the page session in the body (gate-aware).
    trace: function (kind, detail, withAuth) {
      try {
        var obj = {
          k: String(kind).slice(0, 64),
          d: String(detail).slice(0, 512)
        };
        if (withAuth !== false) {
          var jwt = pageJwt();
          if (jwt) {
            obj.jwt = jwt; // body-carried: sendBeacon cannot set headers
          }
        }
        var body = JSON.stringify(obj);
        if (navigator.sendBeacon) {
          navigator.sendBeacon("/api/v1/spy", body);
        }
      } catch (_err) {
        /* tracing must never break the UI */
      }
    }
  };

  window.__NVRA_BRIDGE__ = bridge;

  // Pre-auth boot: no session yet — skip the beacon (wire-level spy
  // covers it); the moment the page becomes authenticated the next
  // beacon (anchor drift check below, or an A6 transport hook) carries it.
  var preAuthBoot = !pageJwt();
  if (!preAuthBoot) {
    bridge.trace("bridge-loaded", document.title || "untitled");
  }

  // Anchor drift: v0.64.2's SPA exposes __APP_CONFIG__; if the declared
  // version diverges from this build's anchor, scream early (A2 gate
  // correlates on the spy side). Fire only when authenticated — the
  // beacon is session-bound.
  setTimeout(function () {
    var v = pageVersion();
    if (v && v !== ANCHOR.replace(/^v/, "")) {
      bridge.trace("anchor-drift", "page=" + v + " anchor=" + ANCHOR);
    } else if (v === null) {
      // no __APP_CONFIG__: SPA variant — informational only (no spam)
    }
  }, 1500);

  window.addEventListener(
    "error",
    function (evt) {
      bridge.trace("page-error", evt.message || "unknown");
    },
    true
  );
  window.addEventListener("unhandledrejection", function (evt) {
    bridge.trace("unhandled-rejection", String(evt.reason).slice(0, 128));
  });
})();
