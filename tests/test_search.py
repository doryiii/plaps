"""Synthetic-vector ranking order; filter-before-rank; filter-scoped coverage;
silent exclusion of un-embedded photos; the three result states (endpoint down
→ 503) including the min_score floor and min_matches top-up; index freshness
without restarts; and the flip-at-start model change with its cutover."""

import numpy as np
import pytest
from conftest import jpeg_bytes, set_mtime_ns

from plaps.embed.adapter import EndpointUnreachable, ModelMismatch


def search(harness, **params):
    return harness.client.get("/api/search", query_string={"q": "query", **params})


def orthogonal(vector):
    """A unit vector orthogonal to the given one (cosine 0, below any floor)."""
    array = np.asarray(vector, dtype=np.float32)
    basis = np.zeros_like(array)
    if abs(array[0]) > 0.5:
        basis[1] = 1.0
    else:
        basis[0] = 1.0
    basis -= array * (float(array @ basis) / float(array @ array))
    return (basis / np.linalg.norm(basis)).tolist()


def seed_and_embed(harness, library, spec):
    """spec: {rel_path: (exif_date, image_vector)}; registers vectors on the
    fake adapter so drain() embeds them deterministically."""
    for rel_path, (date, vector) in spec.items():
        content = jpeg_bytes(width=32, height=32, exif_date=date)
        library.add(rel_path, content=content)
        harness.adapter.register_image(content, vector)
    harness.scan()
    harness.drain()


def test_ranking_order_and_scores(harness, library):
    dog = [1.0, 0.0]
    puppy = [0.9, 0.1]
    cat = orthogonal(dog)
    seed_and_embed(
        harness,
        library,
        {
            "dog.jpg": ("2021:01:01 00:00:00", dog),
            "puppy.jpg": ("2021:01:02 00:00:00", puppy),
            "cat.jpg": ("2021:01:03 00:00:00", cat),
        },
    )
    harness.adapter.register_text("dog", dog)
    body = search(harness, q="dog").get_json()
    assert body["state"] == "ok"
    dates = [card["date_taken"] for card in body["photos"]]
    assert dates[:2] == ["2021-01-01T00:00:00", "2021-01-02T00:00:00"]  # best first
    scores = [card["score"] for card in body["photos"]]
    assert scores == sorted(scores, reverse=True)
    floor = harness.settings.embedding.min_score
    assert scores[0] >= floor and scores[1] >= floor
    assert scores[2] < floor  # the cat rides along as a top-up result only


def test_filter_before_rank(harness, library):
    from plaps import paths

    seed_and_embed(
        harness,
        library,
        {
            "beach/dog.jpg": ("2021:01:01 00:00:00", [1.0, 0.0]),
            "city/dog.jpg": ("2021:01:02 00:00:00", [1.0, 0.0]),
        },
    )
    harness.adapter.register_text("dog", [1.0, 0.0])
    body = search(harness, q="dog", folder=paths.encode_folder_id("beach")).get_json()
    assert len(body["photos"]) == 1
    assert body["photos"][0]["date_taken"] == "2021-01-01T00:00:00"
    # date bounds apply before ranking too
    body = search(harness, q="dog", after="2021-01-02", before="2021-01-02").get_json()
    assert [card["date_taken"] for card in body["photos"]] == ["2021-01-02T00:00:00"]


def test_coverage_counts_are_filter_scoped(harness, library):
    from plaps import paths

    seed_and_embed(
        harness,
        library,
        {
            "beach/dog.jpg": ("2021:01:01 00:00:00", [1.0, 0.0]),
            "city/cat.jpg": ("2021:01:02 00:00:00", [0.0, 1.0]),
        },
    )
    # a third photo exists in the filter but is not embedded yet
    library.add("beach/unembedded.jpg", exif_date="2021:01:03 00:00:00")
    harness.scan()
    harness.adapter.register_text("dog", [1.0, 0.0])
    body = search(harness, q="dog", folder=paths.encode_folder_id("beach")).get_json()
    assert body["coverage"] == {"embedded": 1, "total": 2}
    assert len(body["photos"]) == 1  # the un-embedded photo is silently excluded


def test_three_states(harness, library):
    dog = [1.0, 0.0]
    seed_and_embed(harness, library, {"dog.jpg": ("2021:01:01 00:00:00", dog)})
    harness.adapter.register_text("dog", dog)
    harness.adapter.register_text("taxes", orthogonal(dog))

    # ok: at least one photo reached the floor
    assert search(harness, q="dog").get_json()["state"] == "ok"

    # no_matches: things were searchable, nothing reached the floor — and the
    # response still carries the fallback top-up results
    body = search(harness, q="taxes").get_json()
    assert body["state"] == "no_matches"
    assert body["coverage"]["embedded"] == 1
    assert len(body["photos"]) == 1  # the closest photo, labeled by the UI

    # empty_filter: nothing embedded inside the active filter
    library.add("empty/photo.jpg", exif_date="2021:02:01 00:00:00")
    harness.scan()
    from plaps import paths

    body = search(harness, q="dog", folder=paths.encode_folder_id("empty")).get_json()
    assert body["state"] == "empty_filter"
    assert body["photos"] == []
    assert body["coverage"] == {"embedded": 0, "total": 1}


def test_min_matches_top_up(harness, library):
    query = [1.0, 0.0]
    spec = {
        f"p{i:02d}.jpg": (
            f"2021:01:{i + 1:02d} 00:00:00",
            query if i < 2 else orthogonal(query),
        )
        for i in range(12)
    }
    seed_and_embed(harness, library, spec)
    harness.adapter.register_text("half", query)
    floor = harness.settings.embedding.min_score

    body = search(harness, q="half").get_json()
    assert body["state"] == "ok"  # two photos reached the floor
    assert len(body["photos"]) == 10  # 2 matches + 8 next-closest (min_matches)
    assert body["photos"][0]["score"] >= floor
    assert body["photos"][1]["score"] >= floor
    assert body["photos"][2]["score"] < floor  # the top-up boundary
    assert len(body["coverage"]) == 2 and body["coverage"]["total"] == 12


@pytest.mark.parametrize("failure", [EndpointUnreachable("down"), ModelMismatch("wrong")])
def test_endpoint_down_returns_503_never_empty(harness, library, failure):
    library.add("a.jpg", exif_date="2021:01:01 00:00:00")
    harness.scan()
    harness.adapter.fail_text_with = failure
    response = search(harness)
    assert response.status_code == 503
    body = response.get_json()
    assert body["state"] == "endpoint_down"
    assert body["error"]["code"] == "endpoint_down"


def test_validation(harness, library):
    assert harness.client.get("/api/search").status_code == 400  # q is required
    assert harness.client.get("/api/search?q=x&limit=0").status_code == 400
    assert harness.client.get("/api/search?q=x&folder=!!!").status_code == 400
    assert harness.client.get("/api/search?q=x&after=nope").status_code == 400


def test_index_freshness_without_restart(harness, library):
    harness.adapter.register_text("dog", [1.0, 0.0])
    content = jpeg_bytes(width=32, height=32, exif_date="2021:01:01 00:00:00")
    harness.adapter.register_image(content, [1.0, 0.0])
    library.add("fresh.jpg", content=content)
    harness.scan()
    assert search(harness, q="dog").get_json()["state"] == "empty_filter"
    harness.drain()  # the worker embeds — the index picks it up live
    body = search(harness, q="dog").get_json()
    assert body["state"] == "ok"
    assert len(body["photos"]) == 1


def test_tombstoned_photo_stops_ranking_without_restart(harness, library):
    content = jpeg_bytes(width=32, height=32, exif_date="2021:01:01 00:00:00")
    harness.adapter.register_image(content, [1.0, 0.0])
    harness.adapter.register_text("dog", [1.0, 0.0])
    library.add("bye.jpg", content=content)
    harness.scan()
    harness.drain()
    assert len(search(harness, q="dog").get_json()["photos"]) == 1
    library.delete("bye.jpg")
    harness.scan()  # the tombstone masks the id in the resident index
    body = search(harness, q="dog").get_json()
    assert body["photos"] == []
    assert body["coverage"] == {"embedded": 0, "total": 0}


def test_replaced_photo_old_id_stops_ranking(harness, library):
    content = jpeg_bytes(width=32, height=32, exif_date="2021:01:01 00:00:00")
    harness.adapter.register_image(content, [1.0, 0.0])
    harness.adapter.register_text("dog", [1.0, 0.0])
    library.add("edit.jpg", content=content)
    harness.scan()
    harness.drain()
    set_mtime_ns(library.root / "edit.jpg", library.mtime("edit.jpg") + 5_000_000)
    edited = jpeg_bytes(width=48, height=32, exif_date="2022:01:01 00:00:00")
    (library.root / "edit.jpg").write_bytes(edited)
    harness.scan()  # replace: the old id is masked in the index
    assert search(harness, q="dog").get_json()["photos"] == []


def test_model_change_flips_at_start_with_cutover(make_harness, library):
    first = make_harness()
    content = jpeg_bytes(width=32, height=32, exif_date="2021:01:01 00:00:00")
    first.adapter.register_image(content, [1.0, 0.0])
    first.adapter.register_text("dog", [1.0, 0.0])
    library.add("a.jpg", content=content)
    first.scan()
    first.drain()
    assert first.db.get_meta("active_model_id") == "test-model"
    assert len(search(first, q="dog").get_json()["photos"]) == 1

    # the owner starts the new model on the AI box, edits config, restarts:
    # the new process loads the configured model's vectors (none yet) and
    # queues re-embeds for every photo lacking one
    second = make_harness(**{"embedding.model_id": "new-model"})
    assert second.index.size == 0  # the flip happened at the restart
    assert second.db.get_meta("active_model_id") == "test-model"  # until cutover
    pending = (
        second.db.reader()
        .execute("SELECT COUNT(*) FROM jobs WHERE kind='embed' AND state='pending'")
        .fetchone()[0]
    )
    assert pending == 1
    second.adapter.register_image(content, [1.0, 0.0])
    second.adapter.register_text("dog", [1.0, 0.0])
    second.drain()
    body = search(second, q="dog").get_json()
    assert body["state"] == "ok"  # correct over the re-embedded subset
    assert body["coverage"] == {"embedded": 1, "total": 1}

    # cutover: active_model_id is rewritten and the old vectors are deleted
    response = second.client.post("/api/admin/model/cutover")
    assert response.status_code == 200
    assert second.db.get_meta("active_model_id") == "new-model"
    models = {row["model_id"] for row in second.db.reader().execute("SELECT DISTINCT model_id FROM vectors")}
    assert models == {"new-model"}
    # a second cutover with no change in progress is a conflict
    assert second.client.post("/api/admin/model/cutover").status_code == 409


def test_aborted_model_change_prunes_pending_jobs(make_harness, library):
    first = make_harness()
    content = jpeg_bytes(width=32, height=32, exif_date="2021:01:01 00:00:00")
    first.adapter.register_image(content, [1.0, 0.0])
    library.add("a.jpg", content=content)
    first.scan()
    first.drain()

    # start a change to new-model, but do not drain: the re-embed job pends
    second = make_harness(**{"embedding.model_id": "new-model"})
    assert (
        second.db.reader()
        .execute("SELECT COUNT(*) FROM jobs WHERE kind='embed' AND state='pending'")
        .fetchone()[0]
        == 1
    )

    # the owner reverts the config and restarts: startup prunes the aborted
    # change's leftover jobs (the photos already have old-model vectors)
    third = make_harness()
    assert (
        third.db.reader()
        .execute("SELECT COUNT(*) FROM jobs WHERE kind='embed' AND state='pending'")
        .fetchone()[0]
        == 0
    )
    third.adapter.register_text("dog", [1.0, 0.0])
    assert search(third, q="dog").get_json()["state"] == "ok"  # restored intact


def test_restart_mid_change_requeues_only_the_remainder(make_harness, library):
    contents = []
    for i in range(4):
        content = jpeg_bytes(width=32, height=32, exif_date=f"2021:01:0{i + 1} 00:00:00")
        library.add(f"p{i}.jpg", content=content)
        contents.append(content)
    harness = make_harness()
    harness.scan()

    # a model change is in progress; two photos already carry new-model
    # vectors (their jobs completed), two do not
    array = np.asarray([1.0, 0.0], dtype=np.float32)
    with harness.db.writer() as conn:
        conn.execute(
            "INSERT INTO meta(key, value) VALUES('active_model_id', 'old-model') "
            "ON CONFLICT(key) DO UPDATE SET value='old-model'"
        )
        for i in range(2):
            row = conn.execute("SELECT photo_id FROM photos WHERE path=?", (f"p{i}.jpg",)).fetchone()
            conn.execute(
                "INSERT OR REPLACE INTO vectors(photo_id, model_id, dim, vector) "
                "VALUES(?, 'new-model', ?, ?)",
                (row["photo_id"], len(array), array.tobytes()),
            )
            conn.execute(
                "DELETE FROM jobs WHERE photo_id=? AND kind='embed'",
                (row["photo_id"],),
            )

    # restart with new-model configured: only the two lacking photos re-queue
    second = make_harness(**{"embedding.model_id": "new-model"})
    pending = {
        row["photo_id"]
        for row in second.db.reader().execute(
            "SELECT photo_id FROM jobs WHERE kind='embed' AND state='pending'"
        )
    }
    done = {
        row["photo_id"]
        for row in second.db.reader().execute("SELECT photo_id FROM vectors WHERE model_id='new-model'")
    }
    assert pending.isdisjoint(done)
    assert len(pending) == 2
