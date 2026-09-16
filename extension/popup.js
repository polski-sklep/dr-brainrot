// DRBR — popup: settings, the sync button, and an honest account of the last run.
//
// Rendering rule: a failure is the biggest thing on screen, and "0 videos" is
// never shown for data that was missing. Zero new facts is only described as
// "nothing new" when the backend reached the previous sync's cursor.
"use strict";

const $ = (id) => document.getElementById(id);

function el(tag, text, cls) {
  const node = document.createElement(tag);
  if (text !== undefined) node.textContent = text;
  if (cls) node.className = cls;
  return node;
}

async function loadSettings() {
  const s = await chrome.storage.local.get(["backendUrl", "token", "pageCap"]);
  $("backendUrl").value = s.backendUrl || "http://127.0.0.1:8750";
  $("token").value = s.token || "";
  $("pageCap").value = s.pageCap || 8;
  if (!s.token) $("settings").open = true;
}

async function saveSettings() {
  const backendUrl = $("backendUrl").value.trim() || "http://127.0.0.1:8750";
  if (!/^http:\/\/127\.0\.0\.1(:\d+)?\/?$/.test(backendUrl)) {
    $("saved").textContent = "Backend must be http://127.0.0.1:<port>";
    $("saved").style.color = "#b00020";
    return;
  }
  const pageCap = Math.min(30, Math.max(1, Number($("pageCap").value) || 8));
  await chrome.storage.local.set({ backendUrl, token: $("token").value.trim(), pageCap });
  $("saved").style.color = "";
  $("saved").textContent = "Saved.";
  setTimeout(() => { $("saved").textContent = ""; }, 1500);
}

function list(items) {
  const ul = el("ul");
  for (const item of items) ul.appendChild(el("li", item));
  return ul;
}

function diagnosticsLines(d) {
  if (!d) return [];
  const lines = [];
  if (d.initialFound === false) lines.push("ytInitialData: NOT FOUND");
  if (d.pagesCaptured !== undefined) lines.push(`Pages captured: ${d.pagesCaptured} (scrolls: ${d.scrolls || 0})`);
  if (d.sections !== undefined) lines.push(`History sections seen: ${d.sections}; items seen: ${d.entries}`);
  if (d.oldestDate) lines.push(`Oldest day reached: ${d.oldestDate}`);
  if (d.unresolvedHeaders) lines.push(`Date headers the extension could not read: ${d.unresolvedHeaders}`);
  if (d.stopReason) lines.push(`Stopped because: ${d.stopReason}`);
  return lines;
}

function render(run) {
  const panel = $("status");
  panel.replaceChildren();
  panel.className = "panel";
  $("sync").disabled = Boolean(run && run.status === "running");
  if (!run) {
    panel.appendChild(el("span", "No sync run yet.", "muted"));
    return;
  }
  const o = run.outcome;
  if (run.status === "running") {
    panel.appendChild(el("div", "Sync running…", "title"));
    panel.appendChild(el("div", run.message || run.phase));
  } else if (run.status === "failed") {
    panel.classList.add("failed");
    panel.appendChild(el("div", "SYNC FAILED — nothing was measured", "title"));
    panel.appendChild(el("div", run.error || (o && o.error) || "Unknown failure."));
    if (o && o.import_id) panel.appendChild(el("div", `Recorded as failed import #${o.import_id}; the raw capture is kept for re-parsing.`, "muted"));
    if (o && o.warnings && o.warnings.length) panel.appendChild(list(o.warnings));
  } else if (o) {
    if (o.duplicate_of) {
      panel.classList.add("ok");
      panel.appendChild(el("div", `Already imported (#${o.duplicate_of}) — this exact capture was sent before; nothing new written.`, "title"));
    } else {
      const partial = o.status === "partial";
      panel.classList.add(partial ? "partial" : "ok");
      const cursorReached = run.diagnostics && run.diagnostics.stopReason === "reached the previous sync's newest video";
      let headline;
      if (o.measurements_written > 0) {
        headline = `Imported ${o.measurements_written} video measurement${o.measurements_written === 1 ? "" : "s"}`
          + (o.period_start ? ` (${o.period_start} → ${o.period_end})` : "");
      } else if (cursorReached) {
        headline = "No new videos since the last sync.";
      } else {
        headline = "Import finished but wrote no measurements — check the warnings below.";
      }
      panel.appendChild(el("div", headline, "title"));
      panel.appendChild(el("div", `Import #${o.import_id}: ${o.status}`, "muted"));
      if (o.warnings && o.warnings.length) {
        panel.appendChild(el("div", `${o.warnings.length} warning${o.warnings.length === 1 ? "" : "s"} (not measured, never counted as zero):`));
        panel.appendChild(list(o.warnings));
      }
    }
  }
  const diag = diagnosticsLines(run.diagnostics);
  if (diag.length) {
    const details = el("details");
    details.appendChild(el("summary", "Capture details"));
    details.appendChild(list(diag));
    if (run.status === "failed") details.open = true;
    panel.appendChild(details);
  }
  const when = run.finishedAt || run.updatedAt;
  if (when) panel.appendChild(el("div", `Last update: ${new Date(when).toLocaleString()}`, "muted"));
}

document.addEventListener("DOMContentLoaded", async () => {
  await loadSettings();
  const { lastRun } = await chrome.storage.local.get("lastRun");
  render(lastRun);
  $("save").addEventListener("click", saveSettings);
  $("sync").addEventListener("click", async () => {
    await saveSettings();
    $("sync").disabled = true;
    await chrome.runtime.sendMessage({ type: "drbr-start" });
  });
  chrome.storage.onChanged.addListener((changes, area) => {
    if (area === "local" && changes.lastRun) render(changes.lastRun.newValue);
  });
});
