"""On-device text recognition with Apple Vision. The only module importing pyobjc.

DECISIONS DR0.4: Screen Time screenshots never leave the Mac. Vision's
`VNRecognizeTextRequest` runs locally, so this module makes no network call
and has no configuration that could make one.

pyobjc is imported lazily inside the functions, not at module import. That
keeps `import sources.screentime` cheap and importable on a machine (or a test
run) where Vision is unavailable; the failure only happens if OCR is actually
attempted, and the ingest turns it into a failed import with the raw images
kept for `reparse`.

Settings chosen against the real screenshots (NOTES-B.md):

* **Accurate** recognition level. The fast level merged "1h 2m" into "1h2m" and
  lost single characters entirely.
* **Language correction ON.** On the real Screen Time images it changed no
  duration token and fixed app-name confusions ("Linkedln" -> "LinkedIn"); with
  it off, the device name also read inconsistently between pages. App names
  that are not dictionary words still came through verbatim.
"""
from __future__ import annotations

from dataclasses import dataclass


@dataclass(frozen=True)
class TextObservation:
    """One recognised line of text. Coordinates normalised 0..1, origin top-left.

    Vision reports boxes with a bottom-left origin; they are flipped here, once,
    so every parser reads the page the way a person does (y grows downwards).
    `origin` is 'page' for the full-page pass and 'region' for a targeted
    re-read of a crop (see `recognize_region`), so a parse can say which figures
    needed the second look.
    """

    text: str
    confidence: float
    x: float
    y: float
    w: float
    h: float
    origin: str = "page"

    def to_dict(self) -> dict:
        return {"text": self.text, "confidence": self.confidence, "x": self.x, "y": self.y,
                "w": self.w, "h": self.h, "origin": self.origin}

    @classmethod
    def from_dict(cls, d: dict) -> "TextObservation":
        return cls(str(d["text"]), float(d["confidence"]), float(d["x"]), float(d["y"]),
                   float(d["w"]), float(d["h"]), str(d.get("origin", "page")))


class OcrError(Exception):
    """Vision could not process the image (undecodable bytes, framework failure)."""


def _request(Vision):
    req = Vision.VNRecognizeTextRequest.alloc().init()
    req.setRecognitionLevel_(Vision.VNRequestTextRecognitionLevelAccurate)
    req.setUsesLanguageCorrection_(True)
    return req


def _cg_image(image_bytes: bytes):
    import Quartz  # noqa: PLC0415
    from Foundation import NSData  # noqa: PLC0415

    ns = NSData.dataWithBytes_length_(image_bytes, len(image_bytes))
    src = Quartz.CGImageSourceCreateWithData(ns, None)
    img = Quartz.CGImageSourceCreateImageAtIndex(src, 0, None) if src is not None else None
    if img is None:
        raise OcrError("the image could not be decoded")
    return img


def _perform(Vision, cg_image) -> list:
    handler = Vision.VNImageRequestHandler.alloc().initWithCGImage_options_(cg_image, None)
    req = _request(Vision)
    ok, err = handler.performRequests_error_([req], None)
    if not ok:
        raise OcrError(f"Vision text recognition failed: {err}")
    return list(req.results() or [])


def _to_observation(result, *, ox=0.0, oy=0.0, sx=1.0, sy=1.0, origin="page") -> TextObservation | None:
    candidates = result.topCandidates_(1)
    if not candidates:
        return None
    best = candidates[0]
    box = result.boundingBox()
    # Bottom-left -> top-left, then map from crop space into page space.
    x = box.origin.x
    y = 1.0 - box.origin.y - box.size.height
    return TextObservation(
        text=str(best.string()),
        confidence=float(best.confidence()),
        x=ox + x * sx, y=oy + y * sy, w=box.size.width * sx, h=box.size.height * sy,
        origin=origin,
    )


def recognize(image_bytes: bytes) -> list[TextObservation]:
    """Every text line Vision finds on the page, top-left coordinates."""
    import Vision  # noqa: PLC0415

    img = _cg_image(image_bytes)
    out = [_to_observation(r) for r in _perform(Vision, img)]
    return [o for o in out if o is not None]


def recognize_region(image_bytes: bytes, x: float, y: float, w: float, h: float) -> list[TextObservation]:
    """Re-read one rectangle of the page (normalised, top-left) on its own.

    Why this exists: on the real screenshots Vision's full-page pass never
    returned a one-character app name ("X") although it read that row's
    duration. The same row cropped on its own is read correctly. Coordinates of
    the results are mapped back to the full page.
    """
    import Quartz  # noqa: PLC0415
    import Vision  # noqa: PLC0415

    img = _cg_image(image_bytes)
    W, H = Quartz.CGImageGetWidth(img), Quartz.CGImageGetHeight(img)
    x0, y0 = max(0.0, x), max(0.0, y)
    x1, y1 = min(1.0, x + w), min(1.0, y + h)
    if x1 <= x0 or y1 <= y0:
        return []
    # CGImageCreateWithImageInRect uses pixel coordinates with a top-left origin.
    crop = Quartz.CGImageCreateWithImageInRect(img, Quartz.CGRectMake(x0 * W, y0 * H, (x1 - x0) * W, (y1 - y0) * H))
    if crop is None:
        return []
    out = [_to_observation(r, ox=x0, oy=y0, sx=x1 - x0, sy=y1 - y0, origin="region")
           for r in _perform(Vision, crop)]
    return [o for o in out if o is not None]
