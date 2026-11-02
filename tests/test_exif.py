"""Unit — valid, malformed, and missing EXIF, pure bytes-in/value-out; the
JPEG-header dimension and orientation readers. date_source tagging, the mtime
fallback, and its timezone conversion live in the scanner tests."""

from datetime import datetime

from conftest import jpeg_bytes

from plaps import exif


def test_valid_date():
    data = jpeg_bytes(exif_date="2021:07:03 10:20:30")
    taken, error = exif.date_taken(data)
    assert error is None
    assert taken == datetime(2021, 7, 3, 10, 20, 30)


def test_missing_date_is_not_an_error():
    taken, error = exif.date_taken(jpeg_bytes())
    assert taken is None
    assert error is None  # the mtime fallback applies, without an error


def test_malformed_date():
    data = jpeg_bytes(exif_date="not a date")
    taken, error = exif.date_taken(data)
    assert taken is None
    assert error == "malformed DateTimeOriginal"


def test_impossible_date():
    data = jpeg_bytes(exif_date="2021:13:45 99:99:99")
    taken, error = exif.date_taken(data)
    assert taken is None
    assert error == "malformed DateTimeOriginal"


def test_unreadable_exif():
    taken, error = exif.date_taken(b"\xff\xd8\xff\xe0garbage-not-jpeg")
    assert taken is None
    assert error == "unreadable EXIF"


def test_dimensions_from_header():
    assert exif.dimensions(jpeg_bytes(width=640, height=480)) == (640, 480)


def test_dimensions_of_non_jpeg():
    assert exif.dimensions(b"not a jpeg at all") == (None, None)


def test_orientation_present_and_absent():
    assert exif.orientation(jpeg_bytes(orientation=6)) == 6
    assert exif.orientation(jpeg_bytes()) == 1


def test_never_raises_on_random_bytes():
    for blob in (b"", b"\x00" * 32, b"\xff\xd8", b"\xff\xd8\xff\xdb"):
        exif.date_taken(blob)
        exif.dimensions(blob)
        exif.orientation(blob)


def test_dimensions_eoi_without_frame():
    assert exif.dimensions(b"\xff\xd8\xff\xd9") == (None, None)


def test_dimensions_skips_non_marker_bytes():
    assert exif.dimensions(b"\xff\xd8" + b"\x00" * 8) == (None, None)


def test_dimensions_truncated_segment():
    # a SOF marker whose segment is too short to carry dimensions
    assert exif.dimensions(b"\xff\xd8\xff\xc0\x00\x02") == (None, None)
