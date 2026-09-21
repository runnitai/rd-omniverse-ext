"""What a file IS, and what it should be called on disk.

Two questions the plugin has to answer about every file it receives, and it
receives them from three places that used to answer differently: a run's
outputs, a library item, and an upload. Treating everything as an image was the
original answer, which wrote videos to `.png` and drew models as an empty frame.

The vocabulary is the server's. `RunnitNodeRunResultType` is IMG, VID, MODEL,
ASSET_3D, LAYERS and TEXT; it is MAPPED here rather than passed through, so the
rest of the plugin reasons about what it can draw rather than about the
catalogue's spelling.
"""

from __future__ import annotations

import urllib.parse
from pathlib import Path

#: What the panel can do something with.
KIND_IMAGE = "IMAGE"
KIND_VIDEO = "VIDEO"
KIND_ASSET_3D = "ASSET_3D"
KIND_OTHER = "OTHER"

_TYPE_KINDS = {
    "IMG": KIND_IMAGE,
    "VID": KIND_VIDEO,
    "ASSET_3D": KIND_ASSET_3D,
}

#: The suffix each format is written with. Only what the tools actually return:
#: an unknown mime falls back to the URL's own suffix rather than being given a
#: name invented here.
_MIME_EXTENSIONS = {
    "image/png": ".png",
    "image/jpeg": ".jpg",
    "image/webp": ".webp",
    "video/mp4": ".mp4",
    "video/webm": ".webm",
    "video/quicktime": ".mov",
    "model/gltf-binary": ".glb",
    "model/gltf+json": ".gltf",
    "model/vnd.usdz+zip": ".usdz",
    "model/obj": ".obj",
}

#: Model formats the 3D tools actually return. Not an exhaustive list of every
#: 3D mime in existence: one that is wrong here shows as OTHER, which is a
#: visible gap rather than a file written under the wrong extension.
_ASSET_3D_MIMES = frozenset({
    "model/gltf-binary",
    "model/gltf+json",
    "model/vnd.usdz+zip",
    "model/obj",
    "application/octet-stream",
})


def kind_for(type_name: str | None, mime_type: str | None) -> str:
    """What this plugin would have to draw to show it.

    `type` first because the server means it. The mime is the fallback for
    something that predates the field or comes back without it.
    """
    mapped = _TYPE_KINDS.get((type_name or "").upper())
    if mapped:
        return mapped
    mime = (mime_type or "").split(";")[0].strip().lower()
    if mime.startswith("image/"):
        return KIND_IMAGE
    if mime.startswith("video/"):
        return KIND_VIDEO
    if mime in _ASSET_3D_MIMES:
        return KIND_ASSET_3D
    return KIND_OTHER


def extension_for(mime_type: str | None, url: str | None) -> str:
    """The suffix this file should be written with, including the dot.

    From the mime rather than from the URL, because the URL is signed and its
    path is not a promise. A format with no entry falls back to the URL's own
    suffix and then to `.bin`: a wrong name is worse than an unhelpful one, and
    `.bin` at least does not claim to be a picture.
    """
    mime = (mime_type or "").split(";")[0].strip().lower()
    known = _MIME_EXTENSIONS.get(mime)
    if known:
        return known
    suffix = Path(urllib.parse.urlparse(url or "").path).suffix.lower()
    # Guard against a query-string artefact or a path with no name at all.
    if suffix and len(suffix) <= 6 and suffix[1:].isalnum():
        return suffix
    return ".bin"
