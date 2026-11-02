"""Pure EXIF and JPEG-header functions: bytes in, values out, never raises.

The mtime fallback and its timezone conversion live in the scanner, not here —
this module holds only the pure logic so unit tests can hit malformed inputs
without files or a database.
"""

from __future__ import annotations

import re
from datetime import datetime

import piexif

_EXIF_FORMAT = re.compile(r"^(\d{4}):(\d{2}):(\d{2}) (\d{2}):(\d{2}):(\d{2})$")

# SOF markers carry the frame dimensions. C4 (DHT), C8 (JPG), CC (DAC) do not.
_SOF_MARKERS = {0xC0, 0xC1, 0xC2, 0xC3, 0xC5, 0xC6, 0xC7, 0xC9, 0xCA, 0xCB, 0xCD, 0xCE, 0xCF}


def date_taken(data: bytes) -> tuple[datetime | None, str | None]:
    """Read EXIF DateTimeOriginal.

    Returns (datetime, None) on success, (None, None) when the tag is absent
    (the mtime fallback applies without an error), and (None, message) when EXIF
    is present but unreadable or the date is malformed.
    """
    try:
        exif = piexif.load(data)
    except Exception:
        return None, "unreadable EXIF"
    raw = exif.get("Exif", {}).get(piexif.ExifIFD.DateTimeOriginal)
    if raw is None:
        return None, None
    try:
        text = raw.decode("ascii") if isinstance(raw, bytes) else str(raw)
    except UnicodeDecodeError:
        return None, "malformed DateTimeOriginal"
    match = _EXIF_FORMAT.match(text.strip())
    if not match:
        return None, "malformed DateTimeOriginal"
    try:
        parsed = datetime(
            int(match[1]),
            int(match[2]),
            int(match[3]),
            int(match[4]),
            int(match[5]),
            int(match[6]),
        )
    except ValueError:
        return None, "malformed DateTimeOriginal"
    return parsed, None


def orientation(data: bytes) -> int:
    """EXIF Orientation tag: 1 (upright) when absent or unreadable."""
    try:
        exif = piexif.load(data)
    except Exception:
        return 1
    raw = exif.get("0th", {}).get(piexif.ImageIFD.Orientation)
    if raw is None:
        return 1
    try:
        value = int(raw)
    except (TypeError, ValueError):
        return 1
    return value if 1 <= value <= 8 else 1


def dimensions(data: bytes) -> tuple[int | None, int | None]:
    """JPEG frame dimensions from the first SOF marker: (width, height).

    No decode — the marker segment is parsed directly. (None, None) when the
    bytes are not a parseable JPEG (the scanner records an exif_error).
    """
    if len(data) < 4 or data[0:2] != b"\xff\xd8":
        return None, None
    pos = 2
    size = len(data)
    while pos + 4 <= size:
        if data[pos] != 0xFF:
            pos += 1
            continue
        marker = data[pos + 1]
        if marker in (0xD8, 0x01) or 0xD0 <= marker <= 0xD7:
            pos += 2
            continue
        if marker == 0xD9:  # EOI without a frame
            return None, None
        if pos + 4 > size:
            break
        seg_len = (data[pos + 2] << 8) | data[pos + 3]
        if marker in _SOF_MARKERS:
            if pos + 9 <= size and seg_len >= 7:
                height = (data[pos + 5] << 8) | data[pos + 6]
                width = (data[pos + 7] << 8) | data[pos + 8]
                return width, height
            return None, None
        pos += 2 + seg_len
    return None, None
