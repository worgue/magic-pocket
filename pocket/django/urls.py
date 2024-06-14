import mimetypes
import sys

from django.conf import settings
from django.http import HttpResponse
from django.urls import path

from ..utils import get_stage

if sys.version_info >= (3, 11):
    import tomllib
else:
    import tomli as tomllib

pocket_http_root = settings.PROJECT_DIR / "pocket_http_root"


def find_http_toml_filepaths():
    return pocket_http_root.glob("**/__serve__.toml")


def as_path(toml_file, stage):
    dirpath = toml_file.parent
    filetype = mimetypes.guess_type(dirpath)[0] or "text/plain"

    config = tomllib.loads(toml_file.read_text())
    if stage in config:
        filename = config[stage]["filename"]
    else:
        filename = stage + dirpath.suffix
    content_file = dirpath / filename
    if not content_file.exists():
        filetype = None

    def render(request):
        if filetype is None:
            raise FileNotFoundError(content_file)
        if filetype.startswith("text"):
            content = content_file.read_text()
        else:
            content = content_file.read_bytes()
        return HttpResponse(content, content_type=filetype)

    return path(str(dirpath.relative_to(pocket_http_root)), render)


def get_pocket_http_urls():
    serve_toml_fils = find_http_toml_filepaths()
    paths = [as_path(toml_file, get_stage()) for toml_file in serve_toml_fils]
    return paths
