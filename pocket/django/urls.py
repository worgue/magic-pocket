import mimetypes
import os

from django.conf import settings
from django.http import Http404, HttpResponse
from django.urls import path

pocket_http_root = settings.PROJECT_DIR / "pocket_http_root"


def find_http_toml_filepaths():
    return pocket_http_root.glob("**/+serve")


def as_path(toml_file, stage):
    dirpath = toml_file.parent

    filetype = mimetypes.guess_type(dirpath)[0]
    if filetype is None:
        filetype = "text/plain"
    if stage and (dirpath / stage + dirpath.suffix).exists():
        content_file = dirpath / (stage + dirpath.suffix)
    else:
        content_file = dirpath / ("default" + dirpath.suffix)
        if not content_file.exists():
            filetype = None
    if filetype is None:
        content = None
    elif filetype.startswith("text"):
        content = content_file.read_text()
    else:
        content = content_file.read_bytes()

    def render(request):
        if content is None:
            raise Http404
        return HttpResponse(content, content_type=filetype)

    return path(str(dirpath.relative_to(pocket_http_root)), render)


def get_pocket_http_urls():
    stage = os.environ.get("POCKET_STAGE")
    serve_toml_fils = find_http_toml_filepaths()
    paths = [as_path(url, stage) for url in serve_toml_fils]
    return paths
