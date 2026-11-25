"""`pocket-rs django` の storage の解決が Python 版 `get_storages` と一致すること。

`pocket-rs django deploystatic` / `storage upload` は upload 先 (BACKEND・bucket_name・
location) を Rust で計算する。Python の `pocket.django.utils.get_storages(stage=…)` の
結果から同じ 3 つを取り出し、非表示の `pocket-rs django storages` の出力と比べる。
"""

from __future__ import annotations

import json
import subprocess
import sys
from pathlib import Path

import pytest
from test_context_cli_parity import _env, moto_aws  # noqa: F401 fixture の再利用

REPO_ROOT = Path(__file__).resolve().parent.parent

_PY_STORAGES = """
import json, sys
from pocket.django.utils import get_storages
stage = sys.argv[1] or None
out = {}
for key, value in get_storages(stage=stage).items():
    item = {"BACKEND": value["BACKEND"]}
    options = value.get("OPTIONS") or {}
    picked = {k: options[k] for k in ("bucket_name", "location") if k in options}
    if picked:
        item["OPTIONS"] = picked
    out[key] = item
print(json.dumps(out))
"""

# storage の options による上書き (Python 版は OPTIONS に merge する)
_OPTIONS_TOML = """
[general]
region = "ap-northeast-1"
project_name = "storages-prj"
stages = ["dev"]
s3_fallback_bucket_name = "fallback-bucket"

[general.django_fallback.storages]
default = { store = "filesystem", location = "data/media" }
staticfiles = { store = "filesystem", static = true, manifest = true }
management = { store = "s3", location = "management" }

[s3]

[container.main]
dockerfile_path = "Dockerfile"

[container.main.handlers.wsgi]
command = "pocket.django.lambda_handlers.wsgi_handler"

[container.main.django.storages]
default = { store = "s3", location = "media", options = { location = "override" } }
staticfiles = { store = "s3", location = "static", static = true }
management = { store = "s3", location = "management" }
uploads = { store = "filesystem", options = { location = "from-options" } }
"""

# distribution の route (versioning 無し・manifest) と、route を省いた storage
# (default route)
_DISTRIBUTION_TOML = """
[general]
region = "ap-northeast-1"
project_name = "storages-cf"
stages = ["dev"]

[s3]

[container.main]
dockerfile_path = "Dockerfile"

[container.main.handlers.wsgi]
command = "pocket.django.lambda_handlers.wsgi_handler"

[container.main.django.storages]
default = { store = "s3", distribution = "web" }
[container.main.django.storages.staticfiles]
store = "s3"
static = true
manifest = true
distribution = "web"
route = "static"

[cloudfront.web]
domain = "cdn.example.com"
routes = [
    { type = "s3", is_default = true, origin_path = "/root" },
    { path_pattern = "/static/*", ref = "static", origin_path = "/assets" },
]
"""

INLINE = {"options": _OPTIONS_TOML, "distribution": _DISTRIBUTION_TOML}

CASES = [
    ("tests/data/toml_vectors/context_full.toml", "dev"),
    ("tests/data/toml_vectors/context_full.toml", "live"),
    ("tests/data/toml/django_deploy_hash.toml", "dev"),
    ("tests/data/toml/cloudfront_object_hash.toml", "dev"),
    ("tests/data/toml/staticfiles_link.toml", "dev"),
    ("tests/data/toml/staticfiles_publish_command.toml", "dev"),
    ("tests/data/toml/staticfiles_publish_command.toml", ""),
    ("options", "dev"),
    ("options", ""),
    ("distribution", "dev"),
]


def _project(tmp_path: Path, toml: str) -> Path:
    root = tmp_path / "prj"
    root.mkdir()
    text = INLINE[toml] if toml in INLINE else (REPO_ROOT / toml).read_text()
    (root / "pocket.toml").write_text(text)
    (root / "pyproject.toml").write_text('[project]\nname = "storages-prj"\n')
    return root


def _clean_env(pocket_rs) -> dict[str, str]:
    env = _env(pocket_rs, {})
    for name in (
        "POCKET_STAGE",
        "POCKET_CONTAINER",
        "POCKET_STATICFILES_BACKEND_OVERRIDE",
        "POCKET_STATICFILES_LOCATION_OVERRIDE",
    ):
        env.pop(name, None)
    return env


@pytest.mark.parametrize(("toml", "stage"), CASES)
def test_storages_match_python(tmp_path, pocket_rs, moto_aws, toml, stage):  # noqa: F811
    root = _project(tmp_path, toml)
    env = _clean_env(pocket_rs)
    py = subprocess.run(  # noqa: S603 sys.executable + 固定スクリプト
        [sys.executable, "-c", _PY_STORAGES, stage],
        cwd=root,
        env=env,
        capture_output=True,
        text=True,
    )
    args = ["django", "storages"] + (["--stage", stage] if stage else [])
    rs = pocket_rs.run(root, *args, env=env)
    assert py.returncode == 0, py.stderr
    assert rs.returncode == 0, rs.stderr
    assert json.loads(rs.stdout) == json.loads(py.stdout.strip().splitlines()[-1])
