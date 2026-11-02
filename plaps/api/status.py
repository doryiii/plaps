"""Health and administration — /api/status is the owner's entire debugging
surface: scan progress, queue depth, errors by stage, ignored-file count, cache
size, the stalled flag, and guard state."""

from __future__ import annotations

import json
import time

from flask import Blueprint, current_app, jsonify

from plaps import config

bp = Blueprint("status", __name__)


def _scan_status(scanner) -> dict:
    db = scanner.db
    checkpoint = db.get_meta("scan_checkpoint")
    stats = json.loads(checkpoint) if checkpoint else {}
    heartbeat = float(stats.get("ts", 0))
    idle = db.get_meta("scanner_idle") == "1"
    stalled = heartbeat > 0 and not idle and time.time() - heartbeat > config.STALL_THRESHOLD
    return {
        "checkpoint": stats,
        "stats": json.loads(db.get_meta("scan_stats", "{}") or "{}"),
        "last_pass": float(db.get_meta("scan_last_pass", 0) or 0),
        "scan_stalled": stalled,
    }


def _queue_depth(db) -> dict:
    rows = db.reader().execute("SELECT kind, state, COUNT(*) AS n FROM jobs GROUP BY kind, state").fetchall()
    depth: dict[str, dict[str, int]] = {}
    for row in rows:
        depth.setdefault(row["kind"], {})[row["state"]] = row["n"]
    return depth


def _error_counts(db) -> dict:
    row = (
        db.reader()
        .execute(
            "SELECT SUM(exif_error IS NOT NULL) AS exif, SUM(thumb_error IS NOT NULL) AS thumb, "
            "SUM(embed_error IS NOT NULL) AS embed FROM photos WHERE deleted_at IS NULL"
        )
        .fetchone()
    )
    return {k: (row[k] or 0) for k in ("exif", "thumb", "embed")}


@bp.get("/api/status")
def status_view():
    app = current_app
    db = app.config["PLAPS_DB"]
    scanner = app.config["PLAPS_SCANNER"]
    cache = app.config["PLAPS_CACHE"]
    return jsonify(
        {
            "scan": _scan_status(scanner),
            "queue": _queue_depth(db),
            "errors": _error_counts(db),
            "ignored_files": int(json.loads(db.get_meta("scan_stats", "{}") or "{}").get("ignored", 0)),
            "cache": {"size_bytes": cache.size()},
            "guard": scanner.guard_state(),
        }
    )


@bp.post("/api/admin/guard/resume")
def guard_resume():
    """Manual acknowledgment after a mass-delete guard trip. Accepts no body."""
    scanner = current_app.config["PLAPS_SCANNER"]
    scanner.resume_guard()
    return jsonify({"ok": True, "guard": scanner.guard_state()})
