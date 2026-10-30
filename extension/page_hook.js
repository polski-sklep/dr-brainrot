// dr-brainrot — MAIN-world script on https://www.youtube.com/feed/history.
//
// Runs in the page's own JavaScript world at document_start so it can see the
// data YouTube renders from: the `ytInitialData` object assigned by an inline
// script, and the JSON of every POST /youtubei/v1/browse continuation fetched
// while scrolling. It ships those objects verbatim. It does NOT read the DOM,
// estimate anything from pixels, or parse progress: all parsing happens
// server-side in sources/youtube.py, against a tested fixture (DECISIONS DR1.12).
//
// The only interpretation done here is enough to know when to stop scrolling:
// has the previous sync's newest video been seen (under a date no later than
// the one it was stored with), is there no further continuation, or has the
// page cap been hit.
(() => {
  "use strict";
  if (window.__drBrainrotHookInstalled) return;
  window.__drBrainrotHookInstalled = true;

  const loadedAt = new Date();
  const pages = []; // browse responses carrying history sections, in arrival order
  let initialData = null;

  // --- capture ytInitialData --------------------------------------------------
  // The page does `var ytInitialData = {...};`. A pre-defined accessor on window
  // receives that assignment. A fallback re-reads the inline script text.
  try {
    let stored;
    Object.defineProperty(window, "ytInitialData", {
      configurable: true,
      enumerable: true,
      get() { return stored; },
      set(value) {
        stored = value;
        if (value && typeof value === "object") initialData = value;
      },
    });
  } catch (_) { /* property already defined: fall back to script text */ }

  function initialFromScripts() {
    const marker = "var ytInitialData = ";
    for (const script of document.scripts) {
      const text = script.textContent || "";
      const at = text.indexOf(marker);
      if (at < 0) continue;
      const body = text.slice(at + marker.length);
      const end = body.lastIndexOf("}");
      if (end < 0) continue;
      try { return JSON.parse(body.slice(0, end + 1)); } catch (_) { /* keep looking */ }
    }
    return null;
  }

  // --- capture browse responses -----------------------------------------------
  function hasKey(node, key, depth = 0) {
    if (depth > 60 || node === null || typeof node !== "object") return false;
    if (Array.isArray(node)) return node.some((v) => hasKey(v, key, depth + 1));
    if (Object.prototype.hasOwnProperty.call(node, key)) return true;
    return Object.values(node).some((v) => hasKey(v, key, depth + 1));
  }

  function isBrowseUrl(url) {
    // /youtubei/v1/browse, but not /youtubei/v1/browse/edit_playlist etc.
    return typeof url === "string" && /\/youtubei\/v1\/browse(\?|$)/.test(url);
  }

  function record(json) {
    if (json && typeof json === "object" && hasKey(json, "itemSectionRenderer")) pages.push(json);
  }

  const originalFetch = window.fetch;
  window.fetch = function (input, init) {
    const promise = originalFetch.apply(this, arguments);
    try {
      const url = typeof input === "string" ? input : (input && input.url) || "";
      if (isBrowseUrl(url)) {
        promise.then((resp) => resp.clone().json().then(record)).catch(() => {});
      }
    } catch (_) { /* never break the page */ }
    return promise;
  };

  const originalOpen = XMLHttpRequest.prototype.open;
  const originalSend = XMLHttpRequest.prototype.send;
  XMLHttpRequest.prototype.open = function (method, url) {
    this.__drBrainrotUrl = String(url || "");
    return originalOpen.apply(this, arguments);
  };
  XMLHttpRequest.prototype.send = function () {
    if (isBrowseUrl(this.__drBrainrotUrl)) {
      this.addEventListener("load", () => {
        try {
          if (this.responseType === "json") record(this.response);
          else if (this.responseType === "" || this.responseType === "text") record(JSON.parse(this.responseText));
        } catch (_) { /* not JSON */ }
      });
    }
    return originalSend.apply(this, arguments);
  };

  // --- just enough reading to decide when to stop -------------------------------
  const WEEKDAYS = ["sunday", "monday", "tuesday", "wednesday", "thursday", "friday", "saturday"];
  const MONTHS = { jan: 0, january: 0, feb: 1, february: 1, mar: 2, march: 2, apr: 3, april: 3, may: 4,
    jun: 5, june: 5, jul: 6, july: 6, aug: 7, august: 7, sep: 8, sept: 8, september: 8, oct: 9,
    october: 9, nov: 10, november: 10, dec: 11, december: 11 };

  const iso = (d) => `${d.getFullYear()}-${String(d.getMonth() + 1).padStart(2, "0")}-${String(d.getDate()).padStart(2, "0")}`;

  // Mirrors sources/youtube.py resolve_date_header. Returns null when unsure;
  // the server is the authority and fails loudly on anything it cannot date.
  function resolveHeader(text, now) {
    const s = String(text || "").trim().replace(/\s+/g, " ").toLowerCase();
    const today = new Date(now.getFullYear(), now.getMonth(), now.getDate());
    const back = (n) => new Date(today.getFullYear(), today.getMonth(), today.getDate() - n);
    if (s === "today") return iso(today);
    if (s === "yesterday") return iso(back(1));
    const wd = WEEKDAYS.indexOf(s);
    if (wd >= 0) return iso(back(((today.getDay() - wd) % 7 + 7) % 7 || 7));
    const m = s.match(/^([a-z]+)\.?\s+(\d{1,2})(?:,\s*(\d{4}))?$/);
    if (!m || !(m[1] in MONTHS)) return null;
    const month = MONTHS[m[1]], day = Number(m[2]);
    if (m[3]) return iso(new Date(Number(m[3]), month, day));
    for (let year = today.getFullYear(); year > today.getFullYear() - 9; year--) {
      const d = new Date(year, month, day);
      if (d.getMonth() !== month) continue;
      if (d <= today) return iso(d);
    }
    return null;
  }

  function textOf(node) {
    if (!node || typeof node !== "object") return null;
    if (typeof node.simpleText === "string") return node.simpleText;
    if (typeof node.content === "string") return node.content;
    if (Array.isArray(node.runs)) return node.runs.map((r) => (r && r.text) || "").join("");
    return null;
  }

  function* sections(node, depth = 0) {
    if (depth > 60 || node === null || typeof node !== "object") return;
    if (Array.isArray(node)) { for (const v of node) yield* sections(v, depth + 1); return; }
    for (const [k, v] of Object.entries(node)) {
      if (k === "itemSectionRenderer" && v && typeof v === "object") yield v;
      else yield* sections(v, depth + 1);
    }
  }

  function summarise(allPages, now, cursor, cursorDate) {
    let sectionCount = 0, entries = 0, cursorSeen = false, oldest = null, unresolved = 0;
    for (const page of allPages) {
      for (const section of sections(page)) {
        sectionCount++;
        const header = textOf(section.header && section.header.itemSectionHeaderRenderer &&
                              section.header.itemSectionHeaderRenderer.title);
        const date = resolveHeader(header, now);
        if (date === null) unresolved++;
        else if (oldest === null || date < oldest) oldest = date;
        for (const item of section.contents || []) {
          const ids = [];
          if (item && item.lockupViewModel) ids.push(item.lockupViewModel.contentId);
          if (item && item.reelShelfRenderer) {
            for (const si of item.reelShelfRenderer.items || []) {
              const ep = si && si.shortsLockupViewModel && si.shortsLockupViewModel.onTap &&
                si.shortsLockupViewModel.onTap.innertubeCommand &&
                si.shortsLockupViewModel.onTap.innertubeCommand.reelWatchEndpoint;
              ids.push(ep && ep.videoId);
            }
          }
          for (const id of ids) {
            if (!id) continue;
            entries++;
            if (cursor && id === cursor && (!cursorDate || date === null || date <= cursorDate)) cursorSeen = true;
          }
        }
      }
    }
    const last = allPages[allPages.length - 1];
    return { sectionCount, entries, cursorSeen, oldest, unresolved,
             hasContinuation: hasKey(last, "continuationCommand") };
  }

  const sleep = (ms) => new Promise((r) => setTimeout(r, ms));

  async function waitForNewPage(before, timeoutMs) {
    const deadline = Date.now() + timeoutMs;
    let nudged = Date.now();
    while (Date.now() < deadline) {
      if (pages.length > before) return true;
      if (Date.now() - nudged > 2000) {
        window.scrollBy(0, -200);
        window.scrollTo(0, document.documentElement.scrollHeight);
        nudged = Date.now();
      }
      await sleep(250);
    }
    return pages.length > before;
  }

  const FIRST_SYNC_DAYS = 14;

  async function collect({ cursor, cursorDate, pageCap }) {
    // Give the page a moment to finish assigning its data.
    for (let i = 0; i < 40 && !initialData && !window.ytInitialData; i++) await sleep(250);
    const initial = initialData || (typeof window.ytInitialData === "object" && window.ytInitialData) || initialFromScripts();
    const diagnostics = { initialFound: Boolean(initial), pagesCaptured: 0, sections: 0, entries: 0,
                          scrolls: 0, oldestDate: null, unresolvedHeaders: 0, stopReason: null,
                          loadedAt: loadedAt.toISOString() };
    if (!initial) {
      diagnostics.stopReason = "ytInitialData was not found on the history page (not signed in, or the page changed)";
      return { pages: [], cursorReached: false, diagnostics };
    }
    const cap = Math.max(1, Number(pageCap) || 8);
    const firstSyncFloor = iso(new Date(loadedAt.getFullYear(), loadedAt.getMonth(), loadedAt.getDate() - FIRST_SYNC_DAYS));
    for (;;) {
      const all = [initial, ...pages];
      const s = summarise(all, loadedAt, cursor, cursorDate);
      Object.assign(diagnostics, { pagesCaptured: all.length, sections: s.sectionCount, entries: s.entries,
                                   oldestDate: s.oldest, unresolvedHeaders: s.unresolved });
      if (s.sectionCount === 0) {
        diagnostics.stopReason = "the page data contains no history sections (structure changed?)";
        return { pages: all, cursorReached: false, diagnostics };
      }
      if (cursor && s.cursorSeen) {
        diagnostics.stopReason = "reached the previous sync's newest video";
        return { pages: all, cursorReached: true, diagnostics };
      }
      if (!cursor && s.oldest && s.oldest < firstSyncFloor) {
        diagnostics.stopReason = `first sync: captured back past ${firstSyncFloor}`;
        return { pages: all, cursorReached: false, diagnostics };
      }
      if (!s.hasContinuation) {
        diagnostics.stopReason = "end of history (no further continuation)";
        return { pages: all, cursorReached: false, diagnostics };
      }
      if (all.length >= cap) {
        diagnostics.stopReason = `page cap (${cap}) reached before the previous sync's newest video`;
        return { pages: all, cursorReached: false, diagnostics };
      }
      const before = pages.length;
      window.scrollTo(0, document.documentElement.scrollHeight);
      diagnostics.scrolls++;
      if (!(await waitForNewPage(before, 20000))) {
        diagnostics.stopReason = "scrolling loaded no further history within 20 s";
        return { pages: [initial, ...pages], cursorReached: false, diagnostics };
      }
    }
  }

  window.addEventListener("message", async (event) => {
    const msg = event.data;
    if (event.source !== window || !msg || msg.source !== "dr-brainrot-bridge" || msg.type !== "collect") return;
    let result;
    try {
      result = await collect(msg);
    } catch (err) {
      result = { pages: [], cursorReached: false,
                 diagnostics: { initialFound: Boolean(initialData), stopReason: `collector crashed: ${err && err.message}` } };
    }
    window.postMessage({ source: "dr-brainrot-page", type: "collected", requestId: msg.requestId,
                         capturedAt: loadedAt.toISOString(), ...result }, window.location.origin);
  });
})();
