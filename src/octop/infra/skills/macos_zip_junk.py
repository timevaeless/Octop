"""Filter macOS-specific junk entries from ZIP archives.

Archives created by macOS Finder (or bsdtar with xattrs) carry AppleDouble
resource-fork entries: a ``__MACOSX/`` mirror tree plus ``._<name>`` files.
Besides polluting extracted output, their presence breaks wrapper-directory
stripping in :func:`octop.infra.skills.skillhub_market.parse_zip_entries`
(the extra ``__MACOSX`` top level defeats the "single top-level folder"
detection), which made valid expert packages fail upload validation with
"must contain a root manifest.json".

T4 note (2026-10-21): kept in a standalone module so the upstream
``skillhub_market`` file only carries a one-line hook.
"""

from __future__ import annotations

_APPLEDOUBLE_PREFIX = "._"
_MACOSX_DIR = "__MACOSX"


def is_macos_junk_entry(name: str) -> bool:
    """Return True if *name* is a macOS resource-fork / AppleDouble entry.

    Matches:

    - anything under a ``__MACOSX/`` directory (Finder resource forks,
      case-sensitive: Finder always writes uppercase, so a user directory
      named ``__macosx`` is preserved);
    - ``._<name>`` AppleDouble files (they normally live inside
      ``__MACOSX/`` but are filtered at any depth for robustness).

    Normal dotfiles (``.gitignore``, ``.env``…) are NOT matched — only the
    ``._`` AppleDouble prefix is.
    """
    parts = [part for part in name.split("/") if part]
    if not parts:
        return False
    if _MACOSX_DIR in parts:
        return True
    return parts[-1].startswith(_APPLEDOUBLE_PREFIX)
