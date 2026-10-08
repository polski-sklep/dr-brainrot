"""Regenerate the committed YouTube fixtures from the real saved history page.

    .venv/bin/python tests/fixtures/youtube/sanitise_real_page.py

Reads `/Users/Jacob/Projects/dr-brainrot/inputs/Watch history - YouTube.html` (never
committed) and writes `history_initial.json` and `history_continuation.json`
next to this file. Personal data policy: CONTRACTS.md Part II §12.

How it sanitises:

* A handful of real sections and items are selected (see `INITIAL` and
  `CONTINUATION`); their JSON shape is kept as rendered by YouTube.
* Video ids and channel ids are replaced with synthetic ones
  (`fxVideo0001`, `UCfixtureChannel00000001`) everywhere they occur.
* Titles and channel names become "Fixture video N" / "Fixture channel N".
* Every other string is kept only if it is structural (an ALL_CAPS enum, an
  API path, a date header, a fixed UI word); anything else becomes
  "redacted". URLs are rebuilt from synthetic ids.
* Bulky subtrees the parser never reads (menus, share sheets, colour
  palettes) are dropped and image source lists trimmed to one entry, to keep
  the fixture small. Progress values are set to chosen synthetic numbers.
* After writing, every real video id, channel id and title from the *whole*
  page is searched for in the output, and the script fails if any is found.

The continuation fixture wraps real (sanitised) sections in the browse
continuation envelope `onResponseReceivedActions[].appendContinuationItemsAction
.continuationItems`. That envelope was NOT observed in a real capture (only
the initial page was available); see NOTES-A.md.

The script prints only counts and synthetic identifiers.
"""
from __future__ import annotations

import copy
import json
import pathlib
import re
import sys

HERE = pathlib.Path(__file__).resolve().parent
ROOT = HERE.parents[2]
sys.path.insert(0, str(ROOT))

from sources import youtube  # noqa: E402

REAL = pathlib.Path("/Users/Jacob/Projects/dr-brainrot/inputs/Watch history - YouTube.html")

# (real section index, [("shelf", n_shorts_kept) | ("item", item_index, percent_or_None)])
INITIAL = [
    (0, [("shelf", 2)]),                                   # Today: Shorts only
    (1, [("shelf", 1), ("item", 1, 45), ("item", 2, 100)]),  # Yesterday
    (2, [("item", 1, 100), ("item", 2, 10)]),              # Monday: collaboration avatar stack; a 10% floor value
    (5, [("item", 1, 67), ("item", 6, None)]),             # Friday: a LIVE lockup with no progress bar (real)
    (6, [("shelf", 2)]),                                   # Thursday
    (7, [("shelf", 1), ("item", 1, 92)]),                  # Sep 9
]
CONTINUATION = [
    (21, [("item", 0, 100), ("item", 4, 100)]),            # Aug 25
    (22, [("item", 0, 100), ("item", 1, 30)]),             # Aug 24: item 0 repeats Aug 25 item 4 (real)
]

DROP_KEYS = {"menuButton", "menuOnTap", "themedPalette", "frameworkUpdates", "topbar", "overlay",
             "loggingContext", "loggingDirectives"}
TRIM_LISTS = {"sources", "thumbnails"}

KEEP_WORDS = {
    "Today", "Yesterday", "Monday", "Tuesday", "Wednesday", "Thursday", "Friday", "Saturday", "Sunday",
    "Shorts", "LIVE", "Now playing", "Watch later", "Add to queue", "Added", "More actions",
    "Send feedback", "Collaborators", "Collaboration channels", "lockup", "shelf", "WL", " • ",
    "browse-feedFEhistory",
}
HEADER_RE = re.compile(r"^[A-Z][a-z]{2} \d{1,2}(, \d{4})?$")
ENUM_RE = re.compile(r"^[A-Z][A-Z0-9_]+$")
BADGE_RE = re.compile(r"^\d+(:\d\d)+$")


def find(o, key):
    if isinstance(o, dict):
        for k, v in o.items():
            if k == key:
                yield v
            yield from find(v, key)
    elif isinstance(o, list):
        for v in o:
            yield from find(v, key)


class Sanitiser:
    def __init__(self):
        self.videos: dict[str, str] = {}
        self.channels: dict[str, str] = {}
        self.n_titles = 0

    def vid(self, real: str) -> str:
        return self.videos.setdefault(real, f"fxVideo{len(self.videos) + 1:04d}")

    def chan(self, real: str) -> str:
        return self.channels.setdefault(real, f"UCfixtureChannel{len(self.channels) + 1:08d}")

    def prepare_lockup(self, lv: dict, percent):
        vid = self.vid(lv["contentId"])
        n = int(vid[-4:])
        meta = lv["metadata"]["lockupMetadataViewModel"]
        meta["title"]["content"] = f"Fixture video {n}"
        for b in find(meta.get("image"), "browseEndpoint"):
            self.chan(b["browseId"])
        rows = meta["metadata"]["contentMetadataViewModel"]["metadataRows"]
        rows[0]["metadataParts"][0]["text"]["content"] = f"Fixture channel {n}"
        for part in rows[0]["metadataParts"][1:]:
            part["text"]["content"] = "1K views"
        for li in find(meta.get("image"), "listItemViewModel"):
            li["title"]["content"] = f"Fixture collaborator {n}"
        bars = list(find(lv["contentImage"], "thumbnailOverlayProgressBarViewModel"))
        if percent is None:
            assert not bars, "expected the chosen item to have no progress bar"
        else:
            assert bars, "expected the chosen item to have a progress bar"
            for bar in bars:
                bar["startPercent"] = percent
        for we in find(lv.get("rendererContext"), "watchEndpoint"):
            if "startTimeSeconds" in we:
                we["startTimeSeconds"] = max(1, round((percent or 0) / 100 * 600) - 25)

    def prepare_short(self, sv: dict):
        vid = self.vid(sv["onTap"]["innertubeCommand"]["reelWatchEndpoint"]["videoId"])
        n = int(vid[-4:])
        sv["overlayMetadata"]["primaryText"]["content"] = f"Fixture short {n}"
        sv["overlayMetadata"]["secondaryText"]["content"] = "1K views"
        sv["accessibilityText"] = f"Fixture short {n}"

    def select(self, secs, spec):
        out = []
        for sn, picks in spec:
            sec = copy.deepcopy(secs[sn])
            items = []
            for pick in picks:
                if pick[0] == "shelf":
                    shelf = next(i for i in sec["contents"] if "reelShelfRenderer" in i)
                    shelf["reelShelfRenderer"]["items"] = shelf["reelShelfRenderer"]["items"][:pick[1]]
                    for si in shelf["reelShelfRenderer"]["items"]:
                        self.prepare_short(si["shortsLockupViewModel"])
                    items.append(shelf)
                else:
                    item = sec["contents"][pick[1]]
                    self.prepare_lockup(item["lockupViewModel"], pick[2])
                    items.append(item)
            sec["contents"] = items
            out.append({"itemSectionRenderer": sec})
        return out

    def substitute_ids(self, s: str) -> str:
        for real, fake in {**self.videos, **self.channels}.items():
            if real in s:
                s = s.replace(real, fake)
        return s

    def clean_string(self, s: str) -> str:
        s = self.substitute_ids(s)
        fake_vid = re.search(r"fxVideo\d{4}", s)
        fake_chan = re.search(r"UCfixtureChannel\d{8}", s)
        if s.startswith("Fixture ") or s in KEEP_WORDS or HEADER_RE.match(s) or ENUM_RE.match(s):
            return s
        if s in ("1K views",) or re.fullmatch(r"fxVideo\d{4}|UCfixtureChannel\d{8}", s):
            return s
        if BADGE_RE.match(s):
            return "10:00"
        if s.startswith("/youtubei/"):
            return s
        if fake_vid:
            v = fake_vid.group(0)
            if s.startswith("/watch"):
                return f"/watch?v={v}"
            if s.startswith("/shorts/"):
                return f"/shorts/{v}"
            if s.startswith("history-shorts-shelf-item-"):
                return f"history-shorts-shelf-item-{v}"
            if s.startswith("http"):
                return f"https://i.ytimg.com/vi/{v}/hqdefault.jpg"
            return "redacted"
        if fake_chan:
            if s.startswith("/channel/"):
                return f"/channel/{fake_chan.group(0)}"
            return "redacted"
        if s.startswith("/@"):
            return "/@fixturechannel"
        if s.startswith("http"):
            return "https://example.invalid/redacted"
        return "redacted"

    def clean(self, o):
        if isinstance(o, dict):
            return {k: (self.clean(v)[:1] if k in TRIM_LISTS and isinstance(v, list) else self.clean(v))
                    for k, v in o.items() if k not in DROP_KEYS}
        if isinstance(o, list):
            return [self.clean(v) for v in o]
        if isinstance(o, str):
            return self.clean_string(o)
        return o


def continuation_item(real_sl):
    cont = copy.deepcopy(real_sl["contents"][-1])
    assert "continuationItemRenderer" in cont
    return cont


def main() -> int:
    if not REAL.exists():
        print("real input not present; nothing to do")
        return 1
    html = REAL.read_text(encoding="utf-8")
    data = youtube.extract_initial_data(html)
    sl = data["contents"]["twoColumnBrowseResultsRenderer"]["tabs"][0]["tabRenderer"]["content"]["sectionListRenderer"]
    secs = [c["itemSectionRenderer"] for c in sl["contents"] if "itemSectionRenderer" in c]

    s = Sanitiser()
    initial_sections = s.select(secs, INITIAL)
    cont_sections = s.select(secs, CONTINUATION)

    initial = {
        "responseContext": {"maxAgeSeconds": 0},
        "contents": {"twoColumnBrowseResultsRenderer": {"tabs": [{"tabRenderer": {
            "selected": True,
            "content": {"sectionListRenderer": {
                "contents": initial_sections + [continuation_item(sl)],
                "targetId": "browse-feedFEhistory",
            }},
        }}]}},
        "trackingParams": "redacted",
    }
    continuation = {
        "responseContext": {"maxAgeSeconds": 0},
        "trackingParams": "redacted",
        "onResponseReceivedActions": [{
            "clickTrackingParams": "redacted",
            "appendContinuationItemsAction": {
                "continuationItems": cont_sections + [continuation_item(sl)],
                "targetId": "browse-feedFEhistory",
            },
        }],
    }
    initial, continuation = s.clean(initial), s.clean(continuation)

    # Leak check against the whole real page, not just the selection.
    real_ids = set(find(data, "contentId")) | set(find(data, "videoId"))
    real_channels = {b for b in find(data, "browseId") if isinstance(b, str) and b.startswith("UC")}
    real_titles = {t.get("content") for t in find(data, "title") if isinstance(t, dict) and t.get("content")}
    real_titles |= {t.get("content") for t in find(data, "primaryText") if isinstance(t, dict)}
    real_titles = {t for t in real_titles if t and len(t) >= 8 and t not in KEEP_WORDS}
    for name, obj in (("history_initial.json", initial), ("history_continuation.json", continuation)):
        text = json.dumps(obj, indent=1, ensure_ascii=False, sort_keys=False)
        leaks = [x for x in real_ids | real_channels if x in text] + [t for t in real_titles if t in text]
        if leaks:
            print(f"{name}: {len(leaks)} real identifiers survived sanitisation; not written")
            return 2
        (HERE / name).write_text(text + "\n", encoding="utf-8")
        print(f"wrote {name}: {len(text)} bytes")
    print(f"videos mapped: {len(s.videos)}, channels mapped: {len(s.channels)}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
