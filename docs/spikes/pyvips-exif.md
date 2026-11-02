# Spike: pyvips EXIF reading and thumbnail orientation

* **Date:** 2026-10-30
* **Status:** Verified on real library photos
* **Feeds:** `plaps/exif.py`, `plaps/images.py`

## Environment

* libvips **8.18.7** (system package), pyvips **3.2.0** (Python binding), Python 3.14.
* Test subjects: 8 real photos from `/tmp/2021/` (Sony ILCE-7M3 JPEGs, phone JPEGs).

## Result 1 — `DateTimeOriginal` is awkward in pyvips; piexif choice confirmed

* The field lives at **`exif-ifd2-DateTimeOriginal`** (libvips exposes the EXIF sub-IFD as `ifd2`), *not* `exif-ifd0-DateTimeOriginal` — the ifd0 name silently returns nothing.
* Values come back **decorated**: `'2021:04:14 15:06:47 (2021:04:14 15:06:47, ASCII, 20 components, 20 bytes)'`. The raw value is embedded in a descriptive string and would need fragile parsing.
* Reading a missing field **raises** a catchable `pyvips.Error` rather than returning None.

This validates the design's decision to read dates with **piexif** (pure-Python, returns raw tag values) and keep pyvips for pixels only.

## Result 2 — timezone offsets exist in these files (noted, not acted on)

The Sony files carry `exif-ifd2-OffsetTimeOriginal = '-07:00'`. The design treats EXIF timestamps as local wall-clock and rejects UTC normalization for this single-user library; these offsets are recorded as a possible future refinement (e.g., for the map-view phase), not a design change.

## Result 3 — auto-rotation verified

A synthesized orientation-6 (90° CW) file, thumbnailed via `pyvips.Image.thumbnail(path, 512)`:

* default → **464×512** (portrait — auto-rotation applied)
* `no_rotate=True` → 512×464 (raw landscape)

`Image.thumbnail()` auto-rotates by default; `resize()` would not. The implementation must use `thumbnail()` for every derived image — this was flagged as a trap in review and is now verified on real files.

## Result 4 — square-crop variant behavior

On a 4000×1869 landscape source:

* `thumbnail(p, 512, height=512)` → **512×239** (fits within the box — *not* a square)
* `thumbnail(p, 512, height=512, crop=Interesting.CENTRE)` → **512×512** (exact square, center crop)
* `crop=Interesting.ATTENTION` → 512×512 (salience-weighted crop)

The design's `sq` variant must pass `crop=Interesting.CENTRE` (or `ATTENTION` — a draft-level choice); `as` (justified rows) uses the plain max-edge call.

## Result 5 — stripped/malformed EXIF is survivable

A JPEG saved with `strip=True` (no EXIF at all) still thumbnails normally; only the EXIF field read raises. This maps directly onto the design's error path: missing/malformed EXIF → per-file error row + mtime fallback, thumbnails unaffected.
