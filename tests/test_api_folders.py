"""Tree derivation, recursive newest-date computation, empty-branch hiding,
subtree scoping (separator-safe, case-sensitive, literal `_`), memo
invalidation on scan commit, and subtree-scoped cursor windows."""

import time


def tree(harness):
    response = harness.client.get("/api/folders")
    assert response.status_code == 200
    return response.get_json()


def find(node, name):
    for child in node["children"]:
        if child["name"] == name:
            return child
        found = find(child, name)
        if found:
            return found
    return None


def seed(harness, library):
    library.add("vacation/2021/rome.jpg", exif_date="2021:07:01 10:00:00")
    library.add("vacation/2021/naples.jpg", exif_date="2021:07:05 10:00:00")
    library.add("vacation/old.jpg", exif_date="2019:01:01 10:00:00")
    library.add("2021_Italy/venice.jpg", exif_date="2021:08:01 10:00:00")
    library.add("2021xItaly/trap.jpg", exif_date="2021:09:01 10:00:00")
    library.add("FOO/sibling.jpg", exif_date="2020:01:01 10:00:00")
    library.add("root-photo.jpg", exif_date="2018:01:01 10:00:00")
    harness.scan()


def test_root_node_and_reserved_id(harness, library):
    seed(harness, library)
    root = tree(harness)
    assert root["id"] == "root"
    assert root["name"] == "library"
    assert root["photo_count"] == 7  # recursive: everything
    assert root["newest_date_taken"] == "2021-09-01T10:00:00"


def test_recursive_newest_and_count(harness, library):
    seed(harness, library)
    root = tree(harness)
    vacation = find(root, "vacation")
    assert vacation["photo_count"] == 3  # 2 in 2021/ + 1 direct
    # the parent's newest is the subtree max, not its own direct photos
    assert vacation["newest_date_taken"] == "2021-07-05T10:00:00"
    inner = find(root, "2021")
    assert inner["photo_count"] == 2
    assert inner["newest_date_taken"] == "2021-07-05T10:00:00"


def test_children_sorted_by_recursive_newest(harness, library):
    seed(harness, library)
    root = tree(harness)
    order = [child["newest_date_taken"] for child in root["children"]]
    assert order == sorted(order, reverse=True)


def test_folders_without_photos_never_appear(harness, library):
    library.add("only/photos/here.jpg", exif_date="2021:01:01 00:00:00")
    # a directory holding only non-photo files must not surface
    library.add("empty-dir/readme.txt", content=b"no photos here")
    harness.scan()
    root = tree(harness)
    assert find(root, "empty-dir") is None
    assert find(root, "only") is not None


def test_subtree_scoping_separator_safe(harness, library):
    seed(harness, library)
    root = tree(harness)
    italy = find(root, "2021_Italy")
    body = harness.client.get(f"/api/folders/{italy['id']}/photos").get_json()
    assert [card["date_taken"] for card in body["photos"]] == ["2021-08-01T10:00:00"]
    # the sibling whose name differs only after the separator must not leak
    leak = harness.client.get(f"/api/folders/{find(root, '2021xItaly')['id']}/photos").get_json()
    assert len(leak["photos"]) == 1
    assert leak["photos"][0]["date_taken"] == "2021-09-01T10:00:00"


def test_subtree_scoping_case_sensitive(harness, library):
    seed(harness, library)
    root = tree(harness)
    foo = find(root, "FOO")
    body = harness.client.get(f"/api/folders/{foo['id']}/photos").get_json()
    assert len(body["photos"]) == 1  # and nothing from a differently-cased sibling


def test_root_folder_photos_is_everything(harness, library):
    seed(harness, library)
    body = harness.client.get("/api/folders/root/photos?limit=500").get_json()
    assert len(body["photos"]) == 7


def test_folder_windows_paginate_within_subtree(harness, library):
    for i in range(5):
        library.add(f"series/p{i}.jpg", exif_date=f"2021:01:0{i + 1} 00:00:00")
    library.add("outside/x.jpg", exif_date="2021:12:01 00:00:00")
    harness.scan()
    root = tree(harness)
    series = find(root, "series")
    seen = []
    cursor = None
    while True:
        url = f"/api/folders/{series['id']}/photos?order=asc&limit=2"
        if cursor:
            url += f"&cursor={cursor}"
        body = harness.client.get(url).get_json()
        seen.extend(card["photo_id"] for card in body["photos"])
        cursor = body["next_cursor"]
        if cursor is None:
            break
    assert len(seen) == 5  # the subtree only — the outside photo never appears
    assert len(set(seen)) == 5


def test_unknown_or_malformed_folder(harness, library):
    seed(harness, library)
    unknown = tree(harness)["children"][0]["id"] + "xx"
    assert harness.client.get(f"/api/folders/{unknown}/photos").status_code == 404
    assert harness.client.get("/api/folders/!!!bad/photos").status_code == 400


def test_memo_invalidated_on_scan_commit(harness, library):
    library.add("memo/before.jpg", exif_date="2021:01:01 00:00:00")
    harness.scan()
    before = tree(harness)
    assert before["photo_count"] == 1

    # a second request without a scan serves the memo: same generation
    again = tree(harness)
    assert again["photo_count"] == 1

    # the scanner commits a pass -> the memo must rebuild
    library.add("memo/after.jpg", exif_date="2022:01:01 00:00:00")
    harness.scan()
    after = tree(harness)
    assert after["photo_count"] == 2
    assert after["newest_date_taken"] == "2022-01-01T00:00:00"


def test_memo_dropped_photos_leave_tree(harness, library):
    library.add("drop/one.jpg", exif_date="2021:01:01 00:00:00")
    library.add("drop/two.jpg", exif_date="2021:02:01 00:00:00")
    harness.scan()
    assert find(tree(harness), "drop")["photo_count"] == 2
    library.delete("drop/one.jpg")
    library.delete("drop/two.jpg")
    harness.scan()
    assert find(tree(harness), "drop") is None  # the branch hides when emptied


def test_warm_tree_under_target(harness, library):
    # 300 photos across nested folders; the memoized warm read must be fast
    for d in range(6):
        for i in range(50):
            library.add(f"perf/folder{d}/p{i}.jpg", exif_date=f"20{10 + d}:01:01 00:00:00")
    harness.scan()
    cold_start = time.perf_counter()
    first = tree(harness)
    cold = time.perf_counter() - cold_start
    warm_start = time.perf_counter()
    second = tree(harness)
    warm = time.perf_counter() - warm_start
    assert first == second
    assert first["photo_count"] == 300
    assert warm < 0.2  # target: < 200 ms warm (memoized)
    assert warm <= cold  # and the memo actually helps
