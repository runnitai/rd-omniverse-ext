"""Sealed on-disk storage for the refresh token, device id and chosen account.

Windows DPAPI through `ctypes`. Two reasons it is `ctypes` rather than a
library: `keyring` is the one
dependency this plugin needs that Kit does NOT prebundle, and DPAPI needs no
wheel at all, so the vendoring question disappears instead of being solved.

The sealed blob is scoped to the current user, so another Windows account on the
same machine cannot decrypt it, and the entropy string scopes it to this plugin,
so no other RunDiffusion host can either.

There is a line this module has to respect: a store that cannot be read is NOT
the same as being signed out. If unsealing fails, the caller is told the
secure store is unavailable rather than being silently shown a sign-in screen,
because silently re-prompting trains people to re-authenticate through whatever
is in front of them.
"""

from __future__ import annotations

import ctypes
import ctypes.wintypes
import json
import logging
import os
import sys
from dataclasses import dataclass
from pathlib import Path

from .constants import TOKEN_STORE_ENTROPY, TOKEN_STORE_FOLDER_NAME

logger = logging.getLogger(__name__)


class SecureStoreUnavailable(RuntimeError):
    """The store exists but could not be read, which is not the same as absent."""


@dataclass
class StoredSession:
    refresh_token: str
    device_id: str | None = None
    #: Which account the last run was billed to, so reopening the plugin does
    #: not quietly move the next one to the personal account.
    #:
    #: Not a secret, and it rides in the sealed blob anyway because its LIFETIME
    #: is exactly this file's: it only means anything while signed in, and
    #: signing out must forget it rather than leave a team id pointing at a team
    #: the next person on this machine may not be in. A second file would have
    #: to be kept in step with this one to get that right.
    selected_account_id: str | None = None


class _DataBlob(ctypes.Structure):
    _fields_ = [("cbData", ctypes.wintypes.DWORD), ("pbData", ctypes.POINTER(ctypes.c_char))]


def _blob(data: bytes) -> _DataBlob:
    buffer = ctypes.create_string_buffer(data, len(data))
    return _DataBlob(len(data), ctypes.cast(buffer, ctypes.POINTER(ctypes.c_char)))


def _blob_bytes(blob: _DataBlob) -> bytes:
    return ctypes.string_at(blob.pbData, blob.cbData)


def _free(blob: _DataBlob) -> None:
    if blob.pbData:
        ctypes.windll.kernel32.LocalFree(blob.pbData)


def _protect(plaintext: bytes) -> bytes:
    source, entropy, out = _blob(plaintext), _blob(TOKEN_STORE_ENTROPY.encode()), _DataBlob()
    # CRYPTPROTECT_UI_FORBIDDEN (0x1): never prompt. A modal credential dialog
    # raised from a render thread inside a DCC app is not an acceptable outcome.
    if not ctypes.windll.crypt32.CryptProtectData(
        ctypes.byref(source), None, ctypes.byref(entropy), None, None, 0x01, ctypes.byref(out)
    ):
        raise SecureStoreUnavailable("Windows could not seal the RunDiffusion session.")
    try:
        return _blob_bytes(out)
    finally:
        _free(out)


def _unprotect(sealed: bytes) -> bytes:
    source, entropy, out = _blob(sealed), _blob(TOKEN_STORE_ENTROPY.encode()), _DataBlob()
    if not ctypes.windll.crypt32.CryptUnprotectData(
        ctypes.byref(source), None, ctypes.byref(entropy), None, None, 0x01, ctypes.byref(out)
    ):
        raise SecureStoreUnavailable("Windows could not unseal the stored RunDiffusion session.")
    try:
        return _blob_bytes(out)
    finally:
        _free(out)


def _store_path() -> Path:
    root = os.environ.get("LOCALAPPDATA") or str(Path.home())
    return Path(root) / TOKEN_STORE_FOLDER_NAME / "session.bin"


def is_supported() -> bool:
    """DPAPI is Windows-only, and the supported Kit hosts are too, so this is enough.

    A Linux Kit host would need its own answer before this plugin ships there.
    Returning False keeps that an explicit unsupported state rather than a
    plaintext fallback, which must never ship.
    """
    return sys.platform == "win32"


def load() -> StoredSession | None:
    """Return the stored session, or None if nobody has signed in on this machine.

    Raises SecureStoreUnavailable when a session exists but cannot be read, so
    the caller can distinguish that from signed-out.
    """
    if not is_supported():
        raise SecureStoreUnavailable("Secure storage is not available on this platform.")

    path = _store_path()
    if not path.exists():
        return None

    payload = json.loads(_unprotect(path.read_bytes()).decode("utf-8"))
    refresh_token = payload.get("refresh_token")
    if not refresh_token:
        # A readable blob with nothing usable in it is corruption, not absence.
        raise SecureStoreUnavailable("The stored RunDiffusion session is unreadable.")
    return StoredSession(
        refresh_token=refresh_token,
        device_id=payload.get("device_id"),
        selected_account_id=payload.get("selected_account_id"),
    )


def save(session: StoredSession) -> None:
    if not is_supported():
        raise SecureStoreUnavailable("Secure storage is not available on this platform.")

    path = _store_path()
    path.parent.mkdir(parents=True, exist_ok=True)
    sealed = _protect(
        json.dumps(
            {
                "refresh_token": session.refresh_token,
                "device_id": session.device_id,
                "selected_account_id": session.selected_account_id,
            }
        ).encode("utf-8")
    )
    # Write to a sibling then replace, so an interrupted write cannot leave a
    # half-sealed file that reads as corruption on next launch.
    temporary = path.with_suffix(".tmp")
    temporary.write_bytes(sealed)
    os.replace(temporary, path)


def clear() -> None:
    """Sign out. Missing is success: the goal is that nothing remains."""
    try:
        _store_path().unlink()
    except FileNotFoundError:
        pass
    except OSError:
        logger.exception("RunDiffusion: could not remove the stored session.")
