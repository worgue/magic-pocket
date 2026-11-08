"""Rust 版 `pocket-rs static` の e2e。

project dir と moto server 上の bucket に対して `pocket-rs static` をコマンドとして
実行し、S3 の key 集合と checksum・map 本文・release 表・current・終了コード・
stderr の要旨を明示の値で確かめる。Python 版との parity テスト
(Python 版 `pocket static` の削除で廃止) の各ケースを引き継いでいる。

バイナリの build と moto server は tests/conftest.py の fixture を使う。
"""

from __future__ import annotations

import hashlib
import json
import re
import shutil
import subprocess
from pathlib import Path

import pytest

from pocket.static.mapfile import StaticMap
from pocket.static.release import decode_release

REGION = "ap-northeast-1"  # conftest の moto_s3 と同じ region
STAGE = "dev"
BUCKET = "e2e-static"
META = ".pocket/static/web/static/"
IMMUTABLE = "public, max-age=31536000, immutable"


def _sha(data: bytes) -> str:
    return hashlib.sha256(data).hexdigest()


A_KEY = "static/img/a.%s.jpg" % _sha(b"A")[:16]
B_KEY = "static/b.%s.txt" % _sha(b"B")[:16]
CHUNK_KEY = "static/app/_app/chunk-abc.js"


def _git(cwd: Path, *args: str) -> str:
    result = subprocess.run(  # noqa: S603 shell=False + 制御された引数
        ["git", *args],  # noqa: S607 git は PATH 前提の標準ツール
        cwd=cwd,
        capture_output=True,
        text=True,
        check=True,
    )
    return result.stdout


def _toml(sources: str) -> str:
    return (
        '[general]\nregion = "%s"\nproject_name = "e2e"\nstages = ["dev"]\n\n'
        '[s3]\nbucket_name_format = "%s"\n\n'
        '[cloudfront.web]\ndomain = "cdn.example.com"\n\n'
        "[[cloudfront.web.routes]]\n"
        'is_default = true\nis_spa = true\norigin_path = "/spa"\n\n'
        "[[cloudfront.web.routes]]\n"
        'path_pattern = "/static/*"\nref = "static"\nversioning = "object_hash"\n'
        "sources = [\n%s\n]\n" % (REGION, BUCKET, sources)
    )


DEFAULT_SOURCES = (
    '    { name = "data", dir = "data", exclude = ["*.tmp"] },\n'
    '    { name = "code", dir = "build", hashed = false, prefix = "app/" },'
)


class Project:
    """object_hash route を 1 つ持つ project dir と、その bucket。"""

    def __init__(self, root: Path, pocket_rs, s3, sources=DEFAULT_SOURCES):
        self.root = root
        self.pocket_rs = pocket_rs
        self.s3 = s3
        root.mkdir()
        (root / "pocket.toml").write_text(_toml(sources), encoding="utf-8")
        _git(root, "init", "-q")
        _git(root, "config", "user.email", "test@example.com")
        _git(root, "config", "user.name", "test")

    def write(self, rel: str, data: bytes):
        path = self.root / rel
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_bytes(data)

    def run(self, *args: str, stdin: str = "", env: dict | None = None):
        return self.pocket_rs.run(self.root, "static", *args, stdin=stdin, env=env)

    def ok(self, *args: str, env: dict | None = None):
        res = self.run(*args, env=env)
        assert res.returncode == 0, res.stderr + res.stdout
        return res

    def fail(self, *args: str, match: str, stdin: str = "", env: dict | None = None):
        res = self.run(*args, stdin=stdin, env=env)
        assert res.returncode != 0, res.stdout
        assert match in res.stderr, res.stderr
        return res

    def commit_map(self):
        _git(self.root, "add", "pocket_static")
        _git(self.root, "commit", "-q", "-m", "map")

    @property
    def map_file(self) -> Path:
        return self.root / "pocket_static/web-static.map"

    def static_map(self) -> StaticMap:
        return StaticMap.loads(self.map_file.read_text(encoding="utf-8"))

    # --- S3 の観測 ---

    def keys(self) -> set[str]:
        res = self.s3.list_objects_v2(Bucket=BUCKET)
        return {o["Key"] for o in res.get("Contents", [])}

    def content_keys(self) -> set[str]:
        return {k for k in self.keys() if not k.startswith(META)}

    def body(self, key: str) -> bytes:
        return self.s3.get_object(Bucket=BUCKET, Key=key)["Body"].read()

    def head(self, key: str) -> dict:
        return self.s3.head_object(Bucket=BUCKET, Key=key, ChecksumMode="ENABLED")

    def current(self) -> dict | None:
        try:
            res = self.s3.get_object(Bucket=BUCKET, Key=META + "current")
        except self.s3.exceptions.NoSuchKey:
            return None
        return json.loads(res["Body"].read())

    def releases(self) -> dict[str, dict]:
        """release id → release 表 (decode 済み)。"""
        prefix, suffix = META + "releases/", ".json.gz"
        return {
            key[len(prefix) : -len(suffix)]: decode_release(self.body(key))
            for key in self.keys()
            if key.startswith(prefix)
        }

    def assert_object(self, key: str, body: bytes, cache_control: str | None):
        """key の本文・ETag・Cache-Control を確かめる。

        moto server は ChecksumSHA256 を保存しない (HEAD で返さない) ため、内容は
        本文と ETag (単一 part の MD5) で見る。pocket-rs の照合もこの場合は
        ETag に落ちる。
        """
        assert self.body(key) == body
        head = self.head(key)
        md5 = hashlib.md5(body, usedforsecurity=False).hexdigest()
        assert head["ETag"] == '"%s"' % md5
        assert head.get("CacheControl") == cache_control


@pytest.fixture
def s3_bucket(moto_s3):
    moto_s3.create_bucket(
        Bucket=BUCKET, CreateBucketConfiguration={"LocationConstraint": REGION}
    )
    yield moto_s3
    objects = moto_s3.list_objects_v2(Bucket=BUCKET).get("Contents", [])
    if objects:
        moto_s3.delete_objects(
            Bucket=BUCKET,
            Delete={"Objects": [{"Key": o["Key"]} for o in objects], "Quiet": True},
        )
    moto_s3.delete_bucket(Bucket=BUCKET)


@pytest.fixture
def make_project(tmp_path, pocket_rs, s3_bucket):
    def _make(sources: str = DEFAULT_SOURCES, name: str = "prj") -> Project:
        return Project(tmp_path / name, pocket_rs, s3_bucket, sources)

    return _make


@pytest.fixture
def prj(make_project) -> Project:
    project = make_project()
    project.write("data/img/a.jpg", b"A")
    project.write("data/b.txt", b"B")
    project.write("data/skip.tmp", b"T")
    project.write("build/_app/chunk-abc.js", b"V")
    return project


_SUMMARY = re.compile(
    r"release (?P<release>[0-9a-f]{16}) を公開しました \(upload (?P<upload>\d+), "
    r"copy (?P<copy>\d+), skip (?P<skip>\d+), 照合 (?P<verified>\d+)\)"
)


def _counts(stderr: str) -> dict[str, int]:
    """publish の要約行の件数 (upload / copy / skip / verified)。"""
    m = _SUMMARY.search(stderr)
    assert m, stderr
    return {k: int(v) for k, v in m.groupdict().items() if k != "release"}


def _release_in_summary(stderr: str) -> str:
    m = _SUMMARY.search(stderr)
    assert m, stderr
    return m.group("release")


def test_hash_generate_writes_map(prj: Project):
    prj.ok("hash", "generate", "--stage", STAGE)
    static_map = prj.static_map()
    assert static_map.route == "web/static"
    assert set(static_map.entries) == {"img/a.jpg", "b.txt", "app/_app/chunk-abc.js"}
    a = static_map.entries["img/a.jpg"]
    assert a.sha256 == _sha(b"A")
    assert a.source == "data"
    assert a.hashed is True
    chunk = static_map.entries["app/_app/chunk-abc.js"]
    assert chunk.sha256 == _sha(b"V")
    assert chunk.source == "code"
    assert chunk.hashed is False
    assert prj.keys() == set()  # hash generate は S3 に触らない


def test_first_publish(prj: Project):
    res = prj.ok("publish", "--stage", STAGE, "--hash-generate")
    assert _counts(res.stderr) == {"upload": 3, "copy": 0, "skip": 0, "verified": 0}
    release_id = prj.static_map().release_id
    assert _release_in_summary(res.stderr) == release_id
    assert prj.content_keys() == {A_KEY, B_KEY, CHUNK_KEY}
    prj.assert_object(A_KEY, b"A", IMMUTABLE)
    prj.assert_object(B_KEY, b"B", IMMUTABLE)
    prj.assert_object(CHUNK_KEY, b"V", IMMUTABLE)
    # current は map 本文の sha256 と release を指す
    current = prj.current()
    assert current is not None
    assert current["release"] == release_id
    assert current["map_sha256"] == _sha(prj.map_file.read_bytes())
    assert set(current) == {"version", "release", "map_sha256", "published_at"}
    # release 表は論理 path → key prefix 配下の key (runtime の resolver が読む)
    assert prj.releases() == {
        release_id: {
            "version": 1,
            "release": release_id,
            "route": "web/static",
            "entries": {
                "app/_app/chunk-abc.js": CHUNK_KEY.removeprefix("static/"),
                "b.txt": B_KEY.removeprefix("static/"),
                "img/a.jpg": A_KEY.removeprefix("static/"),
            },
        }
    }


def test_republish_without_change_uploads_nothing(prj: Project):
    prj.ok("publish", "--stage", STAGE, "--hash-generate")
    keys_before = prj.keys()
    res = prj.ok("publish", "--stage", STAGE, "--allow-dirty")
    # hashed は skip、plain (chunk-abc.js) は内容を照合する
    assert _counts(res.stderr) == {"upload": 0, "copy": 0, "skip": 2, "verified": 1}
    assert prj.keys() == keys_before


def test_replace_one_file_uploads_only_that_key(prj: Project):
    prj.ok("publish", "--stage", STAGE, "--hash-generate")
    first_release = prj.static_map().release_id
    prj.write("data/img/a.jpg", b"A2")
    res = prj.ok("publish", "--stage", STAGE, "--hash-generate")
    assert _counts(res.stderr) == {"upload": 1, "copy": 0, "skip": 1, "verified": 1}
    new_key = "static/img/a.%s.jpg" % _sha(b"A2")[:16]
    assert prj.content_keys() == {A_KEY, new_key, B_KEY, CHUNK_KEY}
    prj.assert_object(A_KEY, b"A", IMMUTABLE)  # 旧版は残す
    prj.assert_object(new_key, b"A2", IMMUTABLE)
    second_release = prj.static_map().release_id
    assert second_release != first_release
    assert prj.current()["release"] == second_release  # type: ignore[index]
    assert set(prj.releases()) == {first_release, second_release}


def test_verify_mode_rejects_local_change(prj: Project):
    prj.ok("publish", "--stage", STAGE, "--hash-generate")
    prj.commit_map()
    before = prj.current()
    keys_before = prj.keys()
    prj.write("data/img/a.jpg", b"A2")
    prj.fail("publish", "--stage", STAGE, match="map と違う: img/a.jpg")
    # 照合で止まったら current も S3 も動かない
    assert prj.current() == before
    assert prj.keys() == keys_before


def test_verify_mode_rejects_untracked_or_dirty_map(prj: Project):
    prj.ok("publish", "--stage", STAGE, "--hash-generate")
    prj.fail("publish", "--stage", STAGE, match="untracked")
    prj.commit_map()
    prj.ok("publish", "--stage", STAGE)  # commit 済みなら通る
    prj.map_file.write_text(
        prj.map_file.read_text() + "extra.txt\t%s\tdata\tH\n" % ("c" * 64)
    )
    prj.fail("publish", "--stage", STAGE, match="commit 済みの内容と違います")


def test_verify_mode_outside_git_continues(prj: Project):
    shutil.rmtree(prj.root / ".git")
    prj.ok("publish", "--stage", STAGE, "--hash-generate")
    res = prj.ok("publish", "--stage", STAGE)
    assert _counts(res.stderr) == {"upload": 0, "copy": 0, "skip": 2, "verified": 1}
    assert prj.content_keys() == {A_KEY, B_KEY, CHUNK_KEY}


def test_missing_local_file_is_ok_when_key_exists(prj: Project):
    prj.ok("publish", "--stage", STAGE, "--hash-generate")
    (prj.root / "data/img/a.jpg").unlink()
    res = prj.ok("publish", "--stage", STAGE, "--allow-dirty")
    assert _counts(res.stderr)["upload"] == 0
    prj.assert_object(A_KEY, b"A", IMMUTABLE)


def test_missing_local_and_s3_is_error(prj: Project):
    prj.ok("publish", "--stage", STAGE, "--hash-generate")
    before = prj.current()
    (prj.root / "data/img/a.jpg").unlink()
    prj.s3.delete_object(Bucket=BUCKET, Key=A_KEY)
    prj.fail(
        "publish",
        "--stage",
        STAGE,
        "--allow-dirty",
        match="ローカルにも S3 にも無い: img/a.jpg",
    )
    assert prj.current() == before


def test_plain_entry_content_change_is_rejected(prj: Project):
    prj.ok("publish", "--stage", STAGE, "--hash-generate")
    before = prj.current()
    prj.write("build/_app/chunk-abc.js", b"V2")
    prj.fail(
        "publish", "--stage", STAGE, "--hash-generate", match="名前も変えてください"
    )
    assert prj.current() == before
    prj.assert_object(CHUNK_KEY, b"V", IMMUTABLE)


def test_resume_after_partial_upload(prj: Project):
    """前回が途中で止まった状態 (一部の key だけある、current 無し) から続きを上げる"""
    prj.s3.put_object(Bucket=BUCKET, Key=B_KEY, Body=b"B")
    prj.s3.put_object(Bucket=BUCKET, Key=CHUNK_KEY, Body=b"V")
    res = prj.ok("publish", "--stage", STAGE, "--hash-generate")
    # 上げ終わった b.txt は skip、plain の chunk は照合、残りの img/a.jpg だけ upload
    assert _counts(res.stderr) == {"upload": 1, "copy": 0, "skip": 1, "verified": 1}
    assert prj.content_keys() == {A_KEY, B_KEY, CHUNK_KEY}
    prj.assert_object(A_KEY, b"A", IMMUTABLE)
    assert prj.current()["release"] == prj.static_map().release_id  # type: ignore[index]


def test_existing_plain_key_same_content_is_verified_different_is_error(
    prj: Project,
):
    """同じ key に同じ内容が先にあれば照合して通り、違う内容ならエラー

    plain entry は既定でも照合する。
    """
    prj.s3.put_object(Bucket=BUCKET, Key=CHUNK_KEY, Body=b"V")
    res = prj.ok("publish", "--stage", STAGE, "--hash-generate")
    assert _counts(res.stderr)["verified"] == 1
    before = prj.current()
    prj.write("build/_app/chunk-abc.js", b"V3")
    prj.s3.put_object(Bucket=BUCKET, Key=CHUNK_KEY, Body=b"other")
    prj.fail("publish", "--stage", STAGE, "--hash-generate", match="内容")
    assert prj.current() == before
    assert prj.body(CHUNK_KEY) == b"other"  # 既存 key は上書きしない


def test_verify_existing_flag_checks_hashed_keys_too(prj: Project):
    prj.s3.put_object(Bucket=BUCKET, Key=A_KEY, Body=b"other")
    res = prj.ok(
        "publish", "--stage", STAGE, "--hash-generate"
    )  # 既定は hashed を skip
    assert _counts(res.stderr) == {"upload": 2, "copy": 0, "skip": 1, "verified": 0}
    prj.fail(
        "publish",
        "--stage",
        STAGE,
        "--hash-generate",
        "--verify-existing",
        match="内容",
    )
    assert prj.body(A_KEY) == b"other"


def test_rel_key_collision_is_rejected(make_project):
    sources = (
        '    { name = "h", dir = "h" },\n    { name = "p", dir = "p", hashed = false },'
    )
    prj = make_project(sources)
    prj.write("h/a.js", b"A")
    prj.write("p/a.%s.js" % _sha(b"A")[:16], b"other")
    prj.fail("publish", "--stage", STAGE, "--hash-generate", match="両方から導かれます")
    assert prj.keys() == set()


def test_logical_path_collision_between_sources_is_rejected(make_project):
    sources = '    { name = "x", dir = "x" },\n    { name = "y", dir = "y" },'
    prj = make_project(sources)
    prj.write("x/a.txt", b"A")
    prj.write("y/a.txt", b"B")
    prj.fail("publish", "--stage", STAGE, "--hash-generate", match="両方にあります")
    assert prj.keys() == set()


def test_include_exclude_patterns(make_project):
    """include / exclude は gitignore 風 pattern として解釈する"""
    sources = (
        '    { name = "d", dir = "d",'
        ' exclude = ["*.tmp", "!keep.tmp", "/root.txt", "deep/", "build"] },\n'
        '    { name = "i", dir = "i", prefix = "i/",'
        ' include = ["sub/**", "*.md"], exclude = ["*.tmp", "sub/x/"] },'
    )
    prj = make_project(sources)
    for rel in [
        "d/a.tmp",
        "d/a.txt",
        "d/root.txt",
        "d/sub/root.txt",
        "d/sub/b.tmp",
        "d/sub/keep.tmp",
        "d/sub/deep/c.txt",
        "d/build/x.js",
        "d/nested/build/y.js",
        "d/写真 1.jpg",
        "i/top.md",
        "i/top.txt",
        "i/sub/a.txt",
        "i/sub/a.tmp",
        "i/sub/x/hidden.txt",
        "i/other/b.md",
        "i/other/c.txt",
    ]:
        prj.write(rel, rel.encode())
    prj.ok("hash", "generate", "--stage", STAGE)
    assert sorted(prj.static_map().entries) == [
        "a.txt",
        "i/other/b.md",
        "i/sub/a.txt",
        "i/top.md",
        "sub/keep.tmp",
        "sub/root.txt",
        "写真 1.jpg",
    ]


def test_adopt_copies_matching_flat_key(prj: Project):
    prj.s3.put_object(Bucket=BUCKET, Key="static/img/a.jpg", Body=b"A")
    prj.s3.put_object(Bucket=BUCKET, Key="static/b.txt", Body=b"stale")
    res = prj.ok("adopt", "--stage", STAGE, "--hash-generate")
    # 内容が一致する平置き key は copy、違う / 無いものは upload
    assert _counts(res.stderr) == {"upload": 2, "copy": 1, "skip": 0, "verified": 0}
    assert prj.content_keys() == {
        "static/img/a.jpg",  # 平置きは消さない
        "static/b.txt",
        A_KEY,
        B_KEY,
        CHUNK_KEY,
    }
    prj.assert_object(A_KEY, b"A", IMMUTABLE)
    prj.assert_object(B_KEY, b"B", IMMUTABLE)
    assert prj.body("static/b.txt") == b"stale"
    assert prj.current()["release"] == prj.static_map().release_id  # type: ignore[index]


def test_purge_deletes_only_release_referenced_keys(prj: Project):
    prj.ok("publish", "--stage", STAGE, "--hash-generate")
    prj.write("data/img/a.jpg", b"A2")
    prj.ok("publish", "--stage", STAGE, "--hash-generate")
    new_a_key = "static/img/a.%s.jpg" % _sha(b"A2")[:16]
    prj.s3.put_object(Bucket=BUCKET, Key="static/unrelated.txt", Body=b"keep")
    keys_before = prj.keys()
    # 2 release が参照する key 4 件 (a の新旧・b・chunk) が対象
    res = prj.ok("purge", "--stage", STAGE, "--dry-run")
    assert "削除対象 4 件" in res.stderr
    assert prj.keys() == keys_before
    # 確認プロンプトに答えなければ消さない
    prj.fail("purge", "--stage", STAGE, match="Abort")
    assert prj.keys() == keys_before
    res = prj.ok("purge", "--stage", STAGE, "-y")
    assert "4 件を削除しました" in res.stderr
    keys = prj.keys()
    assert prj.content_keys() == {"static/unrelated.txt"}
    assert META + "current" in keys
    assert not {A_KEY, new_a_key, B_KEY, CHUNK_KEY} & keys
    # purge → publish で同じ key に戻る
    res = prj.ok("publish", "--stage", STAGE, "--allow-dirty")
    assert _counts(res.stderr)["upload"] == 3
    assert prj.content_keys() == {"static/unrelated.txt", new_a_key, B_KEY, CHUNK_KEY}
    prj.assert_object(new_a_key, b"A2", IMMUTABLE)


def _status(res: subprocess.CompletedProcess[str], root: Path) -> dict:
    stdout = res.stdout
    report = json.loads(stdout[: stdout.index("\n}") + 2])
    report.pop("published_at", None)
    report["map"] = report["map"].replace(str(root), "<root>")
    return report


def test_status_report(prj: Project):
    base = {
        "route": "web/static",
        "bucket": BUCKET,
        "key_prefix": "static/",
        "map": "<root>/pocket_static/web-static.map",
    }
    res = prj.ok("status", "--stage", STAGE)
    assert "まだ publish されていません" in res.stderr
    assert _status(res, prj.root) == {**base, "in_sync": False}
    prj.ok("publish", "--stage", STAGE, "--hash-generate")
    release_id = prj.static_map().release_id
    res = prj.ok("status", "--stage", STAGE)
    assert "current は map と一致しています" in res.stderr
    assert _status(res, prj.root) == {
        **base,
        "map_release": release_id,
        "map_entries": 3,
        "current_release": release_id,
        "in_sync": True,
    }
    prj.write("data/img/a.jpg", b"A2")
    prj.ok("hash", "generate", "--stage", STAGE)
    new_release = prj.static_map().release_id
    res = prj.ok("status", "--stage", STAGE)
    assert _status(res, prj.root) == {
        **base,
        "map_release": new_release,
        "map_entries": 3,
        "current_release": release_id,
        "in_sync": False,
    }


def test_map_from_other_route_is_rejected(prj: Project):
    prj.ok("publish", "--stage", STAGE, "--hash-generate")
    prj.map_file.write_text(
        prj.map_file.read_text().replace("# route: web/static", "# route: web/other")
    )
    prj.fail(
        "publish", "--stage", STAGE, "--allow-dirty", match="route web/other の map"
    )


def test_target_selection_and_stage_env(prj: Project):
    env = {"POCKET_DEPLOY_STAGE": STAGE}
    prj.ok("status", "--name", "web", "--route", "static", env=env)
    prj.ok("status", "--route", "/static/*", env=env)
    prj.fail(
        "status", "--name", "nope", match="cloudfront 'nope' is not configured", env=env
    )
    prj.fail(
        "status", "--route", "nope", match="route がありません (route nope)", env=env
    )
