"""File modes for the gate tree, because the tree is evidence and secrets at once.

The gate writes a signing key, an audit log, its anchor, case records and page snapshots. Every one
of those was created with `Path.write_text` and inherited the process umask, which on a normal
workstation is 0o022 or 0o002. Measured on the development tree:

    -rw-rw-r-- casework/permit.key              <- the key that signs collection permits
    -rw-rw-r-- casework/audit/audit.jsonl       <- the audit record
    -rw-rw-r-- casework/audit/audit.anchor.json <- the digest the audit is checked against
    -rw-rw-r-- casework/cases/CASE-DIR-8/snapshots/snapshot-0005.json

Three separate failures follow from that, and they are the three properties this project claims:

**Authorisation.** `permit.key` is the HMAC key that verifies a collection permit. A permit is the
project's legal control: it is what makes "we were authorised to fetch this" checkable. A
world-readable key means any local user can mint a permit for any case, any principal, any expiry,
and every gate check passes, because the check is a signature check and they have the pen.

**Integrity.** The audit log and its anchor were group-writable. The E4 stage measures tamper
*detection*, and its stated assumption is that an adversary who can rewrite both the snapshots and
the ledger can produce a consistent forgery. With these modes that adversary does not need
privilege; membership of the file's group is enough, and the assumption the report carefully states
was, by default, trivially true.

**Confidentiality.** Snapshots hold what was collected about a target. They were world-readable.

The defaults here are closed (0o600 files, 0o700 directories) and repair an existing tree rather
than only applying to new files, because a deployment already on disk is the one that matters.
Loosening them for a shared deployment is a deliberate act the operator performs, not something
that happens because nobody set a umask.
"""

from __future__ import annotations

import os
import stat
from pathlib import Path

#: Files that only their owner should read or write. Secrets, audit records and evidence.
FILE_MODE = 0o600
#: Directories that only their owner should traverse.
DIR_MODE = 0o700


def secure_dir(path: Path) -> Path:
    """Create a directory (and parents) with no group or other access."""
    path.mkdir(parents=True, exist_ok=True)
    _chmod(path, DIR_MODE)
    return path


def secure_write_text(path: Path, text: str, *, encoding: str = "utf-8") -> None:
    """Write a file that is owner-only from the moment it exists.

    Not `write_text` followed by a chmod: that leaves a window in which the file exists with the
    process umask's permissions, and a snapshot holding a target's page is exactly the window that
    matters. The file is created with the mode it will keep.
    """
    parent = path.parent
    if parent and not parent.exists():
        secure_dir(parent)
    flags = os.O_WRONLY | os.O_CREAT | os.O_TRUNC
    handle = os.open(path, flags, FILE_MODE)
    try:
        with os.fdopen(handle, "w", encoding=encoding) as fh:
            fh.write(text)
    finally:
        # An existing file keeps its old mode through O_CREAT, so the mode is applied either way.
        _chmod(path, FILE_MODE)


def secure_append_text(path: Path, text: str, *, encoding: str = "utf-8") -> None:
    """Append one line, creating the file owner-only if it does not exist yet.

    The audit log is appended to, not rewritten, so `secure_write_text` does not fit. `O_APPEND`
    with an explicit mode is what an audit record needs: the entry lands at the end in one write,
    and the file never exists with a looser mode than the one it keeps.
    """
    if path.parent and not path.parent.exists():
        secure_dir(path.parent)
    handle = os.open(path, os.O_WRONLY | os.O_CREAT | os.O_APPEND, FILE_MODE)
    with os.fdopen(handle, "a", encoding=encoding) as fh:
        fh.write(text)
    _chmod(path, FILE_MODE)


def _chmod(path: Path, mode: int) -> bool:
    """Set `mode` if the path is more permissive than that. Returns True when it changed."""
    try:
        current = stat.S_IMODE(path.stat().st_mode)
    except OSError:
        return False
    if current & ~mode == 0:
        return False
    try:
        os.chmod(path, mode)
        return True
    except OSError:
        # A read-only mount or a file owned by another user is not this code's to fix, and
        # failing the whole run over a chmod would turn a harden step into an outage.
        return False


def loosened_paths(root: Path) -> list[Path]:
    """Every file or directory under `root` a group or other user can read, write or traverse."""
    found: list[Path] = []
    if not root.exists():
        return found
    for path in [root, *sorted(root.rglob("*"))]:
        try:
            mode = stat.S_IMODE(path.stat().st_mode)
        except OSError:
            continue
        # 0o077 is "anything for group or other"; for a directory, the execute bit in there is the
        # right to traverse it, which is as good as reading the names inside.
        if mode & 0o077:
            found.append(path)
    return found


def harden_tree(root: Path) -> list[Path]:
    """Close an existing gate tree, and report which paths were open.

    The repair matters more than the creation path: a tree written before this module existed is
    the one sitting on someone's disk, with a permit key that has been world-readable since the day
    it was created. Returning the changed paths lets the caller say so out loud instead of quietly
    fixing permissions and leaving the operator unaware that their key was exposed.
    """
    changed: list[Path] = []
    if not root.exists():
        return changed
    for path in [root, *sorted(root.rglob("*"))]:
        target = DIR_MODE if path.is_dir() else FILE_MODE
        if _chmod(path, target):
            changed.append(path)
    return changed
