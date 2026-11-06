"""map ファイル (path → 内容 hash) の読み書きと release id。"""

from __future__ import annotations

import pytest

from pocket.static.mapfile import MAP_FORMAT_LINE, MapEntry, StaticMap, StaticMapError

H1 = "a" * 64
H2 = "b" * 64


def _map() -> StaticMap:
    m = StaticMap("web/static")
    m.add(MapEntry("img/b.jpg", H1, "data", True))
    m.add(MapEntry("app/chunk.js", H2, "code", False))
    m.add(MapEntry("img/a.jpg", H2, "data", True))
    return m


def test_dumps_is_sorted_by_path_and_roundtrips():
    text = _map().dumps()
    lines = text.splitlines()
    assert lines[0] == MAP_FORMAT_LINE
    assert lines[1] == "# route: web/static"
    assert [ln.split("\t")[0] for ln in lines[2:]] == [
        "app/chunk.js",
        "img/a.jpg",
        "img/b.jpg",
    ]
    loaded = StaticMap.loads(text)
    assert loaded.route == "web/static"
    assert loaded.entries == _map().entries
    assert loaded.dumps() == text


def test_release_id_depends_on_content_not_insertion_order():
    a = _map()
    b = StaticMap("web/static")
    for e in reversed(a.sorted_entries()):
        b.add(e)
    assert a.release_id == b.release_id
    assert len(a.release_id) == 16
    c = _map()
    c.add(MapEntry("new.txt", H1, "data", True))
    assert c.release_id != a.release_id


def test_rel_key_for_hashed_and_plain():
    assert (
        MapEntry("img/a.jpg", H1, "data", True).rel_key == "img/a.aaaaaaaaaaaaaaaa.jpg"
    )
    assert MapEntry("app/chunk.js", H1, "code", False).rel_key == "app/chunk.js"


@pytest.mark.parametrize(
    "text, message",
    [
        ("not a map\n", "形式"),
        (MAP_FORMAT_LINE + "\n# route: r\nbad line\n", "3 行目"),
        (MAP_FORMAT_LINE + "\n# route: r\na\t" + H1 + "\tdata\tX\n", "行目"),
        (MAP_FORMAT_LINE + "\n# route: r\na\tzz\tdata\tH\n", "sha256"),
        (
            MAP_FORMAT_LINE
            + "\n# route: r\na\t"
            + H1
            + "\tdata\tH\na\t"
            + H2
            + "\tdata\tH\n",
            "重複",
        ),
        (MAP_FORMAT_LINE + "\n# route: r\n/abs\t" + H1 + "\tdata\tH\n", "path"),
        (MAP_FORMAT_LINE + "\na\t" + H1 + "\tdata\tH\n", "route"),
    ],
)
def test_loads_rejects_malformed(text, message):
    with pytest.raises(StaticMapError, match=message):
        StaticMap.loads(text)


def test_add_rejects_duplicate_path():
    m = _map()
    with pytest.raises(StaticMapError, match="重複"):
        m.add(MapEntry("img/a.jpg", H1, "data", True))
