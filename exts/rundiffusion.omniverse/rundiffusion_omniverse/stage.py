"""Putting a generated 3D asset onto the stage the user already has open.

This is the one thing this plugin can do that no other RunDiffusion surface
can, and it is also the one that touches something the user owns. Their stage
is their work, so the rules here are narrower than anywhere else in the panel:

**Reference, never merge.** The asset arrives as its own file and is referenced
under a prim of its own. Nothing existing is edited, and removing the prim
removes the asset completely.

**Under one parent.** Everything this plugin adds goes under `/RunDiffusion`,
so a person can find what came from here and delete it in one action without
hunting through their hierarchy.

**Through a command, so it can be undone.** Ctrl+Z is what a user reaches for
when something lands wrong, and an insertion done straight through the USD API
does not appear in the undo stack. The direct call is kept as a fallback for a
host with no command registered, and says so rather than pretending it undoes.

**USD is referenceable; the rest is not.** A `.usd`, `.usda`, `.usdc` or
`.usdz` can be referenced as it stands. A `.glb`, `.gltf`, `.obj` or `.fbx`
cannot, and has to be converted first. `omni.kit.asset_converter` ships in the
stock Kit SDK and in the packaged Composer host (both checked before depending
on it), so the conversion costs no vendoring.
"""

from __future__ import annotations

import logging
from pathlib import Path

logger = logging.getLogger(__name__)

#: The prim everything this plugin adds lives under.
ROOT_PATH = "/RunDiffusion"

#: Referenceable as they stand.
USD_SUFFIXES = frozenset({".usd", ".usda", ".usdc", ".usdz"})

#: Need `omni.kit.asset_converter` first. USD cannot reference these directly,
#: and referencing one anyway produces a prim that resolves to nothing: an
#: insertion that reports success and adds an empty Xform.
CONVERTIBLE_SUFFIXES = frozenset({".glb", ".gltf", ".obj", ".fbx"})


def needs_conversion(path: Path) -> bool:
    """True when this file has to become USD before the stage can take it."""
    return path.suffix.lower() in CONVERTIBLE_SUFFIXES


def is_supported(path: Path) -> bool:
    """True when this file can reach the stage at all, converted or not."""
    suffix = path.suffix.lower()
    return suffix in USD_SUFFIXES or suffix in CONVERTIBLE_SUFFIXES


def usd_name(text: str, fallback: str = "asset") -> str:
    """`text` as a name USD will accept, or `fallback` when nothing survives.

    USD accepts letters, digits and underscores, and will not accept a name
    starting with a digit. A generated result is named `result-<hex>.glb`, so
    without this every insertion would be rejected on the hyphen.
    """
    cleaned = "".join(
        character if character.isalnum() else "_" for character in (text or "")
    ).strip("_")
    # Stripped first, so a name that was nothing but separators reads as having
    # no name at all rather than becoming a row of underscores.
    if not cleaned:
        return fallback
    if cleaned[0].isdigit():
        return f"{fallback}_{cleaned}"
    return cleaned


def prim_name(source: Path) -> str:
    """A USD-legal prim name derived from the file."""
    return usd_name(source.stem)


def unique_prim_path(taken: set[str], name: str, root: str = ROOT_PATH) -> str:
    """`/RunDiffusion/<name>`, suffixed until it collides with nothing.

    Generating the same tool twice is ordinary, and the second result must not
    silently replace the first one on the stage.
    """
    candidate = f"{root}/{name}"
    if candidate not in taken:
        return candidate
    index = 2
    while f"{candidate}_{index}" in taken:
        index += 1
    return f"{candidate}_{index}"


def _sdf():
    from pxr import Sdf

    return Sdf


def run_command(name: str, **kwargs) -> bool:
    """Run one Kit command and say whether it actually worked.

    `omni.kit.commands.execute` does not raise when the command inside it
    fails: it logs and hands back a falsey result. Ignoring that return value
    is how a failed AddReference came to look like a successful insertion.
    """
    import omni.kit.commands

    outcome = omni.kit.commands.execute(name, **kwargs)
    if isinstance(outcome, tuple):
        return bool(outcome and outcome[0])
    return outcome is not False


def _has_reference(stage, path: str) -> bool:
    """Whether the prim exists AND something is actually referenced on it.

    An Xform with no reference is the shape a failed insertion leaves behind:
    a prim in the tree and nothing to see in the viewport.
    """
    try:
        prim = stage.GetPrimAtPath(path)
        return bool(prim and prim.IsValid() and prim.HasAuthoredReferences())
    except Exception:  # noqa: BLE001 - an unreadable prim is not a success
        logger.exception("RunDiffusion: could not confirm the reference on %s.", path)
        return False


def select(path: str) -> None:
    """Select the new prim, so 'added to the stage' is visibly true.

    A referenced asset can land anywhere in a large scene, and an insertion
    nobody can find reads exactly like one that did not happen.
    """
    try:
        import omni.usd

        context = omni.usd.get_context()
        if context is not None:
            context.get_selection().set_selected_prim_paths([path], True)
    except Exception:  # noqa: BLE001 - selection is a courtesy, not the result
        logger.debug("RunDiffusion: could not select %s.", path)


def existing_paths(stage) -> set[str]:
    try:
        return {prim.GetPath().pathString for prim in stage.Traverse()}
    except Exception:  # noqa: BLE001 - an unreadable stage is not a crash here
        logger.exception("RunDiffusion: could not read the stage's prims.")
        return set()


async def convert_to_usd(source: Path, destination: Path) -> bool:
    """Turn a non-USD asset into something the stage can reference.

    Returns False rather than raising: a conversion that cannot run leaves the
    file on disk, where saving it and sending it still work.
    """
    try:
        import omni.kit.asset_converter as converter
    except ImportError:
        logger.warning(
            "RunDiffusion: omni.kit.asset_converter is not available, so %s "
            "cannot be added to the stage.",
            source.suffix,
        )
        return False

    try:
        task = converter.get_instance().create_converter_task(
            str(source), str(destination), None
        )
        if not await task.wait_until_finished():
            logger.warning(
                "RunDiffusion: could not convert %s. %s",
                source.name,
                task.get_error_message(),
            )
            return False
    except Exception:  # noqa: BLE001 - conversion is a bonus, not the result
        logger.exception("RunDiffusion: the asset conversion failed.")
        return False
    return destination.exists()


async def add_to_stage(source: Path, work_dir: Path) -> str | None:
    """Reference `source` onto the open stage. Returns the prim path, or None.

    `work_dir` is where a converted copy is written, so the original the user
    can save is never moved or rewritten.
    """
    try:
        import omni.usd
    except ImportError:
        logger.warning("RunDiffusion: omni.usd is not available.")
        return None

    if not is_supported(source):
        logger.info("RunDiffusion: %s cannot be added to a stage.", source.suffix)
        return None

    asset = source
    if needs_conversion(source):
        converted = work_dir / f"{source.stem}.usd"
        if not await convert_to_usd(source, converted):
            return None
        asset = converted

    context = omni.usd.get_context()
    stage = context.get_stage() if context else None
    if stage is None:
        logger.info("RunDiffusion: there is no open stage to add to.")
        return None

    path = unique_prim_path(existing_paths(stage), prim_name(source))

    try:
        # Through the commands so Ctrl+Z takes it back off the stage. The prim
        # has to exist before a reference can be put on it, and both steps are
        # commands so undo removes both.
        if run_command(
            "CreatePrimCommand",
            prim_path=path,
            prim_type="Xform",
            select_new_prim=False,
        ) and run_command(
            "AddReference",
            stage=stage,
            prim_path=_sdf().Path(path),
            # An Sdf.Reference, not a string. The command reads `.assetPath`
            # off whatever it is given, so a path string raises inside it and
            # the command reports the failure without raising to the caller.
            reference=_sdf().Reference(str(asset)),
        ):
            if _has_reference(stage, path):
                return path
        logger.info(
            "RunDiffusion: the undoable insertion did not take, adding directly. "
            "This one will not come back with Ctrl+Z."
        )
    except Exception:  # noqa: BLE001 - fall back rather than lose the insertion
        logger.exception("RunDiffusion: the undoable insertion raised.")

    try:
        prim = stage.DefinePrim(path, "Xform")
        prim.GetReferences().AddReference(str(asset))
    except Exception:  # noqa: BLE001 - the file is still on disk either way
        logger.exception("RunDiffusion: could not add the asset to the stage.")
        return None

    # Verified rather than assumed. The first version reported the prim path
    # whatever happened, so a reference that never attached was indistinguishable
    # from one that did: the panel said it had been added and the stage was
    # unchanged.
    if not _has_reference(stage, path):
        logger.warning("RunDiffusion: the reference did not attach to %s.", path)
        return None
    return path
