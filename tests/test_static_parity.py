"""Rust 版 `pocket-rs static` と Python 版 `pocket static` の parity。

同じ project dir のコピーと、同じ moto server 上の別 bucket に対して両方の CLI を
コマンドとして実行し、map 本文・S3 の key 集合と checksum・release 表・current の
release・終了コードが一致することを固定する。Python 側が正典
(`tests/test_static_publish.py` の各ケースをコマンド経由で両者に適用する)。

Rust バイナリはこのファイルの session fixture が `cargo build` する。cargo が無い
環境では skip するが、CI (`CI` 環境変数あり) では失敗にする
(parity 未実行で緑にしない)。
"""

from __future__ import annotations

import hashlib
import json
import os
import re
import shutil
import socket
import subprocess
import sys
from pathlib import Path

import boto3
import pytest
from moto.server import ThreadedMotoServer

from pocket.static.mapfile import StaticMap
from pocket.static.release import decode_release

REPO_ROOT = Path(__file__).resolve().parent.parent
REGION = "ap-northeast-1"
STAGE = "dev"
PY_BUCKET = "parity-py"
RS_BUCKET = "parity-rs"
META = ".pocket/static/web/static/"


def _sha(data: bytes) -> str:
    return hashlib.sha256(data).hexdigest()


A_KEY = "static/img/a.%s.jpg" % _sha(b"A")[:16]
B_KEY = "static/b.%s.txt" % _sha(b"B")[:16]
CHUNK_KEY = "static/app/_app/chunk-abc.js"


@pytest.fixture(scope="session")
def pocket_rs_bin() -> Path:
    if shutil.which("cargo") is None:
        if os.environ.get("CI"):
            pytest.fail("CI に cargo がありません (parity テストを実行できません)")
        pytest.skip("cargo が無いため Rust 版 CLI の parity テストを skip します")
    subprocess.run(  # noqa: S603 shell=False + 制御された引数
        ["cargo", "build", "-p", "magic-pocket-cli-rs"],  # noqa: S607 cargo は PATH 前提
        cwd=REPO_ROOT,
        check=True,
        env={**os.environ, "TARGET_GC": "never"},
    )
    target_dir = Path(os.environ.get("CARGO_TARGET_DIR", REPO_ROOT / "target"))
    binary = target_dir / "debug" / "pocket-rs"
    assert binary.is_file(), binary
    return binary


@pytest.fixture(scope="session")
def moto_endpoint():
    with socket.socket() as sock:
        sock.bind(("127.0.0.1", 0))
        port = sock.getsockname()[1]
    server = ThreadedMotoServer(ip_address="127.0.0.1", port=port, verbose=False)
    server.start()
    try:
        yield "http://127.0.0.1:%d" % port
    finally:
        server.stop()


@pytest.fixture
def s3(moto_endpoint):
    client = boto3.client(
        "s3",
        region_name=REGION,
        endpoint_url=moto_endpoint,
        aws_access_key_id="testing",
        aws_secret_access_key="testing",  # noqa: S106 moto 用のダミー
    )
    for bucket in (PY_BUCKET, RS_BUCKET):
        client.create_bucket(
            Bucket=bucket, CreateBucketConfiguration={"LocationConstraint": REGION}
        )
    yield client
    for bucket in (PY_BUCKET, RS_BUCKET):
        objects = client.list_objects_v2(Bucket=bucket).get("Contents", [])
        if objects:
            client.delete_objects(
                Bucket=bucket,
                Delete={"Objects": [{"Key": o["Key"]} for o in objects], "Quiet": True},
            )
        client.delete_bucket(Bucket=bucket)


def _git(cwd: Path, *args: str) -> str:
    result = subprocess.run(  # noqa: S603 shell=False + 制御された引数
        ["git", *args],  # noqa: S607 git は PATH 前提の標準ツール
        cwd=cwd,
        capture_output=True,
        text=True,
        check=True,
    )
    return result.stdout


def _toml(bucket: str, sources: str) -> str:
    return (
        '[general]\nregion = "%s"\nproject_name = "parity"\nstages = ["dev"]\n\n'
        '[s3]\nbucket_name_format = "%s"\n\n'
        '[cloudfront.web]\ndomain = "cdn.example.com"\n\n'
        "[[cloudfront.web.routes]]\n"
        'is_default = true\nis_spa = true\norigin_path = "/spa"\n\n'
        "[[cloudfront.web.routes]]\n"
        'path_pattern = "/static/*"\nref = "static"\nversioning = "object_hash"\n'
        "sources = [\n%s\n]\n" % (REGION, bucket, sources)
    )


DEFAULT_SOURCES = (
    '    { name = "data", dir = "data", exclude = ["*.tmp"] },\n'
    '    { name = "code", dir = "build", hashed = false, prefix = "app/" },'
)


def _make_project(root: Path, bucket: str, sources: str = DEFAULT_SOURCES) -> Path:
    root.mkdir()
    (root / "data" / "img").mkdir(parents=True)
    (root / "build" / "_app").mkdir(parents=True)
    (root / "data" / "img" / "a.jpg").write_bytes(b"A")
    (root / "data" / "b.txt").write_bytes(b"B")
    (root / "data" / "skip.tmp").write_bytes(b"T")
    (root / "build" / "_app" / "chunk-abc.js").write_bytes(b"V")
    (root / "pocket.toml").write_text(_toml(bucket, sources), encoding="utf-8")
    _git(root, "init", "-q")
    _git(root, "config", "user.email", "test@example.com")
    _git(root, "config", "user.name", "test")
    return root


class Pair:
    """同じ内容の project dir 2 つ (py / rs) と、それぞれの CLI。"""

    def __init__(
        self, tmp_path: Path, endpoint: str, binary: Path, s3, sources=DEFAULT_SOURCES
    ):
        self.py = _make_project(tmp_path / "py", PY_BUCKET, sources)
        self.rs = _make_project(tmp_path / "rs", RS_BUCKET, sources)
        self.binary = binary
        self.s3 = s3
        self.env = {
            **os.environ,
            "AWS_ENDPOINT_URL": endpoint,
            "AWS_ACCESS_KEY_ID": "testing",
            "AWS_SECRET_ACCESS_KEY": "testing",
            "AWS_DEFAULT_REGION": REGION,
            "COLUMNS": "500",  # rich が 80 桁で折り返さないように
        }
        self.env.pop("POCKET_DEPLOY_STAGE", None)

    def run_py(self, *args: str, stdin: str = "", env: dict | None = None):
        return subprocess.run(  # noqa: S603 shell=False + 制御された引数
            [
                sys.executable,
                "-c",
                "from pocket_cli.cli.main_cli import main; main()",
                "static",
                *args,
            ],
            cwd=self.py,
            env={**self.env, **(env or {})},
            capture_output=True,
            text=True,
            input=stdin,
        )

    def run_rs(self, *args: str, stdin: str = "", env: dict | None = None):
        return subprocess.run(  # noqa: S603 shell=False + 制御された引数
            [str(self.binary), "static", *args],
            cwd=self.rs,
            env={**self.env, **(env or {})},
            capture_output=True,
            text=True,
            input=stdin,
        )

    def run_both(self, *args: str, stdin: str = "", env: dict | None = None):
        return self.run_py(*args, stdin=stdin, env=env), self.run_rs(
            *args, stdin=stdin, env=env
        )

    def ok_both(self, *args: str, env: dict | None = None):
        py, rs = self.run_both(*args, env=env)
        assert py.returncode == 0, py.stderr + py.stdout
        assert rs.returncode == 0, rs.stderr + rs.stdout
        return py, rs

    def fail_both(
        self, *args: str, match: str, stdin: str = "", env: dict | None = None
    ):
        py, rs = self.run_both(*args, stdin=stdin, env=env)
        assert py.returncode != 0, py.stdout
        assert rs.returncode != 0, rs.stdout
        assert match in py.stderr, py.stderr
        assert match in rs.stderr, rs.stderr
        return py, rs

    def commit_map(self):
        for root in (self.py, self.rs):
            _git(root, "add", "pocket_static")
            _git(root, "commit", "-q", "-m", "map")

    def write_both(self, rel: str, data: bytes):
        for root in (self.py, self.rs):
            (root / rel).write_bytes(data)

    def unlink_both(self, rel: str):
        for root in (self.py, self.rs):
            (root / rel).unlink()

    def maps(self) -> tuple[str, str]:
        return (
            (self.py / "pocket_static/web-static.map").read_text(encoding="utf-8"),
            (self.rs / "pocket_static/web-static.map").read_text(encoding="utf-8"),
        )

    # --- S3 の観測 ---

    def keys(self, bucket: str) -> set[str]:
        return {
            o["Key"] for o in self.s3.list_objects_v2(Bucket=bucket).get("Contents", [])
        }

    def snapshot(self, bucket: str) -> dict:
        """key → (ChecksumSHA256, CacheControl, 本文) と release 表・current の中身。"""
        snap: dict = {}
        for key in sorted(self.keys(bucket)):
            if key.startswith(META):
                continue
            head = self.s3.head_object(Bucket=bucket, Key=key, ChecksumMode="ENABLED")
            body = self.s3.get_object(Bucket=bucket, Key=key)["Body"].read()
            snap[key] = (head.get("ChecksumSHA256"), head.get("CacheControl"), body)
        current = self.current(bucket)
        snap["__current_release__"] = current["release"] if current else None
        snap["__map_sha256__"] = current["map_sha256"] if current else None
        releases = {}
        for key in sorted(self.keys(bucket)):
            if key.startswith(META + "releases/"):
                data = self.s3.get_object(Bucket=bucket, Key=key)["Body"].read()
                releases[key] = decode_release(data)
        snap["__releases__"] = releases
        return snap

    def current(self, bucket: str) -> dict | None:
        try:
            res = self.s3.get_object(Bucket=bucket, Key=META + "current")
        except self.s3.exceptions.NoSuchKey:
            return None
        return json.loads(res["Body"].read())

    def assert_same_s3(self):
        assert self.snapshot(PY_BUCKET) == self.snapshot(RS_BUCKET)


@pytest.fixture
def pair(tmp_path, moto_endpoint, pocket_rs_bin, s3) -> Pair:
    return Pair(tmp_path, moto_endpoint, pocket_rs_bin, s3)


_SUMMARY = re.compile(
    r"release (?P<release>[0-9a-f]{16}) を公開しました \(upload (?P<upload>\d+), "
    r"copy (?P<copy>\d+), skip (?P<skip>\d+), 照合 (?P<verified>\d+)\)"
)


def _summary(stderr: str) -> dict[str, str]:
    """publish の要約行 (診断メッセージは両者とも stderr に出す)。"""
    m = _SUMMARY.search(stderr)
    assert m, stderr
    return m.groupdict()


def test_hash_generate_writes_identical_map(pair: Pair):
    pair.ok_both("hash", "generate", "--stage", STAGE)
    py_map, rs_map = pair.maps()
    assert py_map == rs_map
    static_map = StaticMap.loads(rs_map)
    assert static_map.route == "web/static"
    assert set(static_map.entries) == {"img/a.jpg", "b.txt", "app/_app/chunk-abc.js"}


def test_first_publish_produces_same_s3_state(pair: Pair):
    py, rs = pair.ok_both("publish", "--stage", STAGE, "--hash-generate")
    assert _summary(py.stderr) == _summary(rs.stderr)
    assert _summary(rs.stderr)["upload"] == "3"
    assert pair.maps()[0] == pair.maps()[1]
    pair.assert_same_s3()
    assert {A_KEY, B_KEY, CHUNK_KEY} <= pair.keys(RS_BUCKET)
    assert pair.snapshot(RS_BUCKET)[A_KEY][1] == "public, max-age=31536000, immutable"
    current = pair.current(RS_BUCKET)
    assert current is not None
    assert current["release"] == StaticMap.loads(pair.maps()[1]).release_id


def test_republish_without_change_uploads_nothing(pair: Pair):
    pair.ok_both("publish", "--stage", STAGE, "--hash-generate")
    py, rs = pair.ok_both("publish", "--stage", STAGE, "--allow-dirty")
    assert _summary(py.stderr) == _summary(rs.stderr)
    # H は skip、P (chunk-abc.js) は内容を照合する
    assert _summary(rs.stderr)["upload"] == "0"
    assert _summary(rs.stderr)["skip"] == "2"
    assert _summary(rs.stderr)["verified"] == "1"
    pair.assert_same_s3()


def test_replace_one_file_uploads_only_that_key(pair: Pair):
    pair.ok_both("publish", "--stage", STAGE, "--hash-generate")
    pair.write_both("data/img/a.jpg", b"A2")
    py, rs = pair.ok_both("publish", "--stage", STAGE, "--hash-generate")
    assert _summary(py.stderr) == _summary(rs.stderr)
    assert _summary(rs.stderr)["upload"] == "1"
    pair.assert_same_s3()
    new_key = "static/img/a.%s.jpg" % _sha(b"A2")[:16]
    assert {A_KEY, new_key} <= pair.keys(RS_BUCKET)
    assert pair.snapshot(RS_BUCKET)[A_KEY][2] == b"A"


def test_verify_mode_rejects_local_change(pair: Pair):
    pair.ok_both("publish", "--stage", STAGE, "--hash-generate")
    pair.commit_map()
    before = pair.current(RS_BUCKET)
    pair.write_both("data/img/a.jpg", b"A2")
    pair.fail_both("publish", "--stage", STAGE, match="map と違う: img/a.jpg")
    assert pair.current(RS_BUCKET) == before
    pair.assert_same_s3()


def test_verify_mode_rejects_untracked_or_dirty_map(pair: Pair):
    pair.ok_both("publish", "--stage", STAGE, "--hash-generate")
    pair.fail_both("publish", "--stage", STAGE, match="untracked")
    pair.commit_map()
    pair.ok_both("publish", "--stage", STAGE)  # commit 済みなら通る
    for root in (pair.py, pair.rs):
        map_file = root / "pocket_static/web-static.map"
        map_file.write_text(
            map_file.read_text() + "extra.txt\t%s\tdata\tH\n" % ("c" * 64)
        )
    pair.fail_both("publish", "--stage", STAGE, match="commit 済みの内容と違います")


def test_verify_mode_outside_git_warns_and_continues(pair: Pair):
    for root in (pair.py, pair.rs):
        shutil.rmtree(root / ".git")
    pair.ok_both("publish", "--stage", STAGE, "--hash-generate")
    pair.ok_both("publish", "--stage", STAGE)
    pair.assert_same_s3()


def test_missing_local_file_is_ok_when_key_exists(pair: Pair):
    pair.ok_both("publish", "--stage", STAGE, "--hash-generate")
    pair.unlink_both("data/img/a.jpg")
    py, rs = pair.ok_both("publish", "--stage", STAGE, "--allow-dirty")
    assert _summary(py.stderr) == _summary(rs.stderr)
    assert _summary(rs.stderr)["upload"] == "0"


def test_missing_local_and_s3_is_error(pair: Pair):
    pair.ok_both("publish", "--stage", STAGE, "--hash-generate")
    pair.unlink_both("data/img/a.jpg")
    for bucket in (PY_BUCKET, RS_BUCKET):
        pair.s3.delete_object(Bucket=bucket, Key=A_KEY)
    pair.fail_both(
        "publish",
        "--stage",
        STAGE,
        "--allow-dirty",
        match="ローカルにも S3 にも無い: img/a.jpg",
    )


def test_plain_entry_content_change_is_rejected(pair: Pair):
    pair.ok_both("publish", "--stage", STAGE, "--hash-generate")
    pair.write_both("build/_app/chunk-abc.js", b"V2")
    pair.fail_both(
        "publish", "--stage", STAGE, "--hash-generate", match="名前も変えてください"
    )


def test_resume_after_partial_upload(pair: Pair):
    """前回が途中で止まった状態 (一部の key だけある、current 無し) から続きを上げる"""
    for bucket in (PY_BUCKET, RS_BUCKET):
        pair.s3.put_object(Bucket=bucket, Key=B_KEY, Body=b"B")
        pair.s3.put_object(Bucket=bucket, Key=CHUNK_KEY, Body=b"V")
    py, rs = pair.ok_both("publish", "--stage", STAGE, "--hash-generate")
    assert _summary(py.stderr) == _summary(rs.stderr)
    # 上げ終わった b.txt は skip、P の chunk は照合、残りの img/a.jpg だけ upload
    assert _summary(rs.stderr)["upload"] == "1"
    assert _summary(rs.stderr)["skip"] == "1"
    assert _summary(rs.stderr)["verified"] == "1"
    pair.assert_same_s3()


def test_existing_key_with_same_content_is_verified_and_different_is_error(pair: Pair):
    """同じ key に先に同じ内容があれば照合して通り、違う内容ならエラー (P は常に照合)"""
    for bucket in (PY_BUCKET, RS_BUCKET):
        pair.s3.put_object(Bucket=bucket, Key=CHUNK_KEY, Body=b"V")
    py, rs = pair.ok_both("publish", "--stage", STAGE, "--hash-generate")
    assert _summary(py.stderr) == _summary(rs.stderr)
    assert _summary(rs.stderr)["verified"] == "1"
    pair.write_both("build/_app/chunk-abc.js", b"V3")
    for bucket in (PY_BUCKET, RS_BUCKET):
        pair.s3.put_object(Bucket=bucket, Key=CHUNK_KEY, Body=b"other")
    pair.fail_both("publish", "--stage", STAGE, "--hash-generate", match="内容")


def test_verify_existing_flag_checks_hashed_keys_too(pair: Pair):
    for bucket in (PY_BUCKET, RS_BUCKET):
        pair.s3.put_object(Bucket=bucket, Key=A_KEY, Body=b"other")
    pair.ok_both(
        "publish", "--stage", STAGE, "--hash-generate"
    )  # 既定は H を skip する
    pair.fail_both(
        "publish",
        "--stage",
        STAGE,
        "--hash-generate",
        "--verify-existing",
        match="内容",
    )


def test_rel_key_collision_is_rejected(tmp_path, moto_endpoint, pocket_rs_bin, s3):
    sources = (
        '    { name = "h", dir = "h" },\n    { name = "p", dir = "p", hashed = false },'
    )
    pair = Pair(tmp_path, moto_endpoint, pocket_rs_bin, s3, sources)
    for root in (pair.py, pair.rs):
        (root / "h").mkdir()
        (root / "p").mkdir()
        (root / "h" / "a.js").write_bytes(b"A")
        (root / "p" / ("a.%s.js" % _sha(b"A")[:16])).write_bytes(b"other")
    pair.fail_both(
        "publish", "--stage", STAGE, "--hash-generate", match="両方から導かれます"
    )


def test_logical_path_collision_between_sources_is_rejected(
    tmp_path, moto_endpoint, pocket_rs_bin, s3
):
    sources = '    { name = "x", dir = "x" },\n    { name = "y", dir = "y" },'
    pair = Pair(tmp_path, moto_endpoint, pocket_rs_bin, s3, sources)
    for root in (pair.py, pair.rs):
        (root / "x").mkdir()
        (root / "y").mkdir()
        (root / "x" / "a.txt").write_bytes(b"A")
        (root / "y" / "a.txt").write_bytes(b"B")
    pair.fail_both(
        "publish", "--stage", STAGE, "--hash-generate", match="両方にあります"
    )


def test_include_exclude_patterns_enumerate_identically(
    tmp_path, moto_endpoint, pocket_rs_bin, s3
):
    """pathspec (Python) と ignore crate (Rust) の gitignore 風 pattern の意味が揃う"""
    sources = (
        '    { name = "d", dir = "d",'
        ' exclude = ["*.tmp", "!keep.tmp", "/root.txt", "deep/", "build"] },\n'
        '    { name = "i", dir = "i", prefix = "i/",'
        ' include = ["sub/**", "*.md"], exclude = ["*.tmp", "sub/x/"] },'
    )
    pair = Pair(tmp_path, moto_endpoint, pocket_rs_bin, s3, sources)
    for root in (pair.py, pair.rs):
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
            (root / rel).parent.mkdir(parents=True, exist_ok=True)
            (root / rel).write_bytes(rel.encode())
    pair.ok_both("hash", "generate", "--stage", STAGE)
    py_map, rs_map = pair.maps()
    assert py_map == rs_map
    paths = sorted(StaticMap.loads(rs_map).entries)
    assert paths == [
        "a.txt",
        "i/other/b.md",
        "i/sub/a.txt",
        "i/top.md",
        "sub/keep.tmp",
        "sub/root.txt",
        "写真 1.jpg",
    ]


def test_adopt_copies_matching_flat_key(pair: Pair):
    for bucket in (PY_BUCKET, RS_BUCKET):
        pair.s3.put_object(Bucket=bucket, Key="static/img/a.jpg", Body=b"A")
        pair.s3.put_object(Bucket=bucket, Key="static/b.txt", Body=b"stale")
    py, rs = pair.ok_both("adopt", "--stage", STAGE, "--hash-generate")
    assert _summary(py.stderr) == _summary(rs.stderr)
    assert _summary(rs.stderr)["copy"] == "1"
    assert _summary(rs.stderr)["upload"] == "2"
    pair.assert_same_s3()
    snap = pair.snapshot(RS_BUCKET)
    assert snap[A_KEY][1] == "public, max-age=31536000, immutable"
    assert snap[B_KEY][2] == b"B"
    assert "static/img/a.jpg" in pair.keys(RS_BUCKET)  # 平置きは消さない


def test_purge_deletes_only_release_referenced_keys(pair: Pair):
    pair.ok_both("publish", "--stage", STAGE, "--hash-generate")
    pair.write_both("data/img/a.jpg", b"A2")
    pair.ok_both("publish", "--stage", STAGE, "--hash-generate")
    for bucket in (PY_BUCKET, RS_BUCKET):
        pair.s3.put_object(Bucket=bucket, Key="static/unrelated.txt", Body=b"keep")
    py, rs = pair.ok_both("purge", "--stage", STAGE, "--dry-run")
    assert "削除対象 4 件" in py.stderr
    assert "削除対象 4 件" in rs.stderr
    assert pair.keys(PY_BUCKET) == pair.keys(RS_BUCKET)
    # 確認プロンプトに答えなければ消さない
    pair.fail_both("purge", "--stage", STAGE, match="Abort")
    assert A_KEY in pair.keys(RS_BUCKET)
    py, rs = pair.ok_both("purge", "--stage", STAGE, "-y")
    assert "4 件を削除しました" in py.stderr
    assert "4 件を削除しました" in rs.stderr
    assert pair.keys(PY_BUCKET) == pair.keys(RS_BUCKET)
    keys = pair.keys(RS_BUCKET)
    assert "static/unrelated.txt" in keys
    assert META + "current" in keys
    assert A_KEY not in keys
    # purge → publish で同じ key に戻る
    py, rs = pair.ok_both("publish", "--stage", STAGE, "--allow-dirty")
    assert _summary(rs.stderr)["upload"] == "3"
    pair.assert_same_s3()


def _status(stdout: str, root: Path) -> dict:
    report = json.loads(stdout[: stdout.index("\n}") + 2])
    report.pop("published_at", None)
    report["map"] = report["map"].replace(str(root), "<root>")
    report["bucket"] = (
        report["bucket"].replace(PY_BUCKET, "<bucket>").replace(RS_BUCKET, "<bucket>")
    )
    return report


def test_status_report(pair: Pair):
    py, rs = pair.ok_both("status", "--stage", STAGE)
    assert _status(py.stdout, pair.py) == _status(rs.stdout, pair.rs)
    assert _status(rs.stdout, pair.rs)["in_sync"] is False
    assert "まだ publish されていません" in py.stderr
    assert "まだ publish されていません" in rs.stderr
    pair.ok_both("publish", "--stage", STAGE, "--hash-generate")
    py, rs = pair.ok_both("status", "--stage", STAGE)
    assert _status(py.stdout, pair.py) == _status(rs.stdout, pair.rs)
    assert _status(rs.stdout, pair.rs)["in_sync"] is True
    assert _status(rs.stdout, pair.rs)["map_entries"] == 3
    pair.write_both("data/img/a.jpg", b"A2")
    pair.ok_both("hash", "generate", "--stage", STAGE)
    py, rs = pair.ok_both("status", "--stage", STAGE)
    assert _status(py.stdout, pair.py) == _status(rs.stdout, pair.rs)
    assert _status(rs.stdout, pair.rs)["in_sync"] is False


def test_map_from_other_route_is_rejected(pair: Pair):
    pair.ok_both("publish", "--stage", STAGE, "--hash-generate")
    for root in (pair.py, pair.rs):
        map_file = root / "pocket_static/web-static.map"
        map_file.write_text(
            map_file.read_text().replace("# route: web/static", "# route: web/other")
        )
    pair.fail_both(
        "publish", "--stage", STAGE, "--allow-dirty", match="route web/other の map"
    )


def test_target_selection_and_stage_env(pair: Pair):
    env = {"POCKET_DEPLOY_STAGE": STAGE}
    pair.ok_both("status", "--name", "web", "--route", "static", env=env)
    pair.ok_both("status", "--route", "/static/*", env=env)
    pair.fail_both(
        "status", "--name", "nope", match="cloudfront 'nope' is not configured", env=env
    )
    pair.fail_both(
        "status", "--route", "nope", match="route がありません (route nope)", env=env
    )
