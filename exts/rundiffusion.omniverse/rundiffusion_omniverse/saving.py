"""The two ways to keep a render, and why there are exactly two.

A generated result is temporary in a way that is easy to miss. A plugin run and
a library run are recorded separately on the server and the two never meet, so
a render made here does NOT appear in the library no matter how long you wait.
The storage bucket holds objects about 14 days and a signed URL is capped at 7.

So keeping a render means one of:

**Save to this computer.** A real file dialog, so the person chooses the name and
the folder like they would anywhere else. `omni.kit.window.filepicker` ships in
the stock SDK and in the packaged Composer host (checked in both before
depending on it), so this costs no vendoring and no bet.

**Send to RunDiffusion.** POST /uploads, which makes a durable copy on the
account and, as a bonus, one that can be fed back into another generation as an
UPLOAD_REF.
"""

from __future__ import annotations

import logging
from pathlib import Path

logger = logging.getLogger(__name__)

#: Extensions the save dialog offers. PNG first because most results are still
#: pictures, then the video and model formats the tools return: a result is
#: saved under the format it already is, so a dialog that only offered .png
#: would rename an .mp4 into a lie.
SAVE_FILTERS = (
    (".png", "PNG image"),
    (".jpg", "JPEG image"),
    (".webp", "WebP image"),
    (".mp4", "MP4 video"),
    (".webm", "WebM video"),
    (".mov", "QuickTime video"),
    (".glb", "glTF binary"),
    (".gltf", "glTF"),
    (".usd", "USD"),
    (".usdz", "USDZ"),
    (".obj", "OBJ model"),
)

def filters_for(path) -> tuple:
    """The save filters with this file's own format first.

    So the dialog opens on the format the thing already is. The file on disk
    already carries the right suffix, so this reads it rather than deciding
    again, and a suffix that matches nothing simply leaves the order alone.
    """
    suffix = str(path).lower()
    matching = tuple(f for f in SAVE_FILTERS if suffix.endswith(f[0]))
    return matching + tuple(f for f in SAVE_FILTERS if f not in matching)

#: What the uploads endpoint accepts, so a file that cannot be uploaded is not
#: offered in the first place.
OPEN_FILTERS = (
    (".png", "PNG image"),
    (".jpg", "JPEG image"),
    (".jpeg", "JPEG image"),
    (".webp", "WebP image"),
)


def default_suffix(suggested_name: str) -> str:
    """The suffix to put back on a name the dialog returned without one.

    Taken from what was suggested, NOT from a constant. This used to be `.png`
    whatever the file was, so accepting the dialog's default name for a video
    wrote an MP4 called `.png`: a file the operating system will not open and
    the user has no reason to suspect. A suggestion with no suffix of its own
    still falls back to `.png`, because there is nothing better to say and the
    caller has already failed to name the format.
    """
    suffix = Path(suggested_name).suffix.lower()
    return suffix if suffix else ".png"


def open_save_dialog(*, suggested_name: str, on_chosen) -> bool:
    """Ask where to put a file. `on_chosen` receives a full Path.

    The dialog opens on the format `suggested_name` is in, and puts that same
    suffix back if the name comes home without one.

    Returns False when the picker is unavailable, so the caller can fall back
    rather than leave a button that silently does nothing.
    """
    try:
        from omni.kit.window.filepicker import FilePickerDialog
    except ImportError:
        logger.warning("RunDiffusion: the file picker extension is not available.")
        return False

    dialog: FilePickerDialog | None = None
    fallback = default_suffix(suggested_name)

    def _apply(filename: str, directory: str) -> None:
        # The dialog hands back the name and folder separately, and the name can
        # come back without the extension the user expects.
        chosen = Path(directory) / filename
        if not chosen.suffix:
            chosen = chosen.with_suffix(fallback)
        if dialog is not None:
            dialog.hide()
        on_chosen(chosen)

    def _cancel(*_args) -> None:
        if dialog is not None:
            dialog.hide()

    dialog = FilePickerDialog(
        # Not "Save image". A run returns videos and models too, and a dialog
        # that names the wrong thing is the first place a person doubts what
        # they are about to write.
        "Save file",
        apply_button_label="Save",
        click_apply_handler=_apply,
        click_cancel_handler=_cancel,
        file_extension_options=list(filters_for(suggested_name)),
    )
    # set_filename after construction, not a constructor kwarg: the dialog reads
    # file_extension_options from kwargs but exposes the filename as a method.
    dialog.set_filename(suggested_name)
    dialog.show()
    return True


def open_open_dialog(*, on_chosen) -> bool:
    """Pick an existing image off disk. `on_chosen` receives a full Path.

    The same dialog as saving, with the apply verb and the handler swapped. It
    returns False when the picker extension is missing, so a caller can say so
    rather than leave a button that does nothing.
    """
    try:
        from omni.kit.window.filepicker import FilePickerDialog
    except ImportError:
        logger.warning("RunDiffusion: the file picker extension is not available.")
        return False

    dialog: FilePickerDialog | None = None

    def _apply(filename: str, directory: str) -> None:
        if dialog is not None:
            dialog.hide()
        on_chosen(Path(directory) / filename)

    def _cancel(*_args) -> None:
        if dialog is not None:
            dialog.hide()

    dialog = FilePickerDialog(
        "Choose an image",
        apply_button_label="Open",
        click_apply_handler=_apply,
        click_cancel_handler=_cancel,
        file_extension_options=list(OPEN_FILTERS),
    )
    dialog.show()
    return True
