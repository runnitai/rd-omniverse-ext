"""Pinning the view a capture was taken from, so it can be gone back to.

A capture from a named camera has always been reproducible: the camera is on
the stage, and next week it still frames the same thing. A capture from the
active viewport was not. It was wherever the orbit happened to have stopped,
and the moment the user moved, the framing behind that image was gone. They
could see the picture and had no way back to the place it was taken from.

So a capture can be pinned. The framing becomes a camera prim of its own, the
generation remembers which one, and a button on the result puts the viewport
back through it.

Three rules, and each of them is about the fact that this writes to a stage
somebody else owns.

**Only when asked.** Every ordinary capture leaves the stage exactly as it
found it, and that stays true. Authoring a camera on every generate would put
a hundred prims into a scene over an afternoon's work, all of them named after
nothing, and the person who has to delete them did not ask for any of them.

**Under one parent.** Pinned views land in a scope of their own beside
everything else this plugin adds, so they can be found together and removed in
one action.

**Through a command, so Ctrl+Z takes it back.** Undo is what a person reaches
for the second after they realise they did not want that. A prim authored
straight through the USD API does not appear in the undo stack, and something
that edits a stage without being undoable is a worse offer than not offering.

A camera is built here rather than copied, and the difference is not a style
preference. The first version ran Kit's `CopyPrim` on whatever the viewport was
looking through, which on an untouched stage is `/OmniverseKit_Persp`, and that
prim lives in the SESSION layer. Copying a prim spec copies it where it already
is, so every pinned view landed in the session layer too: drawn in the viewport,
absent from the Stage outliner, and gone the moment the file was saved. Reported
from Base Editor as "I see the Cameras folder but there is nothing inside it".

Building one authors it in the edit target, which is the file the user is
actually working in. What is carried across is stated rather than inherited: the
world transform, so the framing is exact, and the lens attributes below, because
a render that matched the framing but not the focal length would be a different
picture of the same place.
"""

from __future__ import annotations

import contextlib
import logging

from . import stage as stage_module

logger = logging.getLogger(__name__)

#: Where a pinned view is authored. A scope of its own under the plugin's root
#: rather than in among the inserted assets: they are two different kinds of
#: thing, and someone tidying up wants to be able to tell them apart.
RENDER_CAMERA_ROOT = f"{stage_module.ROOT_PATH}/Cameras"

#: What a pinned view is called when nobody gives it a name. Suffixed into
#: View, View_2, View_3 by the same uniqueness rule an inserted asset uses.
DEFAULT_CAMERA_NAME = "View"

#: The lens, carried over from the camera the view was framed through.
#:
#: Named one by one rather than copied wholesale, because this is the list of
#: things that change the PICTURE. Everything a UsdGeom.Camera can hold that is
#: not here (its purpose, its visibility, its custom attributes) belongs to the
#: prim it came from rather than to the view that was framed.
#:
#: `projection` is in the list for a reason worth knowing: an orthographic
#: viewport pinned as a perspective camera would frame the same subject and
#: draw it differently, which is the one kind of difference nobody would think
#: to check for.
LENS_ATTRIBUTES = (
    "focalLength",
    "focusDistance",
    "fStop",
    "horizontalAperture",
    "verticalAperture",
    "horizontalApertureOffset",
    "verticalApertureOffset",
    "clippingRange",
    "projection",
)


def camera_label(camera_path: str | None) -> str:
    """What to call a camera in a sentence: its own name, without the path."""
    if not camera_path:
        return ""
    return camera_path.rsplit("/", 1)[-1]


def _open_stage():
    """The open stage, or None when there is not one.

    Not having one is ordinary rather than broken: a Kit app can be running
    with nothing open, and a test process has no USD at all.
    """
    try:
        import omni.usd
    except ImportError:
        return None
    context = omni.usd.get_context()
    return context.get_stage() if context else None


def _active_viewport():
    """The viewport to read or re-point, or None when there is not one."""
    try:
        from omni.kit.viewport.utility import get_active_viewport

        return get_active_viewport()
    except Exception:  # noqa: BLE001 - a host with no viewport is not a crash
        logger.exception("RunDiffusion: could not reach the active viewport.")
        return None


def _ensure_scope(stage, path: str) -> bool:
    """Make sure `path` exists as a Scope, creating it undoably if it does not."""
    prim = stage.GetPrimAtPath(path)
    if prim and prim.IsValid():
        return True
    return stage_module.run_command(
        "CreatePrimCommand", prim_path=path, prim_type="Scope", select_new_prim=False
    )


@contextlib.contextmanager
def _one_undo_step():
    """Group everything done inside into a single Ctrl+Z.

    Without a running Kit there is no undo stack to group, and this is a
    plain pass-through: nothing here is worth failing a capture over.
    """
    try:
        import omni.kit.undo
    except ImportError:
        yield
        return
    omni.kit.undo.begin_group()
    try:
        yield
    finally:
        omni.kit.undo.end_group()


def _author_camera(stage, source_path: str, target_path: str) -> bool:
    """Give the new camera the framing and the lens of the one being pinned.

    The transform is taken as the source's LOCAL-TO-WORLD matrix and written as
    a single transform op. Two reasons rather than copying its op stack: a
    viewport camera's ops are whatever Kit happened to author while the user
    orbited, and the new camera hangs under a Scope at the origin, so its world
    transform and its local one are the same matrix. Exact, and with nothing
    inherited that has to be understood first.
    """
    from pxr import Usd, UsdGeom

    source = stage.GetPrimAtPath(source_path)
    target = stage.GetPrimAtPath(target_path)
    if not (source and source.IsValid() and target and target.IsValid()):
        return False

    xformable = UsdGeom.Xformable(source)
    if not xformable:
        return False
    matrix = xformable.ComputeLocalToWorldTransform(Usd.TimeCode.Default())

    pinned = UsdGeom.Xformable(target)
    pinned.ClearXformOpOrder()
    pinned.AddTransformOp().Set(matrix)

    for name in LENS_ATTRIBUTES:
        attribute = source.GetAttribute(name)
        if not attribute:
            continue
        value = attribute.Get()
        if value is None:
            # Never set on the source, so there is nothing to carry. Writing
            # the schema fallback would be this module inventing a lens.
            continue
        target.CreateAttribute(name, attribute.GetTypeName()).Set(value)
    return True


def _authoring_layer(stage):
    """Swap a session-layer edit target for the root layer, and say so.

    Returns whatever the edit target was, or None when it was left alone.

    The session layer is where Kit keeps state that is deliberately not part of
    the file: it is not saved, so a camera authored into it disappears the next
    time the stage is opened. Pinning a view is the opposite of that promise.

    Only the session layer is swapped. A user editing a sublayer chose that
    sublayer, and moving their authoring somewhere else would be a bigger
    liberty than this is entitled to take.
    """
    try:
        from pxr import Usd

        session = stage.GetSessionLayer()
        target = stage.GetEditTarget()
        if session is None or target.GetLayer() != session:
            return None
        stage.SetEditTarget(Usd.EditTarget(stage.GetRootLayer()))
        return target
    except Exception:  # noqa: BLE001 - a stage that will not say is left alone
        logger.exception("RunDiffusion: could not read the edit target.")
        return None


def save_active_view(name: str = DEFAULT_CAMERA_NAME) -> str | None:
    """Pin what the viewport is looking through as a camera of its own.

    Returns the new prim's path, or None when there was nothing to pin or the
    stage would not take it. None is a state to report, never one to raise on:
    the capture this belongs to has already succeeded and its image is already
    in the form, so a framing that could not be saved costs the user the button
    they pressed and nothing else.

    Everything is done inside ONE undo group. Pinning a view is one action as
    far as the person who pressed the button is concerned, and it takes three
    commands to do; without the group, Ctrl+Z would peel it apart a command at
    a time and the second press would leave a camera under no parent.
    """
    stage = _open_stage()
    if stage is None:
        logger.info("RunDiffusion: there is no open stage to pin a view on.")
        return None

    viewport = _active_viewport()
    if viewport is None:
        return None
    try:
        source = str(viewport.camera_path)
    except Exception:  # noqa: BLE001 - a viewport mid-teardown has no camera
        logger.exception("RunDiffusion: could not read the viewport camera.")
        return None

    path = stage_module.unique_prim_path(
        stage_module.existing_paths(stage),
        stage_module.usd_name(name, fallback=DEFAULT_CAMERA_NAME),
        root=RENDER_CAMERA_ROOT,
    )

    previous_target = _authoring_layer(stage)
    try:
        with _one_undo_step():
            if not _ensure_scope(stage, stage_module.ROOT_PATH) or not _ensure_scope(
                stage, RENDER_CAMERA_ROOT
            ):
                logger.warning("RunDiffusion: could not make a place to pin the view.")
                return None
            # `select_new_prim` off, because the user is in the middle of
            # building a run rather than looking at the outliner. Taking their
            # selection away to hand them a camera the panel is about to name
            # for them would be a change they did not ask for.
            if not stage_module.run_command(
                "CreatePrimCommand",
                prim_path=path,
                prim_type="Camera",
                select_new_prim=False,
            ):
                logger.warning("RunDiffusion: the view could not be pinned.")
                return None
            if not _author_camera(stage, source, path):
                logger.warning("RunDiffusion: %s could not be given a framing.", path)
                return None
    except Exception:  # noqa: BLE001 - a stage this cannot write is not a crash
        logger.exception("RunDiffusion: pinning the view raised.")
        return None
    finally:
        if previous_target is not None:
            try:
                stage.SetEditTarget(previous_target)
            except Exception:  # noqa: BLE001 - nothing here can help the user
                logger.exception("RunDiffusion: could not restore the edit target.")

    # Verified rather than assumed, for the same reason an inserted asset is:
    # a command that quietly did nothing would otherwise leave the panel
    # reporting a camera that is not on the stage, and a button on the result
    # that goes nowhere.
    prim = stage.GetPrimAtPath(path)
    if not (prim and prim.IsValid()):
        logger.warning("RunDiffusion: %s was not created.", path)
        return None
    logger.info("RunDiffusion: pinned the view as %s.", path)
    return path


def look_through(camera_path: str) -> bool:
    """Point the active viewport at `camera_path`. True when it took.

    This is the whole return journey: someone is looking at a render they made
    an hour ago and wants to be standing where it was taken from. Pointing the
    viewport is enough, and it is reversible by the same route, because the
    camera they were on is still on the stage and still in the dropdown.
    """
    if not camera_path:
        return False

    stage = _open_stage()
    if stage is not None:
        prim = stage.GetPrimAtPath(camera_path)
        if not (prim and prim.IsValid()):
            # Deleted since the run. Reported so the caller can say so, rather
            # than leaving the viewport where it was and letting the button
            # look like it missed. Assigning a path that does not resolve is
            # not an error Kit raises, which is the same quiet failure a
            # capture from a deleted camera already guards against.
            logger.info("RunDiffusion: %s is no longer on the stage.", camera_path)
            return False

    viewport = _active_viewport()
    if viewport is None:
        return False
    try:
        viewport.camera_path = camera_path
    except Exception:  # noqa: BLE001 - nothing here can help the user
        logger.exception("RunDiffusion: could not look through %s.", camera_path)
        return False
    return True
