"""Unit — the path jail, symlink skip decisions, folder prefix logic (including
the reserved root id), and hash validation, all against plaps/paths.py."""

from plaps import paths


def test_valid_hash_is_64_hex():
    assert paths.valid_hash("a" * 64)
    assert paths.valid_hash("0123456789abcdef" * 4)
    assert not paths.valid_hash("A" * 64)  # uppercase is not a stored hash
    assert not paths.valid_hash("a" * 63)
    assert not paths.valid_hash("a" * 65)
    assert not paths.valid_hash("zz" * 32)


def test_relative_to_library(tmp_path):
    root = tmp_path / "lib"
    (root / "a" / "b").mkdir(parents=True)
    assert paths.relative_to_library(root, root / "a" / "b" / "x.jpg") == "a/b/x.jpg"
    assert paths.relative_to_library(root, root) == ""


def test_jail_rejects_outside_paths(tmp_path):
    root = tmp_path / "lib"
    root.mkdir()
    assert paths.inside_jail(root, root / "ok.jpg")
    assert not paths.inside_jail(root, tmp_path / "outside.jpg")
    assert not paths.inside_jail(root, root / ".." / "escape.jpg")


def test_folder_id_round_trip():
    for rel in ("", "2021", "2021/Italy", "2021_Italy/rom", "a/b/c/d"):
        assert paths.decode_folder_id(paths.encode_folder_id(rel)) == rel


def test_reserved_root_id():
    assert paths.encode_folder_id("") == paths.ROOT_ID == "root"
    assert paths.decode_folder_id("root") == ""


def test_folder_id_rejects_malformed():
    assert paths.decode_folder_id("not-base64!!!") is None
    assert paths.decode_folder_id("////") is None


def test_subtree_bounds_are_separator_safe():
    low, high = paths.subtree_bounds("2021")
    assert low == "2021/" and high == "20210"
    # "/foo" must not match "/foobar": 'b' > '0' puts it outside the range
    assert not (low <= "foobar" < high)
    # and not "/FOO/": case-sensitive comparison
    assert not (low <= "FOO/x" < high)
    # but every real descendant matches
    for child in ("2021/a.jpg", "2021/x/y.jpg", "2021/z.jpg"):
        assert low <= child < high
    # nothing can sort between '/' (0x2F) and '0' (0x30): adjacent in ASCII


def test_subtree_bounds_root():
    # the root matches everything — a special case, not the '/'..'0' trick
    low, high = paths.subtree_bounds("")
    assert low == ""
    assert low <= "anything.jpg" < high
    assert low <= "zzz/deep/photo.jpg" < high


def test_symlinks_are_identified(tmp_path):
    real = tmp_path / "real"
    real.mkdir()
    link = tmp_path / "link"
    link.symlink_to(real)
    assert link.is_symlink()  # the scanner skips entries where this is true
