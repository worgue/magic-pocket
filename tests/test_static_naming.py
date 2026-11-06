"""object_hash の key 名と URL encode の規則 (Rust と共通の test vector)。"""

from __future__ import annotations

import hashlib
import json
from pathlib import Path

import pytest

from pocket.static.naming import (
    HASH_LEN,
    encode_url_path,
    hashed_name,
    is_hashed_name,
    sha256_file,
    validate_logical_path,
)

VECTORS = json.loads(
    (Path(__file__).parent / "data" / "static_url_vectors.json").read_text()
)


@pytest.mark.parametrize("case", VECTORS["cases"], ids=lambda c: c["path"])
def test_vectors_match_python(case):
    assert VECTORS["hash_len"] == HASH_LEN
    assert hashed_name(case["path"], case["sha256"]) == case["hashed_name"]
    assert encode_url_path(case["hashed_name"]) == case["encoded_hashed_name"]
    assert encode_url_path(case["path"]) == case["encoded_path"]


def test_hashed_name_rules():
    h = "0123456789abcdef" + "0" * 48
    assert hashed_name("a.jpg", h) == "a.0123456789abcdef.jpg"
    assert hashed_name("dir/sub/a.tar.gz", h) == "dir/sub/a.tar.0123456789abcdef.gz"
    assert hashed_name("noext", h) == "noext.0123456789abcdef"
    assert hashed_name(".htaccess", h) == ".htaccess.0123456789abcdef"
    assert hashed_name("d/.env.local", h) == "d/.env.0123456789abcdef.local"


def test_hashed_name_rejects_short_hash():
    with pytest.raises(ValueError):
        hashed_name("a.jpg", "abc")


def test_is_hashed_name():
    assert is_hashed_name("img/a.0123456789abcdef.jpg")
    assert is_hashed_name("noext.0123456789abcdef")
    assert not is_hashed_name("img/a.jpg")
    assert not is_hashed_name("img/a.0123.jpg")
    # Vite の 8 文字 hash は対象外
    assert not is_hashed_name("_app/immutable/chunk-BxYz1234.js")


@pytest.mark.parametrize(
    "path",
    ["", "/abs", "trail/", "a//b", "./a", "a/../b", "tab\tx", "nl\nx", "back\\slash"],
)
def test_validate_logical_path_rejects(path):
    with pytest.raises(ValueError):
        validate_logical_path(path)


def test_validate_logical_path_accepts_unicode_and_symbols():
    validate_logical_path("img/写真 1 #%+?.jpg")


def test_sha256_file(tmp_path):
    file = tmp_path / "x.bin"
    file.write_bytes(b"hello" * 1000)
    assert (
        sha256_file(file, chunk_size=7) == hashlib.sha256(b"hello" * 1000).hexdigest()
    )
