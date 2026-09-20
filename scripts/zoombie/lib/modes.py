"""Deploy our Zoo Code custom mode(s) into a user-owned ``custom_modes.yaml``.

The problem this module exists to solve is *shared ownership*. A skill is one
file that only we write, so deployment can simply overwrite it. A mode lives in a
single YAML document that the user also owns: the extension writes it, and a user
may add their own modes by hand. Overwriting that file would destroy their work.

Two decisions follow, and both are deliberate:

1. **The merge is textual, never a parse-and-reserialize.** We never decode the
   foreign entries. We locate *our* block by its ``- slug:`` line and splice in
   the new block, so every byte the user did not ask us to change is preserved
   exactly -- including comments, quoting style and ordering. This also means no
   YAML dependency is introduced and there is no emitter that could drift from
   the source of truth.

2. **The source file IS the artifact.** ``modes/<name>.yaml`` holds a normal
   ``customModes:`` list, and the item text we splice is taken verbatim from it.
   There is no template step, so what a reader inspects in the repo is exactly
   what lands in the user's config.

The ownership key is the slug. We only ever replace an entry whose slug matches
ours; a file with no ``customModes:`` key is refused rather than repaired, so a
malformed document is never silently rewritten.
"""

from __future__ import annotations

import os
import re
from dataclasses import dataclass

from . import process
from .errors import ZoombieError

# Folder in the repo holding the mode sources, mirroring ``skills/``.
SOURCE_FOLDER = "modes"

# The extension's settings folder, relative to %APPDATA%. The extension id is
# fixed by the product, so the global modes file has one known location.
EXTENSION_ID = "zoocodeorganization.zoo-code"

# Environment override, used by the tests and by a non-standard install.
TARGET_ENV_VAR = "ZOOMBIE_MODES_PATH"

# The top-level key both the global file and a workspace ``.roomodes`` use.
ROOT_KEY = "customModes"

# A list item whose ``slug`` is the first key. Anchored so a slug mentioned inside
# a roleDefinition is never mistaken for a mode boundary.
_ITEM_RE = re.compile(r"^(?P<indent>\s*)-\s*slug:\s*(?P<quote>[\"']?)(?P<slug>[A-Za-z0-9._-]+)(?P=quote)\s*$")
_ROOT_KEY_RE = re.compile(rf"^(?P<indent>\s*){ROOT_KEY}\s*:\s*(?P<value>.*)$")


@dataclass
class Source:
    """A parsed mode source file."""

    path: str
    slugs: list[str]
    items_text: str


@dataclass
class MergeResult:
    """What a merge did or would do."""

    text: str
    action: str  # "created", "updated", "up to date"
    slug: str


# ---------------------------------------------------------------------------
# Where the files live
# ---------------------------------------------------------------------------

def global_modes_path() -> str:
    """The global ``custom_modes.yaml`` path.

    ``ZOOMBIE_MODES_PATH`` wins so a test (or an unusual install) can redirect it
    without touching the real user profile.
    """
    override = os.environ.get(TARGET_ENV_VAR, "").strip()
    if override:
        return override
    appdata = os.environ.get("APPDATA", "")
    if not appdata:
        raise ZoombieError(
            "APPDATA is not set, so the global custom_modes.yaml cannot be located"
        )
    return os.path.join(appdata, "Code", "User", "globalStorage", EXTENSION_ID, "settings", "custom_modes.yaml")


def workspace_modes_path(project_root: str) -> str:
    """The project-local ``.roomodes`` path for a workspace root."""
    return os.path.join(project_root, ".roomodes")


def source_dir(cli_dir: str) -> str:
    """The repo ``modes/`` folder, given the directory holding the CLI package."""
    return os.path.normpath(os.path.join(cli_dir, "..", SOURCE_FOLDER))


# ---------------------------------------------------------------------------
# Reading and writing text
# ---------------------------------------------------------------------------

def _read_text(path: str) -> str:
    from . import paths  # local import keeps this module import-light

    if not paths.is_file(path):
        return ""
    with open(paths.to_extended(path), "r", encoding="utf-8", errors="replace") as handle:
        return handle.read()


def _write_text(path: str, text: str) -> None:
    from . import paths

    paths.ensure_dir(os.path.dirname(path))
    # newline="" so a lone "\n" we emit is written as-is and never doubled.
    # encoding utf-8 (no BOM): YAML is read by a JS parser that would choke on one.
    with open(paths.to_extended(path), "w", encoding="utf-8", newline="") as handle:
        handle.write(text)


# ---------------------------------------------------------------------------
# Reading a mode source
# ---------------------------------------------------------------------------

def _lines(text: str) -> list[str]:
    """Split into lines, tolerating CRLF, and drop the trailing empty element."""
    normalised = text.replace("\r\n", "\n").replace("\r", "\n")
    result = normalised.split("\n")
    if result and result[-1] == "":
        result.pop()
    return result


def read_source(path: str) -> Source:
    """Read a ``customModes:`` source and return its item text verbatim.

    The item text is the exact slice of the source below the ``customModes:`` key,
    so it can be spliced into a target without any re-rendering.
    """
    text = _read_text(path)
    if not text.strip():
        raise ZoombieError(f"Mode source is empty: {path}")

    lines = _lines(text)
    root = _find_root_key(lines)
    if root is None:
        raise ZoombieError(f"Mode source has no '{ROOT_KEY}:' key: {path}")

    items = _items_slice(lines, root)
    if not items:
        raise ZoombieError(f"Mode source defines no modes: {path}")

    slugs = [match.group("slug") for line in items if (match := _ITEM_RE.match(line))]
    if not slugs:
        raise ZoombieError(f"Mode source has no '- slug:' item: {path}")
    return Source(path=path, slugs=slugs, items_text="\n".join(items) + "\n")


def _find_root_key(lines: list[str]) -> int | None:
    """Index of the top-level ``customModes:`` line, or None."""
    for index, line in enumerate(lines):
        if line.startswith("#") or not line.strip():
            continue
        match = _ROOT_KEY_RE.match(line)
        if match and match.group("indent") == "":
            return index
    return None


def _items_slice(lines: list[str], root_index: int) -> list[str]:
    """The list-item lines below the root key, verbatim, with blanks trimmed.

    The list ends at the first non-blank line that is not indented deeper than the
    key, so a following top-level key is not swallowed.
    """
    collected: list[str] = []
    for line in lines[root_index + 1:]:
        if not line.strip():
            collected.append(line)
            continue
        if line[0] not in " \t":
            break
        collected.append(line)
    while collected and not collected[-1].strip():
        collected.pop()
    return collected


# ---------------------------------------------------------------------------
# The merge
# ---------------------------------------------------------------------------

def _item_indent(lines: list[str], root_index: int) -> int:
    """Indentation of the ``customModes:`` list's own items.

    Derived from the first ``- slug:`` line below the key. Matching this exactly
    is what stops a slug written *inside* a foreign block scalar -- say a
    roleDefinition that happens to contain a line like ``- slug: zoombie`` -- from
    being mistaken for a mode boundary. A block scalar's content is indented the
    same as the keys it feeds, so it can never sit at the list's own indent.
    """
    for line in lines[root_index + 1:]:
        match = _ITEM_RE.match(line)
        if match:
            return len(match.group("indent"))
    return -1


def _find_item(lines: list[str], slug: str, root_index: int) -> tuple[int, int] | None:
    """Line span ``[start, end)`` of the item whose slug matches, or None.

    Only the region below ``customModes:`` is searched, so a slug that appears in
    prose elsewhere in the document can never be matched.
    """
    item_indent = _item_indent(lines, root_index)
    if item_indent < 0:
        return None
    for index in range(root_index + 1, len(lines)):
        match = _ITEM_RE.match(lines[index])
        if not match or match.group("slug") != slug:
            continue
        indent = len(match.group("indent"))
        if indent != item_indent:
            # A deeper or shallower item-shaped line: it is content, not a mode.
            continue
        end = len(lines)
        for probe in range(index + 1, len(lines)):
            line = lines[probe]
            if not line.strip():
                continue
            leading = len(line) - len(line.lstrip(" \t"))
            if leading <= indent:
                end = probe
                break
        while end > index + 1 and not lines[end - 1].strip():
            end -= 1
        return index, end
    return None


def _append_at(lines: list[str], root: int, items: list[str]) -> list[str]:
    """Append items to the end of the ``customModes:`` list."""
    root_match = _ROOT_KEY_RE.match(lines[root])
    value = root_match.group("value").strip() if root_match else ""

    if value.startswith("[") and value != "[]":
        # An inline flow list we do not rewrite: appending to it while keeping it
        # valid would mean parsing YAML, and silently dropping its contents would
        # lose user data. Refuse instead.
        raise ZoombieError(
            f"'{ROOT_KEY}:' is an inline list, which this tool does not rewrite. "
            "Convert it to a block list (one '- slug:' item per line) and retry."
        )

    # NOTE: a bare ``customModes:`` is NOT an empty list when items follow it. It
    # is only empty when it explicitly says so (``[]``/``null``/``~``) or when no
    # item line follows. Getting this wrong prepends our entry ahead of the
    # user's, which is how the foreign-first ordering test caught it.
    has_items = _item_indent(lines, root) >= 0

    if value in ("[]", "null", "~") or not has_items:
        # Empty list or bare key with no children: (re)write the list here.
        head = lines[: root]
        tail = lines[root + 1:]
        return head + [f"{ROOT_KEY}:"] + items + tail

    last = root
    for index in range(root + 1, len(lines)):
        if not lines[index].strip():
            continue
        if lines[index][0] in " \t":
            last = index
        else:
            break
    head = lines[: last + 1]
    tail = lines[last + 1:]
    return head + items + tail


def merge(text: str, items_text: str, slug: str) -> MergeResult:
    """Merge ``items_text`` for ``slug`` into ``text``.

    Replaces only the matching entry; appends when it is absent; refuses a
    document it cannot understand rather than repairing it.
    """
    items = _lines(items_text)
    if not items:
        raise ZoombieError("Nothing to merge: the mode items are empty")

    if not text.strip():
        return MergeResult(text=f"{ROOT_KEY}:\n" + "\n".join(items) + "\n",
                           action="created", slug=slug)

    lines = _lines(text)
    root = _find_root_key(lines)
    if root is None:
        raise ZoombieError(
            f"Refusing to modify a file with no '{ROOT_KEY}:' key. Fix it by hand, "
            "or point ZOOMBIE_MODES_PATH at the correct file."
        )

    span = _find_item(lines, slug, root)
    if span is None:
        merged = _append_at(lines, root, items)
        return MergeResult(text="\n".join(merged) + "\n", action="created", slug=slug)

    start, end = span
    existing = _normalise(lines[start:end])
    incoming = _normalise(items)
    if existing == incoming:
        # No write at all: the file already holds exactly this block.
        return MergeResult(text=text, action="up to date", slug=slug)

    merged = lines[:start] + items + lines[end:]
    return MergeResult(text="\n".join(merged) + "\n", action="updated", slug=slug)


def _normalise(lines: list[str]) -> list[str]:
    """Compare blocks on content, not on trailing whitespace or blank padding."""
    trimmed = [line.rstrip() for line in lines]
    while trimmed and not trimmed[-1]:
        trimmed.pop()
    while trimmed and not trimmed[0]:
        trimmed.pop(0)
    return trimmed


# ---------------------------------------------------------------------------
# Deployment
# ---------------------------------------------------------------------------

def deploy(
    source_file: str,
    target_path: str,
    *,
    dry_run: bool = False,
    force: bool = False,
) -> dict:
    """Deploy one mode source file into ``target_path``.

    Returns ``{"slug", "action", "path", "source"}``. ``dry_run`` computes the
    action and the resulting text but writes nothing. ``force`` rewrites the file
    even when the content already matches, which matters when a hand-edited file
    has drifted in whitespace but the block itself is unchanged.
    """
    source = read_source(source_file)
    # The source is expected to hold the entries to deploy. Splicing the whole
    # item text at once keeps them in the order the file declares.
    existing = _read_text(target_path)
    result = merge(existing, source.items_text, source.slugs[0])
    record = {
        "slug": result.slug,
        "action": result.action,
        "path": target_path,
        "source": source_file,
    }
    should_write = result.action != "up to date" or force
    if should_write and not dry_run:
        _write_text(target_path, result.text)
    return record


def deploy_all(
    source_root: str,
    target_path: str,
    *,
    dry_run: bool = False,
    force: bool = False,
) -> list[dict]:
    """Deploy every ``<source_root>/<name>.yaml`` into ``target_path``."""
    from . import paths

    results: list[dict] = []
    if not paths.is_dir(source_root):
        process.log(f"no modes folder to deploy at {source_root}; skipping", "warn")
        return results

    for entry in sorted(paths.list_dir(source_root, dirs=False), key=lambda e: e.name):
        if not entry.name.lower().endswith((".yaml", ".yml")):
            continue
        results.append(deploy(entry.path, target_path, dry_run=dry_run, force=force))

    if results:
        verb = "would deploy" if dry_run else "deployed"
        process.log(f"modes {verb} -> {target_path}")
    return results
