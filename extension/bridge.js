// DRBR — isolated-world bridge on https://www.youtube.com/feed/history.
//
// The MAIN-world hook can see the page's data but not the extension APIs; this
// script can use the extension APIs but not the page's JavaScript objects. It
// relays a "collect" command down via window.postMessage and hands the
// collected pages up to the service worker, which does the POST (content
// scripts cannot reach 127.0.0.1 without CORS; the service worker has host
// permission).
(() => {
  "use strict";

  chrome.runtime.onMessage.addListener((msg, _sender, sendResponse) => {
    if (!msg || typeof msg !== "object") return;
    if (msg.type === "drbr-ping") {
      sendResponse({ ok: true });
      return;
    }
    if (msg.type !== "drbr-collect") return;

    const requestId = crypto.randomUUID();
    const onMessage = (event) => {
      const data = event.data;
      if (event.source !== window || !data || data.source !== "drbr-page" ||
          data.type !== "collected" || data.requestId !== requestId) return;
      window.removeEventListener("message", onMessage);
      const payload = {
        schema: 1,
        // The instant the page loaded: "Today"/"Monday" headers are relative to it.
        captured_at: data.capturedAt,
        extension_version: chrome.runtime.getManifest().version,
        cursor: msg.cursor || null,
        cursor_reached: Boolean(data.cursorReached),
        pages: data.pages || [],
      };
      chrome.runtime.sendMessage({
        type: "drbr-collected",
        runId: msg.runId,
        body: JSON.stringify(payload),
        diagnostics: data.diagnostics || {},
      });
    };
    window.addEventListener("message", onMessage);
    window.postMessage({ source: "drbr-bridge", type: "collect", requestId, cursor: msg.cursor || null,
                         cursorDate: msg.cursorDate || null, pageCap: msg.pageCap }, window.location.origin);
    sendResponse({ started: true });
  });
})();
