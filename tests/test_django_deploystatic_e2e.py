"""`pocket-rs django deploystatic` / `storage upload` を moto server 相手に確かめる。

collectstatic は PATH に置いた偽の `uv` が代わりに行う (`uv run python manage.py
collectstatic …` の引数・環境変数・cwd を記録し、override の出力先にファイルを書く)。
Django の project は要らない。upload は AWS CLI を呼ばず Rust が直接行う。
"""

from __future__ import annotations

import os
from pathlib import Path

import pytest
from conftest import MOTO_REGION

pytestmark = pytest.mark.usefixtures("moto_versioning_root")

PROJECT = "djstatic"
BUCKET = f"dev-{PROJECT}-pocket"

_TOML = f"""
[general]
region = "{MOTO_REGION}"
project_name = "{PROJECT}"
stages = ["dev"]

[general.django_fallback.storages]
default = {{ store = "filesystem" }}
staticfiles = {{ store = "filesystem", static = true }}
media = {{ store = "filesystem", location = "data/media" }}

[s3]

[container.main]
dockerfile_path = "Dockerfile"

[container.main.handlers.wsgi]
command = "pocket.django.lambda_handlers.wsgi_handler"

[container.main.django]
project_dir = "app"

[container.main.django.storages]
default = {{ store = "filesystem" }}
staticfiles = {{ store = "s3", location = "static", static = true }}
media = {{ store = "s3", location = "media" }}
"""

# 偽の uv: 引数・環境変数・cwd を記録し、override の出力先に collectstatic の
# 結果を書く。
# --link のときは元のファイルへの symlink を置く (Django の collectstatic --link と同じ)
_FAKE_UV = """#!/bin/sh
log="$FAKE_LOG"
echo "args: $*" > "$log"
echo "cwd: $(pwd)" >> "$log"
echo "backend: $POCKET_STATICFILES_BACKEND_OVERRIDE" >> "$log"
if [ -n "$FAKE_FAIL" ]; then exit "$FAKE_FAIL"; fi
out="$POCKET_STATICFILES_LOCATION_OVERRIDE"
mkdir -p "$out/css"
case "$*" in
  *--link*)
    ln -sf "$FAKE_SRC/a.css" "$out/css/a.css"
    ln -sf "$FAKE_SRC/b.js" "$out/b.js"
    ;;
  *)
    cp "$FAKE_SRC/a.css" "$out/css/a.css"
    cp "$FAKE_SRC/b.js" "$out/b.js"
    ;;
esac
"""


@pytest.fixture
def project(tmp_path, moto_s3) -> Path:
    root = tmp_path / "prj"
    (root / "app").mkdir(parents=True)
    (root / "pocket.toml").write_text(_TOML)
    (root / "pyproject.toml").write_text('[project]\nname = "djstatic"\n')
    src = root / "src"
    src.mkdir()
    (src / "a.css").write_text("body {}\n")
    (src / "b.js").write_text("console.log(1)\n")
    fake_bin = tmp_path / "bin"
    fake_bin.mkdir()
    uv = fake_bin / "uv"
    uv.write_text(_FAKE_UV)
    uv.chmod(0o755)
    # moto server は session で共有なので、bucket は作り直さずに空にする
    try:
        moto_s3.create_bucket(
            Bucket=BUCKET, CreateBucketConfiguration={"LocationConstraint": MOTO_REGION}
        )
    except moto_s3.exceptions.BucketAlreadyOwnedByYou:
        for key in _keys(moto_s3):
            moto_s3.delete_object(Bucket=BUCKET, Key=key)
    return root


def _env(project: Path, **extra: str) -> dict[str, str]:
    fake_bin = project.parent / "bin"
    return {
        "PATH": f"{fake_bin}:{os.environ['PATH']}",
        "FAKE_LOG": str(project / "uv.log"),
        "FAKE_SRC": str(project / "src"),
        **extra,
    }


def _run(pocket_rs, project: Path, *args: str, **extra: str):
    return pocket_rs.run(project, "django", *args, env=_env(project, **extra))


def _ok(pocket_rs, project: Path, *args: str, **extra: str) -> str:
    result = _run(pocket_rs, project, *args, **extra)
    assert result.returncode == 0, result.stderr + result.stdout
    return result.stderr + result.stdout


def _keys(moto_s3, prefix: str = "") -> list[str]:
    objects = moto_s3.list_objects_v2(Bucket=BUCKET, Prefix=prefix).get("Contents", [])
    return sorted(o["Key"] for o in objects)


def _body(moto_s3, key: str) -> bytes:
    return moto_s3.get_object(Bucket=BUCKET, Key=key)["Body"].read()


def test_deploystatic_collects_and_syncs(pocket_rs, project, moto_s3):
    out = _ok(pocket_rs, project, "deploystatic", "--stage", "dev")
    build = project / "pocket_cache/static_build/dev"
    log = (project / "uv.log").read_text()
    assert "args: run python manage.py collectstatic --noinput\n" in log
    # cwd は django の project_dir (pocket.toml からの相対)
    assert f"cwd: {project / 'app'}\n" in log
    assert "backend: django.contrib.staticfiles.storage.StaticFilesStorage\n" in log, (
        log
    )
    assert f"collectstatic to {build}..." in out
    assert f"Bucket: {BUCKET}" in out
    assert "Location: static" in out
    assert _keys(moto_s3) == ["static/b.js", "static/css/a.css"]
    head = moto_s3.head_object(Bucket=BUCKET, Key="static/css/a.css")
    assert head["ContentType"].startswith("text/css")

    # 内容が同じなら上げ直さない
    out = _ok(pocket_rs, project, "deploystatic", "--stage", "dev")
    assert (
        "0 ファイルをアップロードしました (skip 2 / 旧版を保持 0、prefix: static)"
        in out
    )

    # ローカルに無い object は --delete のときだけ消す
    moto_s3.put_object(Bucket=BUCKET, Key="static/old.js", Body=b"old")
    (build / "b.js").write_text("console.log(2)\n")
    out = _ok(
        pocket_rs, project, "deploystatic", "--stage", "dev", "--skip-collectstatic"
    )
    assert "1 ファイルをアップロードしました (skip 1 / 旧版を保持 1" in out
    assert _body(moto_s3, "static/b.js") == b"console.log(2)\n"
    assert "static/old.js" in _keys(moto_s3)
    out = _ok(
        pocket_rs,
        project,
        "deploystatic",
        "--stage",
        "dev",
        "--skip-collectstatic",
        "--delete",
    )
    assert "削除: s3://%s/static/old.js" % BUCKET in out
    assert _keys(moto_s3) == ["static/b.js", "static/css/a.css"]


def test_deploystatic_delete_refuses_empty_output(pocket_rs, project, moto_s3):
    _ok(pocket_rs, project, "deploystatic", "--stage", "dev")
    build = project / "pocket_cache/static_build/dev"
    for path in sorted(build.rglob("*"), reverse=True):
        path.rmdir() if path.is_dir() else path.unlink()
    result = _run(
        pocket_rs,
        project,
        "deploystatic",
        "--stage",
        "dev",
        "--skip-collectstatic",
        "--delete",
    )
    assert result.returncode != 0
    assert "ファイルがありません。S3 の object を消さずに止めます" in result.stderr
    assert _keys(moto_s3) == ["static/b.js", "static/css/a.css"]


def test_deploystatic_link_uploads_symlink_targets(pocket_rs, project, moto_s3):
    build = project / "pocket_cache/static_build/dev"
    build.mkdir(parents=True)
    (build / "stale.txt").write_text("from a previous build")
    _ok(pocket_rs, project, "deploystatic", "--stage", "dev", "--link")
    assert "--link" in (project / "uv.log").read_text()
    # link のときは出力先を作り直す (前回の実体が残らない)
    assert not (build / "stale.txt").exists()
    assert (build / "b.js").is_symlink()
    # symlink は指す先の内容で上がる
    assert _body(moto_s3, "static/b.js") == b"console.log(1)\n"
    assert _keys(moto_s3) == ["static/b.js", "static/css/a.css"]


def test_deploystatic_stops_on_collectstatic_failure(pocket_rs, project, moto_s3):
    result = _run(pocket_rs, project, "deploystatic", "--stage", "dev", FAKE_FAIL="3")
    assert result.returncode != 0
    assert "returned non-zero exit status 3." in result.stderr
    assert _keys(moto_s3) == []


def test_deploystatic_refuses_existing_override(pocket_rs, project, moto_s3):
    result = _run(
        pocket_rs,
        project,
        "deploystatic",
        "--stage",
        "dev",
        POCKET_STATICFILES_BACKEND_OVERRIDE="x",
    )
    assert result.returncode != 0
    assert "POCKET_STATICFILES_BACKEND_OVERRIDE is already set" in result.stderr
    assert not (project / "uv.log").exists()


def test_storage_upload_syncs_filesystem_to_s3(pocket_rs, project, moto_s3):
    media = project / "data/media"
    (media / "sub/.git").mkdir(parents=True)
    (media / "a.txt").write_text("a")
    (media / ".hidden").write_text("h")
    (media / "sub/b.txt").write_text("b")
    (media / "sub/.git/x").write_text("x")

    out = _ok(
        pocket_rs, project, "storage", "upload", "media", "--stage", "dev", "--dryrun"
    )
    assert f"data/media -> s3://{BUCKET}/media" in out
    assert f"(dryrun) アップロード: s3://{BUCKET}/media/a.txt" in out
    assert _keys(moto_s3) == []

    _ok(pocket_rs, project, "storage", "upload", "media", "--stage", "dev")
    # `.` で始まる要素を含む path は上げない
    assert _keys(moto_s3) == ["media/a.txt", "media/sub/b.txt"]

    # --delete はローカルに無い key を消すが、除外した key は消さない
    moto_s3.put_object(Bucket=BUCKET, Key="media/.keep", Body=b"")
    moto_s3.put_object(Bucket=BUCKET, Key="media/stale.txt", Body=b"")
    out = _ok(
        pocket_rs,
        project,
        "storage",
        "upload",
        "media",
        "--stage",
        "dev",
        "--delete",
        "--dryrun",
    )
    assert f"(dryrun) 削除: s3://{BUCKET}/media/stale.txt" in out
    assert "media/stale.txt" in _keys(moto_s3)
    _ok(pocket_rs, project, "storage", "upload", "media", "--stage", "dev", "--delete")
    assert _keys(moto_s3) == ["media/.keep", "media/a.txt", "media/sub/b.txt"]


def test_storage_upload_checks_backends(pocket_rs, project, moto_s3):
    result = _run(pocket_rs, project, "storage", "upload", "default", "--stage", "dev")
    assert result.returncode != 0
    assert "Upload to only support S3Boto3Storage" in result.stderr


def test_deploystatic_refuses_object_hash(pocket_rs, tmp_path, moto_s3):
    root = tmp_path / "oh"
    root.mkdir()
    toml = Path(__file__).parent / "data/toml/cloudfront_object_hash_no_domain.toml"
    (root / "pocket.toml").write_text(toml.read_text())
    (root / "pyproject.toml").write_text('[project]\nname = "oh"\n')
    result = pocket_rs.run(root, "django", "deploystatic", "--stage", "dev")
    assert result.returncode != 0
    assert (
        "staticfiles is object_hash: deploystatic does not support it. "
        "Publish with `pocket-rs static publish --stage dev`." in result.stderr
    )
