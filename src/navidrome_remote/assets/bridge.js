/* navidrome-remote bridge — M0' (anchor: Navidrome UI v0.64.2)
 *
 * Injected by the proxy into the HTML head of the official Navidrome UI.
 * Deliberately minimal in M0': it proves the injection is PRESENTE E
 * CARREGANDO (M0' acceptance 4) and beacons boot/error traces to
 * /api/v1/spy so the NDJSON spy records player boot client-side.
 *
 * It does NOT intercept transport yet — that is M1 ("bridge.js
 * definitivo"): play/pause/seek/next listeners will redirect to
 * /api/v1/* so the SBC (mpv) performs the playback while the browser
 * stays a pure control surface.
 *
 * Credential invariant: this file never stores or echoes Navidrome
 * credentials or stream URLs.
 */
(function () {
  "use strict";
  if (window.__NVRA_BRIDGE__) {
    return; /* injected exactly once (idempotent) */
  }

  var bridge = {
    svc: "navidrome-remote",
    phase: "M0'",
    anchor: "v0.64.2",
    version: "0.1.0-m0",
    bootedAt: new Date().toISOString(),
    url: location.href,
    trace: function (kind, detail) {
      try {
        var body = JSON.stringify({
          k: String(kind).slice(0, 64),
          d: String(detail).slice(0, 512)
        });
        if (navigator.sendBeacon) {
          navigator.sendBeacon("/api/v1/spy", body);
        }
      } catch (_err) {
        /* tracing must never break the UI */
      }
    }
  };

  window.__NVRA_BRIDGE__ = bridge;
  bridge.trace("bridge-loaded", document.title || "untitled");

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
