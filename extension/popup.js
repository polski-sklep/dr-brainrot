// dr-brainrot — popup: settings, the sync button, and an honest account of the last run.
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

//: A run is only believed to be running while it keeps saying so. A sync that
//: dies mid-scroll — the tab is switched away, the browser quits, the machine
//: sleeps — leaves `status: "running"` in storage for ever, and without this
//: the popup reports "Sync running…" and DISABLES the sync button, so the one
//: thing that would recover it cannot be pressed. That is how a failed run on
//: 22 Sep silently blocked every sync for a week.
const STALE_RUN_MS = 15 * 60 * 1000;

function isStalled(run) {
  if (!run || run.status !== "running") return false;
  const beat = Date.parse(run.updatedAt || run.startedAt || "");
  return !Number.isFinite(beat) || Date.now() - beat > STALE_RUN_MS;
}

function render(run) {
  const panel = $("status");
  panel.replaceChildren();
  panel.className = "panel";
  const stalled = isStalled(run);
  $("sync").disabled = Boolean(run && run.status === "running" && !stalled);
  if (!run) {
    panel.appendChild(el("span", "No sync run yet.", "muted"));
    return;
  }
  const o = run.outcome;
  if (stalled) {
    panel.classList.add("failed");
    panel.appendChild(el("div", "SYNC INTERRUPTED — nothing was measured", "title"));
    panel.appendChild(el("div", `It stopped at "${run.message || run.phase}" and never finished. `
      + "The history tab has to stay visible for the whole run. Nothing was lost: the backend "
      + "remembers where the last completed sync got to, so running again picks up from there."));
  } else if (run.status === "running") {
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
  // Distinguish a finish from a last breath: labelling an abandoned run's last
  // heartbeat "Last update" is what made a week-old failure look like a success.
  if (run.finishedAt) {
    panel.appendChild(el("div", `Last completed: ${new Date(run.finishedAt).toLocaleString()}`, "muted"));
  } else if (run.updatedAt) {
    panel.appendChild(el("div", `Last activity: ${new Date(run.updatedAt).toLocaleString()} `
      + "(this run did not complete)", "muted"));
  }
}

document.addEventListener("DOMContentLoaded", async () => {
  await loadSettings();
  const { lastRun } = await chrome.storage.local.get("lastRun");
  render(lastRun);
  $("save").addEventListener("click", saveSettings);
  $("sync").addEventListener("click", async () => {
    await saveSettings();
    $("sync").disabled = true;
    // A background script left over from before an update ignores a message it
    // doesn't know, and the button would stay grey with nothing happening (as
    // after the dr-brainrot rename, 12 Oct). Say so instead of failing silently.
    let reply;
    try {
      reply = await chrome.runtime.sendMessage({ type: "dr-brainrot-start" });
    } catch (_) {
      reply = null;
    }
    if (!reply || !reply.ok) {
      $("sync").disabled = false;
      const panel = $("status");
      panel.replaceChildren();
      panel.className = "panel failed";
      panel.appendChild(el("div", "THE SYNC DIDN'T START", "title"));
      panel.appendChild(el("div", "The extension's background script didn't answer, so it is still running an "
        + "older version. Open brave://extensions, click the reload arrow on this extension, reload this "
        + "tab, and press Run weekly sync again."));
    }
  });
  chrome.storage.onChanged.addListener((changes, area) => {
    if (area === "local" && changes.lastRun) render(changes.lastRun.newValue);
  });
});
