"""`pocket static publish` のエンジン (moto)。

完了条件: 変更なしの再 publish は upload 0 件 / 1 ファイル差し替えでその key だけ
upload され旧 key は残る / 同じ key に別の内容は載らない / 失敗したら current を
更新しない / 途中で止まっても再実行で続きから進む。
"""

from __future__ import annotations

import base64
import hashlib
import json
import subprocess
from pathlib import Path

import boto3
import pytest
from botocore.exceptions import ClientError, EndpointConnectionError
from moto import mock_aws
from pocket_cli import static_publish
from pocket_cli.static_publish import StaticPublisher, StaticPublishError, status_report

from pocket.context import RouteContext
from pocket.settings import StaticSource
from pocket.static.mapfile import StaticMap
from pocket.static.release import current_key, decode_current, decode_release

REGION = "ap-northeast-1"
BUCKET = "dev-testprj-pocket"


def _sha(data: bytes) -> str:
    return hashlib.sha256(data).hexdigest()


def _git(cwd: Path, *args: str) -> str:
    result = subprocess.run(  # noqa: S603 shell=False + 制御された引数
        ["git", *args],  # noqa: S607 git は PATH 前提の標準ツール
        cwd=cwd,
        capture_output=True,
        text=True,
        check=True,
    )
    return result.stdout


@pytest.fixture
def project(tmp_path: Path) -> Path:
    (tmp_path / "data" / "img").mkdir(parents=True)
    (tmp_path / "build" / "_app").mkdir(parents=True)
    (tmp_path / "data" / "img" / "a.jpg").write_bytes(b"A")
    (tmp_path / "data" / "b.txt").write_bytes(b"B")
    (tmp_path / "data" / "skip.tmp").write_bytes(b"T")
    (tmp_path / "build" / "_app" / "chunk-abc.js").write_bytes(b"V")
    _git(tmp_path, "init", "-q")
    _git(tmp_path, "config", "user.email", "test@example.com")
    _git(tmp_path, "config", "user.name", "test")
    return tmp_path


def _route(**overrides) -> RouteContext:
    data: dict = {
        "path_pattern": "/static/*",
        "ref": "static",
        "versioning": "object_hash",
        "cloudfront_name": "web",
        "sources": [
            StaticSource(name="data", dir="data", exclude=["*.tmp"]),
            StaticSource(name="code", dir="build", hashed=False, prefix="app/"),
        ],
    }
    data.update(overrides)
    return RouteContext(**data)


@pytest.fixture
def s3():
    with mock_aws():
        client = boto3.client("s3", region_name=REGION)
        client.create_bucket(
            Bucket=BUCKET, CreateBucketConfiguration={"LocationConstraint": REGION}
        )
        yield client


@pytest.fixture
def publisher(project, s3) -> StaticPublisher:
    return StaticPublisher(
        _route(), bucket=BUCKET, region=REGION, base_dir=project, jobs=2, s3_client=s3
    )


def _keys(s3) -> set[str]:
    return {o["Key"] for o in s3.list_objects_v2(Bucket=BUCKET).get("Contents", [])}


def _current(s3, publisher) -> dict:
    res = s3.get_object(Bucket=BUCKET, Key=current_key(publisher.meta_prefix))
    return decode_current(res["Body"].read())


A_KEY = "static/img/a.%s.jpg" % _sha(b"A")[:16]
B_KEY = "static/b.%s.txt" % _sha(b"B")[:16]
CHUNK_KEY = "static/app/_app/chunk-abc.js"


def test_first_publish_uploads_all_and_writes_map(publisher, s3, project):
    result = publisher.publish(hash_generate=True)
    assert (result.uploaded, result.skipped, result.copied) == (3, 0, 0)
    assert {A_KEY, B_KEY, CHUNK_KEY} <= _keys(s3)
    assert "static/skip.tmp" not in str(_keys(s3))

    static_map = StaticMap.loads((project / "pocket_static/web-static.map").read_text())
    assert static_map.route == "web/static"
    assert set(static_map.entries) == {"img/a.jpg", "b.txt", "app/_app/chunk-abc.js"}
    assert static_map.entries["app/_app/chunk-abc.js"].hashed is False

    current = _current(s3, publisher)
    assert current["release"] == static_map.release_id == result.release_id
    rel = s3.get_object(
        Bucket=BUCKET,
        Key="%sreleases/%s.json.gz" % (publisher.meta_prefix, current["release"]),
    )
    document = decode_release(rel["Body"].read())
    assert document["entries"]["img/a.jpg"] == "img/a.%s.jpg" % _sha(b"A")[:16]
    assert document["entries"]["app/_app/chunk-abc.js"] == "app/_app/chunk-abc.js"

    head = s3.head_object(Bucket=BUCKET, Key=A_KEY)
    assert head["CacheControl"] == "public, max-age=31536000, immutable"
    assert head["ContentType"] == "image/jpeg"


def test_republish_without_change_uploads_nothing(publisher, s3):
    publisher.publish(hash_generate=True)
    result = publisher.publish(allow_dirty=True)
    assert result.uploaded == 0
    # H は skip、P (chunk-abc.js) は内容を照合する
    assert result.skipped == 2
    assert result.verified == 1


def test_replace_one_file_uploads_only_that_key(publisher, s3, project):
    first = publisher.publish(hash_generate=True)
    (project / "data" / "img" / "a.jpg").write_bytes(b"A2")
    second = publisher.publish(hash_generate=True)
    assert second.uploaded == 1
    assert second.release_id != first.release_id
    new_key = "static/img/a.%s.jpg" % _sha(b"A2")[:16]
    assert {A_KEY, new_key} <= _keys(s3)
    # 差し替え前の key は前の内容を返し続ける
    assert s3.get_object(Bucket=BUCKET, Key=A_KEY)["Body"].read() == b"A"
    assert _current(s3, publisher)["release"] == second.release_id


def test_verify_mode_rejects_local_change(publisher, s3, project):
    publisher.publish(hash_generate=True)
    _git(project, "add", "pocket_static")
    _git(project, "commit", "-q", "-m", "map")
    (project / "data" / "img" / "a.jpg").write_bytes(b"A2")
    with pytest.raises(StaticPublishError, match="map と違う: img/a.jpg"):
        publisher.publish()
    # current は変わっていない
    assert _current(s3, publisher)["release"] == publisher.read_map().release_id


def test_verify_mode_rejects_untracked_or_dirty_map(publisher, s3, project):
    publisher.publish(hash_generate=True)
    with pytest.raises(StaticPublishError, match="untracked"):
        publisher.publish()
    _git(project, "add", "pocket_static")
    _git(project, "commit", "-q", "-m", "map")
    publisher.publish()  # commit 済みなら通る
    map_file = project / "pocket_static/web-static.map"
    map_file.write_text(map_file.read_text() + "extra.txt\t%s\tdata\tH\n" % ("c" * 64))
    with pytest.raises(StaticPublishError, match="commit 済みの内容と違います"):
        publisher.publish()


def test_verify_mode_outside_git_warns_and_continues(tmp_path, s3):
    (tmp_path / "data").mkdir()
    (tmp_path / "data" / "a.txt").write_bytes(b"A")
    route = _route(sources=[StaticSource(name="data", dir="data")])
    publisher = StaticPublisher(
        route, bucket=BUCKET, region=REGION, base_dir=tmp_path, s3_client=s3
    )
    publisher.publish(hash_generate=True)
    publisher.publish()


def test_missing_local_file_is_ok_when_key_exists(publisher, s3, project):
    """2 回目以降は、S3 に key があればローカルに無いファイルがあってもよい。"""
    publisher.publish(hash_generate=True)
    (project / "data" / "img" / "a.jpg").unlink()
    result = publisher.publish(allow_dirty=True)
    assert result.uploaded == 0


def test_missing_local_and_s3_is_error(publisher, s3, project):
    publisher.publish(hash_generate=True)
    (project / "data" / "img" / "a.jpg").unlink()
    s3.delete_object(Bucket=BUCKET, Key=A_KEY)
    with pytest.raises(StaticPublishError, match="ローカルにも S3 にも無い: img/a.jpg"):
        publisher.publish(allow_dirty=True)


def test_plain_entry_content_change_is_rejected(publisher, s3, project):
    """hashed = false は同じ名前で内容を変えられない (URL が変わらないため)。"""
    publisher.publish(hash_generate=True)
    (project / "build" / "_app" / "chunk-abc.js").write_bytes(b"V2")
    with pytest.raises(StaticPublishError, match="名前も変えてください"):
        publisher.publish(hash_generate=True)


def test_resume_after_partial_upload(project, s3):
    """途中で止まった publish は、上げ終わった key を飛ばして続きから進む。"""
    publisher = StaticPublisher(
        _route(), bucket=BUCKET, region=REGION, base_dir=project, jobs=1, s3_client=s3
    )

    class Boom(Exception):
        pass

    calls = {"n": 0}
    original = publisher._put

    def flaky(key, local):
        calls["n"] += 1
        if calls["n"] == 3:
            raise Boom()
        original(key, local)

    publisher._put = flaky  # type: ignore[method-assign]
    with pytest.raises(Boom):
        publisher.publish(hash_generate=True)
    # current は切り替わっていない (全部上がってから書く)
    assert current_key(publisher.meta_prefix) not in _keys(s3)
    publisher._put = original  # type: ignore[method-assign]
    result = publisher.publish(allow_dirty=True)
    # 上げ終わった b.txt は skip、P の chunk は照合、残りの img/a.jpg だけ upload
    assert (result.uploaded, result.skipped, result.verified) == (1, 1, 1)
    assert current_key(publisher.meta_prefix) in _keys(s3)


def test_precondition_failed_verifies_existing_content(publisher, s3, project):
    publisher.enumerate_sources()
    files = publisher.enumerate_sources()
    publisher.hash_files(files)
    a = [f for f in files if f.path == "img/a.jpg"][0]
    # 同じ内容が先に置かれていれば照合して通る
    s3.put_object(Bucket=BUCKET, Key=A_KEY, Body=b"A")
    assert publisher._upload_one(A_KEY, a) == "verified"
    # 違う内容ならエラー (同じ key に別の内容は載らない)
    s3.put_object(Bucket=BUCKET, Key=A_KEY, Body=b"other")
    with pytest.raises(StaticPublishError, match="内容"):
        publisher._upload_one(A_KEY, a)


def test_verify_existing_flag_checks_hashed_keys_too(publisher, s3):
    s3.put_object(Bucket=BUCKET, Key=A_KEY, Body=b"other")
    publisher.publish(hash_generate=True)  # 既定は H を skip する
    with pytest.raises(StaticPublishError, match="内容"):
        publisher.publish(hash_generate=True, verify_existing=True)


def test_rel_key_collision_is_rejected(tmp_path, s3):
    (tmp_path / "h").mkdir()
    (tmp_path / "p").mkdir()
    (tmp_path / "h" / "a.js").write_bytes(b"A")
    (tmp_path / "p" / ("a.%s.js" % _sha(b"A")[:16])).write_bytes(b"other")
    route = _route(
        sources=[
            StaticSource(name="h", dir="h"),
            StaticSource(name="p", dir="p", hashed=False),
        ]
    )
    publisher = StaticPublisher(
        route, bucket=BUCKET, region=REGION, base_dir=tmp_path, s3_client=s3
    )
    with pytest.raises(StaticPublishError, match="両方から導かれます"):
        publisher.publish(hash_generate=True)


def test_logical_path_collision_between_sources_is_rejected(tmp_path, s3):
    (tmp_path / "x").mkdir()
    (tmp_path / "y").mkdir()
    (tmp_path / "x" / "a.txt").write_bytes(b"A")
    (tmp_path / "y" / "a.txt").write_bytes(b"B")
    route = _route(
        sources=[StaticSource(name="x", dir="x"), StaticSource(name="y", dir="y")]
    )
    publisher = StaticPublisher(
        route, bucket=BUCKET, region=REGION, base_dir=tmp_path, s3_client=s3
    )
    with pytest.raises(StaticPublishError, match="両方にあります"):
        publisher.publish(hash_generate=True)


def test_concurrent_publish_conflict_on_current(publisher, s3):
    publisher.publish(hash_generate=True)
    original = publisher._transfer

    def transfer_and_race(*args, **kwargs):
        original(*args, **kwargs)
        s3.put_object(
            Bucket=BUCKET,
            Key=current_key(publisher.meta_prefix),
            Body=json.dumps({"version": 1, "release": "x" * 16}).encode(),
        )

    publisher._transfer = transfer_and_race  # type: ignore[method-assign]
    with pytest.raises(StaticPublishError, match="別の publish"):
        publisher.publish(allow_dirty=True)


def test_adopt_copies_matching_flat_key(publisher, s3):
    """平置き key (ETag = MD5 が一致) は server-side copy、違えば upload。"""
    s3.put_object(Bucket=BUCKET, Key="static/img/a.jpg", Body=b"A")
    s3.put_object(Bucket=BUCKET, Key="static/b.txt", Body=b"stale")
    result = publisher.publish(hash_generate=True, adopt=True)
    assert result.copied == 1
    assert result.uploaded == 2
    head = s3.head_object(Bucket=BUCKET, Key=A_KEY, ChecksumMode="ENABLED")
    assert head["CacheControl"] == "public, max-age=31536000, immutable"
    assert head["ContentType"] == "image/jpeg"
    assert s3.get_object(Bucket=BUCKET, Key=B_KEY)["Body"].read() == b"B"
    # 平置きは消さない (凍結して残す)
    assert "static/img/a.jpg" in _keys(s3)


def test_purge_deletes_only_release_referenced_keys(publisher, s3, project):
    publisher.publish(hash_generate=True)
    (project / "data" / "img" / "a.jpg").write_bytes(b"A2")
    publisher.publish(hash_generate=True)
    s3.put_object(Bucket=BUCKET, Key="static/unrelated.txt", Body=b"keep")
    dry = publisher.purge(dry_run=True)
    assert "static/unrelated.txt" not in dry
    assert A_KEY in dry  # 旧 release が参照する key も対象
    deleted = publisher.purge()
    assert set(deleted) == set(dry)
    keys = _keys(s3)
    assert "static/unrelated.txt" in keys
    assert current_key(publisher.meta_prefix) in keys
    assert not any(k in keys for k in deleted)
    # purge → publish で同じ key に戻る
    again = publisher.publish(allow_dirty=True)
    assert again.uploaded == 3
    assert "static/img/a.%s.jpg" % _sha(b"A2")[:16] in _keys(s3)


def test_purge_reports_keys_that_delete_objects_could_not_delete(
    publisher, s3, monkeypatch
):
    """DeleteObjects は一部失敗でも HTTP 200 なので Errors を見て失敗にする。"""
    publisher.publish(hash_generate=True)
    targets = publisher.purge(dry_run=True)
    real = publisher.s3.delete_objects

    def partial(**kwargs):
        objects = kwargs["Delete"]["Objects"]
        res = real(
            Bucket=kwargs["Bucket"], Delete={"Objects": objects[1:], "Quiet": True}
        )
        res["Errors"] = [
            {
                "Key": objects[0]["Key"],
                "Code": "AccessDenied",
                "Message": "Access Denied",
            }
        ]
        return res

    monkeypatch.setattr(publisher.s3, "delete_objects", partial)
    with pytest.raises(StaticPublishError) as info:
        publisher.purge()
    assert str(info.value) == (
        "1 件の削除に失敗しました (purge を再実行すると残りを対象にします):\n"
        "  %s: AccessDenied Access Denied" % targets[0]
    )
    assert targets[0] in _keys(s3)
    assert not any(k in _keys(s3) for k in targets[1:])
    # 再実行で残りが対象になる
    monkeypatch.undo()
    assert publisher.purge() == [targets[0]]


def test_status_report(publisher, s3, project):
    report = status_report(publisher)
    assert report["in_sync"] is False
    assert "current_release" not in report
    publisher.publish(hash_generate=True)
    report = status_report(publisher)
    assert report["in_sync"] is True
    assert report["map_entries"] == 3
    (project / "data" / "img" / "a.jpg").write_bytes(b"A2")
    publisher.hash_files(files := publisher.enumerate_sources())
    publisher.write_map(publisher.build_map(files))
    assert status_report(publisher)["in_sync"] is False


def test_map_from_other_route_is_rejected(publisher, s3, project):
    publisher.publish(hash_generate=True)
    text = (project / "pocket_static/web-static.map").read_text()
    (project / "pocket_static/web-static.map").write_text(
        text.replace("# route: web/static", "# route: web/other")
    )
    with pytest.raises(StaticPublishError, match="route web/other の map"):
        publisher.publish(allow_dirty=True)


def test_transient_put_failure_is_retried_per_key(publisher, s3, monkeypatch):
    """接続層の瞬断は、その key だけ待って再試行する (publish 全体は止めない)。"""
    monkeypatch.setattr(static_publish.time, "sleep", lambda _s: None)
    calls: list[str] = []
    original = s3.put_object

    def flaky(**kwargs):
        calls.append(kwargs["Key"])
        if kwargs["Key"] == A_KEY and calls.count(A_KEY) <= 2:
            raise EndpointConnectionError(endpoint_url="https://s3.example")
        return original(**kwargs)

    publisher.s3 = type("S3", (), {"__getattr__": lambda _, n: getattr(s3, n)})()
    publisher.s3.put_object = flaky  # type: ignore[attr-defined]
    result = publisher.publish(hash_generate=True)
    assert result.uploaded == 3
    assert calls.count(A_KEY) == 3
    assert A_KEY in _keys(s3)


def _client_error(code: str, status: int) -> ClientError:
    return ClientError(
        {
            "Error": {"Code": code, "Message": code},
            "ResponseMetadata": {
                "RequestId": "r",
                "HostId": "h",
                "HTTPStatusCode": status,
                "HTTPHeaders": {},
                "RetryAttempts": 0,
            },
        },
        "PutObject",
    )


def test_retry_transient_gives_up_and_skips_non_transient():
    slept: list[float] = []

    def always_down():
        raise EndpointConnectionError(endpoint_url="https://s3.example")

    with pytest.raises(EndpointConnectionError):
        static_publish._retry_transient(
            "PutObject k", always_down, attempts=3, sleep=slept.append
        )
    assert slept == [1.0, 2.0]

    def forbidden():
        raise _client_error("AccessDenied", 403)

    slept.clear()
    with pytest.raises(ClientError):
        static_publish._retry_transient("PutObject k", forbidden, sleep=slept.append)
    assert slept == []
    # 5xx と SlowDown は一過性。412 (PreconditionFailed) は違う
    assert static_publish._is_transient_error(_client_error("SlowDown", 503))
    assert static_publish._is_transient_error(_client_error("InternalError", 500))
    assert not static_publish._is_transient_error(
        _client_error("PreconditionFailed", 412)
    )


def test_put_sends_checksum_and_no_overwrite(publisher, s3):
    """PutObject は If-None-Match: * と内容の SHA-256 checksum を付ける。"""
    calls: list[dict] = []
    original = s3.put_object

    def spy(**kwargs):
        calls.append(kwargs)
        return original(**kwargs)

    publisher.s3 = type("S3", (), {"__getattr__": lambda _, n: getattr(s3, n)})()
    publisher.s3.put_object = spy  # type: ignore[attr-defined]
    publisher.publish(hash_generate=True)
    a = [c for c in calls if c["Key"] == A_KEY][0]
    assert a["IfNoneMatch"] == "*"
    assert (
        a["ChecksumSHA256"] == base64.b64encode(hashlib.sha256(b"A").digest()).decode()
    )
