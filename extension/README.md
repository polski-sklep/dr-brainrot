# dr-brainrot YouTube history sync (unpacked extension)

Captures the data YouTube's own watch-history page is rendered from
(`ytInitialData` plus the `/youtubei/v1/browse` responses loaded while
scrolling) and posts it to the local dr-brainrot backend, which does all parsing.
Works in Chrome and Brave (Manifest V3, Chrome 111+).

## Load it

1. Open `chrome://extensions` (Brave: `brave://extensions`).
2. Turn on **Developer mode**.
3. **Load unpacked** → choose this `extension/` folder.
4. Pin "dr-brainrot YouTube history sync" to the toolbar.

After pulling changes, click the reload arrow on the extension's card in
`chrome://extensions`. The toolbar icon is the dashboard's brain mark
(`BRAIN_OUTLINE` / `BRAIN_FOLDS` in `dashboard/app.py`): outline, midline and
specks only at 16px, where the fold arcs blur; bolder lines at 32 and 48px; the
mark as drawn at 128px.

In Brave, Shields do not block the extension, but if the history page looks
empty, check you are signed in to YouTube in that browser profile.

## Configure (once)

Click the toolbar icon → **Backend settings**:

- **Backend URL**: `http://127.0.0.1:<DR_BRAINROT_DASHBOARD_PORT>` (default 8750). Only
  127.0.0.1 is accepted; reach a remote machine with an SSH/Tailscale port
  forward to that local port.
- **Ingest token**: the value of `DR_BRAINROT_INGEST_TOKEN` from dr-brainrot's `.env`.
- **Page cap**: how many history pages to capture at most (default 8; each
  page is up to ~2 MB, the backend accepts 25 MB).

Settings are stored in `chrome.storage.local` of this browser profile only.

## Weekly run

1. Make sure the dr-brainrot dashboard/backend is running.
2. Click **Run weekly sync**. The extension asks the backend for the cursor
   (the newest video from the previous sync), opens or reloads
   `https://www.youtube.com/feed/history`, and scrolls back until it sees that
   video, runs out of history, or hits the page cap. The first ever sync
   captures roughly the last two weeks.
3. **Keep the history tab visible** while it scrolls: background tabs are
   throttled and may stop loading more history.
4. The popup (you can close and reopen it) shows the result: measurements
   written, warnings, or a red **SYNC FAILED** panel with the reason. A failed
   import keeps the raw capture on the backend so it can be re-parsed later.

## What it does not do

- It does not read the DOM, pixels, or cookies, and it sends nothing anywhere
  except the configured 127.0.0.1 backend.
- It does not compute minutes, progress, or dates for storage; the backend
  does, from the posted page data.
