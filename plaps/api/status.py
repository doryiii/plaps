"""Health and administration — /api/status is the owner's entire debugging
surface: scan progress, queue depth, errors by stage, ignored-file count, cache
size, the stalled flag, and guard state."""

from __future__ import annotations

import json
import time

from flask import Blueprint, current_app, jsonify

from plaps import config
from plaps.api import error_body

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


def _model_status(db, settings) -> dict:
    """Active vs. configured model and re-embed coverage during a change.

    Coverage counts photos with vectors for the configured model over live
    photos — never `embedded_at`, which says nothing about which model.
    """
    configured = settings.embedding.model_id
    embedded = (
        db.reader()
        .execute(
            "SELECT COUNT(*) FROM photos p WHERE p.deleted_at IS NULL AND EXISTS ("
            "SELECT 1 FROM vectors v WHERE v.photo_id = p.photo_id AND v.model_id = ?)",
            (configured,),
        )
        .fetchone()[0]
    )
    total = db.reader().execute("SELECT COUNT(*) FROM photos WHERE deleted_at IS NULL").fetchone()[0]
    active = db.get_meta("active_model_id")
    return {
        "active_model_id": active,
        "configured_model_id": configured,
        "change_in_progress": active is not None and active != configured,
        "reembed_coverage": {"embedded": embedded, "total": total},
    }


@bp.get("/api/status")
def status_view():
    app = current_app
    db = app.config["PLAPS_DB"]
    scanner = app.config["PLAPS_SCANNER"]
    cache = app.config["PLAPS_CACHE"]
    adapter = app.config["PLAPS_ADAPTER"]
    settings = app.config["PLAPS_SETTINGS"]
    return jsonify(
        {
            "scan": _scan_status(scanner),
            "queue": _queue_depth(db),
            "errors": _error_counts(db),
            "ignored_files": int(json.loads(db.get_meta("scan_stats", "{}") or "{}").get("ignored", 0)),
            "cache": {"size_bytes": cache.size()},
            "guard": scanner.guard_state(),
            "model": _model_status(db, settings),
            "adapter": {
                # the served-model check is the reachability probe (cached 60 s)
                **adapter.probe(),
                "last_embed_success": float(db.get_meta("last_embed_success", 0) or 0),
                "circuit": db.get_meta("embed_circuit", "closed"),
            },
        }
    )


@bp.post("/api/admin/guard/resume")
def guard_resume():
    """Manual acknowledgment after a mass-delete guard trip. Accepts no body."""
    scanner = current_app.config["PLAPS_SCANNER"]
    scanner.resume_guard()
    return jsonify({"ok": True, "guard": scanner.guard_state()})


@bp.post("/api/admin/model/cutover")
def model_cutover():
    """Owner-initiated cutover: rewrite active_model_id, delete old vectors.

    Accepts no body. The flip happened at the restart — the index has been
    serving the configured model all along; this confirms the change and frees
    the old vectors. No threshold, no automatic switch.
    """
    app = current_app
    db = app.config["PLAPS_DB"]
    settings = app.config["PLAPS_SETTINGS"]
    configured = settings.embedding.model_id
    active = db.get_meta("active_model_id")
    if active is None or active == configured:
        return jsonify(error_body("no_model_change", "no model change is in progress")), 409
    with db.writer() as conn:
        conn.execute(
            "INSERT INTO meta(key, value) VALUES('active_model_id', ?) "
            "ON CONFLICT(key) DO UPDATE SET value=excluded.value",
            (configured,),
        )
        conn.execute("DELETE FROM vectors WHERE model_id != ?", (configured,))
    return jsonify(
        {
            "ok": True,
            "active_model_id": configured,
            "deleted_model_ids": [active],
        }
    )
