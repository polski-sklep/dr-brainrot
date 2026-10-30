// dr-brainrot — service worker. Orchestrates one weekly sync:
//   GET cursor -> (re)load the history tab -> content scripts scroll and collect
//   -> POST payload -> store the Outcome for the popup.
//
// State lives in chrome.storage.local ("lastRun"), not in this worker, so the
// popup can be closed mid-sync and the worker can be suspended between steps.
"use strict";

const HISTORY_URL = "https://www.youtube.com/feed/history";
const DEFAULTS = { backendUrl: "http://127.0.0.1:8750", token: "", pageCap: 8 };

async function getSettings() {
  const stored = await chrome.storage.local.get(["backendUrl", "token", "pageCap"]);
  const out = { ...DEFAULTS };
  for (const k of Object.keys(DEFAULTS)) if (stored[k] !== undefined && stored[k] !== "") out[k] = stored[k];
  out.backendUrl = String(out.backendUrl).replace(/\/+$/, "");
  return out;
}

async function updateRun(runId, patch) {
  const { lastRun } = await chrome.storage.local.get("lastRun");
  if (!lastRun || lastRun.runId !== runId) return false; // superseded by a newer run
  await chrome.storage.local.set({ lastRun: { ...lastRun, ...patch, updatedAt: new Date().toISOString() } });
  return true;
}

async function fail(runId, error, extra = {}) {
  await updateRun(runId, { status: "failed", phase: "finished", error, ...extra });
}

function waitForTabComplete(getTabId, timeoutMs) {
  // getTabId is a function because a newly created tab's id is only known
  // after chrome.tabs.create resolves, and the listener must exist before.
  return new Promise((resolve) => {
    let sawLoading = false;
    const timer = setTimeout(() => { chrome.tabs.onUpdated.removeListener(listener); resolve(false); }, timeoutMs);
    function listener(id, info) {
      if (id !== getTabId()) return;
      if (info.status === "loading") sawLoading = true;
      if (info.status === "complete" && sawLoading) {
        clearTimeout(timer);
        chrome.tabs.onUpdated.removeListener(listener);
        resolve(true);
      }
    }
    chrome.tabs.onUpdated.addListener(listener);
  });
}

async function pingContentScript(tabId, attempts = 20) {
  for (let i = 0; i < attempts; i++) {
    try {
      const r = await chrome.tabs.sendMessage(tabId, { type: "dr-brainrot-ping" });
      if (r && r.ok) return true;
    } catch (_) { /* not injected yet */ }
    await new Promise((r) => setTimeout(r, 500));
  }
  return false;
}

async function startSync() {
  const runId = crypto.randomUUID();
  await chrome.storage.local.set({ lastRun: { runId, status: "running", phase: "cursor",
    startedAt: new Date().toISOString(), updatedAt: new Date().toISOString(),
    message: "Asking the backend for the cursor…", outcome: null, error: null, diagnostics: null } });

  const settings = await getSettings();
  if (!settings.token) return fail(runId, "No dr-brainrot token set. Paste DR_BRAINROT_INGEST_TOKEN into the settings and save.");

  let cursor;
  try {
    const resp = await fetch(`${settings.backendUrl}/api/youtube/cursor`, {
      headers: { "X-Dr-Brainrot-Token": settings.token },
    });
    if (resp.status === 401) return fail(runId, "The backend rejected the token (401). Check DR_BRAINROT_INGEST_TOKEN.");
    if (!resp.ok) return fail(runId, `The backend answered HTTP ${resp.status} for the cursor.`);
    cursor = await resp.json();
  } catch (err) {
    return fail(runId, `The dr-brainrot backend is unreachable at ${settings.backendUrl} (${err.message}). Is the dashboard running?`);
  }

  await updateRun(runId, { phase: "loading", message: "Loading the YouTube history page…", cursor });
  const existing = await chrome.tabs.query({ url: `${HISTORY_URL}*` });
  // Always (re)load: a fresh page load gives a fresh ytInitialData and a clean
  // list of continuation responses, whatever the tab was showing before. The
  // load listener is registered before navigating so a fast load is not missed.
  let tabId = existing.length ? existing[0].id : null;
  const loaded = waitForTabComplete(() => tabId, 60000);
  if (tabId !== null) {
    await chrome.tabs.update(tabId, { url: HISTORY_URL, active: true });
  } else {
    tabId = (await chrome.tabs.create({ url: HISTORY_URL, active: true })).id;
  }
  const tab = { id: tabId };
  if (!(await loaded)) return fail(runId, "The YouTube history page did not finish loading within 60 s.");
  if (!(await pingContentScript(tab.id))) {
    return fail(runId, "The extension's page script did not start on the history page. Reload the extension and try again.");
  }

  await updateRun(runId, { phase: "scrolling", tabId: tab.id,
    message: "Scrolling history back to the last sync… keep the tab visible." });
  try {
    await chrome.tabs.sendMessage(tab.id, { type: "dr-brainrot-collect", runId, cursor: cursor.cursor,
      cursorDate: cursor.cursor_date, pageCap: Number(settings.pageCap) || DEFAULTS.pageCap });
  } catch (err) {
    return fail(runId, `Could not start collection on the page (${err.message}).`);
  }
}

async function postPayload(msg) {
  const { runId, body, diagnostics } = msg;
  if (!(await updateRun(runId, { phase: "posting", message: "Sending captured history to the backend…",
    diagnostics, payloadBytes: body.length }))) return;
  const settings = await getSettings();

  // Missing page data is still posted: the backend records a failed import with
  // the raw capture kept, and the popup shows the failure, never "0 videos".
  const localProblem = diagnostics && !diagnostics.initialFound ? diagnostics.stopReason : null;

  let resp;
  try {
    resp = await fetch(`${settings.backendUrl}/api/youtube/history`, {
      method: "POST",
      headers: { "Content-Type": "application/json", "X-Dr-Brainrot-Token": settings.token },
      body,
    });
  } catch (err) {
    return fail(runId, `The dr-brainrot backend is unreachable at ${settings.backendUrl} (${err.message}). Nothing was imported.`);
  }
  if (resp.status === 401) return fail(runId, "The backend rejected the token (401). Nothing was imported.");
  if (resp.status === 413) return fail(runId, "The capture is too large for the backend (413). Lower the page cap and run again.");
  let outcome = null;
  try { outcome = await resp.json(); } catch (_) { /* handled below */ }
  if (!outcome || (resp.status !== 200 && resp.status !== 422)) {
    return fail(runId, `The backend answered HTTP ${resp.status} without an import outcome.`, { httpStatus: resp.status });
  }
  const failed = outcome.status === "failed";
  await updateRun(runId, {
    status: failed ? "failed" : "done",
    phase: "finished",
    httpStatus: resp.status,
    outcome,
    error: failed ? (localProblem ? `${localProblem}. Backend: ${outcome.error}` : outcome.error) : null,
    finishedAt: new Date().toISOString(),
  });
}

chrome.runtime.onMessage.addListener((msg, sender, sendResponse) => {
  if (!msg || typeof msg !== "object") return;
  if (msg.type === "dr-brainrot-start" && !sender.tab) {
    startSync().catch(async (err) => {
      const { lastRun } = await chrome.storage.local.get("lastRun");
      if (lastRun) await fail(lastRun.runId, `Unexpected error: ${err && err.message}`);
    });
    sendResponse({ ok: true });
    return;
  }
  if (msg.type === "dr-brainrot-collected" && sender.tab) {
    postPayload(msg).catch((err) => fail(msg.runId, `Unexpected error while posting: ${err && err.message}`));
    sendResponse({ ok: true });
  }
});
