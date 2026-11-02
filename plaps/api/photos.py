"""Timeline endpoints: cursor-paged windows over live photos.

The composite cursor (base64url of "<date_taken>|<photo_id>") is what makes
windows stable while the scanner inserts — keyset pagination is skip-free, so
concurrent inserts never produce skipped or repeated entries in the fetch
stream. Optional day-inclusive date bounds let the client jump to a date in one
request instead of hundreds of cursor pages. The folder endpoint reuses the
same window machinery with a subtree predicate.
"""

from __future__ import annotations

import base64
from datetime import date, timedelta

from flask import Blueprint, current_app, jsonify, request

from plaps.api import error_body, photo_card

bp = Blueprint("photos", __name__)

DEFAULT_LIMIT = 100
MAX_LIMIT = 500


def encode_cursor(date_taken: str, photo_id: int) -> str:
    raw = f"{date_taken}|{photo_id}".encode()
    return base64.urlsafe_b64encode(raw).decode().rstrip("=")


def decode_cursor(cursor: str) -> tuple[str, int] | None:
    try:
        padded = cursor + "=" * (-len(cursor) % 4)
        date_taken, _, photo_id = base64.urlsafe_b64decode(padded).decode().partition("|")
        return date_taken, int(photo_id)
    except (ValueError, UnicodeDecodeError):
        return None


def _date_bounds() -> tuple[str | None, str | None]:
    """Day-inclusive after/before as a half-open raw-string range — no function
    wraps the indexed column."""
    after = request.args.get("after")
    before = request.args.get("before")
    low = high = None
    if after:
        try:
            low = date.fromisoformat(after).strftime("%Y-%m-%dT00:00:00")
        except ValueError:
            return "invalid", None
    if before:
        try:
            high = (date.fromisoformat(before) + timedelta(days=1)).strftime("%Y-%m-%dT00:00:00")
        except ValueError:
            return None, "invalid"
    return low, high


def parse_window_args() -> tuple[str, int] | tuple[dict, None]:
    """Shared limit/order validation; the error variant is a JSON error body."""
    order = request.args.get("order", "desc")
    if order not in ("asc", "desc"):
        return error_body("bad_request", "order must be asc or desc"), 400
    try:
        limit = int(request.args.get("limit", DEFAULT_LIMIT))
    except ValueError:
        return error_body("bad_request", "limit must be an integer"), 400
    if not 1 <= limit <= MAX_LIMIT:
        return error_body("bad_request", f"limit must be 1..{MAX_LIMIT}"), 400
    return order, limit


def fetch_window(db, extra_clauses: list[str], extra_params: list, order: str, limit: int) -> dict:
    """One keyset window over live photos with extra scoping predicates."""
    cursor = request.args.get("cursor")
    anchor = None
    if cursor:
        anchor = decode_cursor(cursor)
        if anchor is None:
            return {"error": ("bad_request", "malformed cursor")}
    low, high = _date_bounds()
    if low == "invalid" or high == "invalid":
        return {"error": ("bad_request", "after/before must be ISO dates")}

    clauses = ["deleted_at IS NULL", *extra_clauses]
    params: list = list(extra_params)
    if low:
        clauses.append("date_taken >= ?")
        params.append(low)
    if high:
        clauses.append("date_taken < ?")
        params.append(high)
    if anchor is not None:
        date_taken, photo_id = anchor
        if order == "desc":
            clauses.append("(date_taken < ? OR (date_taken = ? AND photo_id < ?))")
        else:
            clauses.append("(date_taken > ? OR (date_taken = ? AND photo_id > ?))")
        params.extend([date_taken, date_taken, photo_id])

    sql = (
        "SELECT * FROM photos WHERE "
        + " AND ".join(clauses)
        + " ORDER BY date_taken "
        + ("DESC" if order == "desc" else "ASC")
        + ", photo_id "
        + ("DESC" if order == "desc" else "ASC")
        + " LIMIT ?"
    )
    params.append(limit)
    rows = db.reader().execute(sql, params).fetchall()

    next_cursor = None
    if len(rows) == limit:
        last = rows[-1]
        next_cursor = encode_cursor(last["date_taken"], last["photo_id"])
    return {"photos": [photo_card(row) for row in rows], "next_cursor": next_cursor}


@bp.get("/api/photos")
def timeline_window():
    db = current_app.config["PLAPS_DB"]
    parsed = parse_window_args()
    if isinstance(parsed, tuple) and isinstance(parsed[0], dict):
        body, code = parsed
        return jsonify(body), code
    order, limit = parsed
    result = fetch_window(db, [], [], order, limit)
    if "error" in result:
        code, message = result["error"]
        return jsonify(error_body(code, message)), 400
    return jsonify(result)
