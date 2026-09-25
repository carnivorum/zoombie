"""``data.next``: the recommended next step a result advertises, and its cap.

Every pipeline result carries a recommended next invocation, so a skill reads a
**graph** rather than a procedure it has to remember::

    data.next = {
      command, args,               # the recommended next invocation
      why,                         # plain-language reason
      attach: [...],               # what the agent may read inline, HARD-CAPPED
      budget: { images: n, bytes: m }   # what the agent is about to spend
    }

**The enforcement is the point, not the field (plan §9).** A prose rule --
"attach at most N images" -- is exactly what a tired agent drops, so the cap
lives here in code, and every command that emits ``data.next`` routes it through
:func:`build`. That function does not trust the caller: it re-counts what is
actually there and truncates to the cap itself, reporting the truncation by name.

The two halves are deliberate, and they mirror the JSON transport contract:

* ``attach`` is the **sanctioned inline-read list** -- the paths the agent is
  *meant* to open, capped at :data:`DEFAULT_ATTACH_CAP`.
* ``overAttach`` is the **full list, kept in the result** for a programmatic
  consumer (or a later MCP facade, plan §11) that will shape the payload itself.
  Truncating *this* would hide the work that remains; truncating ``attach`` is
  what makes "read at most N" structural rather than a hope.
* ``truncated`` and ``reason`` name the refusal, so a caller that exceeded the cap
  is told which cap and by how much rather than silently handed a shorter list.

Like the no-overwrite refusals and ``-DryRun``, the cap applies in every mode,
including ``-DryRun``: a dry run is what the agent *plans* to attach from, so it
must already be honest about the cap. ``build`` writes nothing, so it composes
with the "``-DryRun`` writes nothing" rule for free.

Budget size is a work-saving fact, not arithmetic for the agent: ``bytes`` is the
sum of the attachable files that exist on disk. A path that does not exist
contributes nothing -- the number is what is really about to be spent.
"""

from __future__ import annotations

from . import paths

# The hard cap on ``attach``: how many image paths a single result may advertise
# for inline reading. It is a small number on purpose. The 413 root cause (§3) was
# a session that read 8 full-resolution PNGs -- ~6.4 M base64 chars -- and blew a
# 128K context; a cap of 8 is the measured envelope the pipeline can survive, and
# the rest is reached by re-running with a narrower request rather than by reading
# everything at once.
DEFAULT_ATTACH_CAP = 8

__all__ = ["DEFAULT_ATTACH_CAP", "build", "cap_of", "describe"]


def cap_of(args) -> int:
    """The attach cap in force for a parsed Namespace.

    ``-AttachLimit`` is the caller's explicit override (plan §9 keeps the cap in
    code, so raising it is a deliberate, auditable act that shows up in the result's
    ``next.attachCap``); omitting it -- or passing ``0``/a non-number -- means the
    built-in :data:`DEFAULT_ATTACH_CAP`. Read via ``getattr`` because a programmatic
    caller and the existing unit tests build a Namespace without the flag.
    """
    raw = getattr(args, "attach_limit", None)
    if raw is None:
        return DEFAULT_ATTACH_CAP
    try:
        value = int(raw)
    except (TypeError, ValueError):
        return DEFAULT_ATTACH_CAP
    return value if value > 0 else DEFAULT_ATTACH_CAP


def describe(attachable: list[dict] | None) -> dict:
    """Count and size an attach list *without* applying the cap.

    Split out from :func:`build` because a caller sometimes needs the honest total
    (``data.images.visionCount``, ``data.visionPageCount``) even when the attach
    list itself is truncated. ``attachable`` entries are ``{"file", "path",
    "bytes"?}``; ``bytes`` is read from disk when the entry does not carry it, so a
    command that already measured a frame does not pay for a second stat.
    """
    entries = list(attachable or [])
    total_bytes = 0
    for entry in entries:
        if not isinstance(entry, dict):
            continue
        value = entry.get("bytes")
        if value is None:
            path = entry.get("path")
            try:
                value = paths.file_size(path) if path and paths.is_file(path) else 0
            except OSError:
                value = 0
        try:
            total_bytes += int(value or 0)
        except (TypeError, ValueError):
            continue
    return {"images": len(entries), "bytes": total_bytes}


def build(
    command: str | None,
    args: dict | None = None,
    *,
    why: str = "",
    attachable: list[dict] | None = None,
    attach_cap: int = DEFAULT_ATTACH_CAP,
    args_capped: bool = False,
) -> dict:
    """Build a ``data.next`` block, enforcing ``attach_cap``.

    ``attachable`` is the complete list of image paths this result *could* hand
    over, in the order they should be read. ``build`` applies the cap **itself**:
    the first ``attach_cap`` become ``attach`` (sanctioned inline, each carrying
    its own ``bytes``), and everything beyond it survives in ``overAttach`` so it
    is reported rather than lost.

    When the list is longer than ``attach_cap`` the block is marked ``truncated``
    with a named ``reason``: the caller asked for more inline images than the
    transport may carry, and the excess is deferred to a further call. That is a
    *reported refusal*, not a silent shortening -- the failure mode this item
    exists to remove is an agent that never learns it should have stopped.

    ``command`` may be ``None`` for a terminal result (nothing left to do); the
    block is still emitted, so every result has the same shape and an agent never
    has to branch on "is there a next at all".
    """
    capped = select(attachable, attach_cap)
    budget = describe(capped["attach"])
    total = describe(attachable)

    block: dict = {
        "command": command,
        "args": dict(args or {}),
        "why": why,
        "attach": capped["attach"],
        "attachCap": attach_cap,
        "attachCount": len(capped["attach"]),
        "count": total["images"],
        "truncated": capped["truncated"],
        "overAttach": capped["overAttach"],
        "budget": budget,
        "overBudgetReasons": [],
        "writes": False,
    }
    if capped["truncated"]:
        block["overBudgetReasons"].append(
            f"attach list truncated: {total['images']} image(s) available, the "
            f"inline cap is {attach_cap}; {len(capped['overAttach'])} deferred to "
            "data.next.overAttach (re-run with a narrower request to read them)"
        )
    if args_capped:
        block["overBudgetReasons"].append(
            "more fits on disk than one result may attach, so the recommended next "
            "step is to NARROW the request for the deferred paths (for slides, "
            "-Times with their timestamps) rather than raise the cap: the cap is a "
            "transport guarantee and -Force does not bypass it"
        )
    block["reason"] = "; ".join(block["overBudgetReasons"])
    return block


def select(attachable: list[dict] | None, attach_cap: int = DEFAULT_ATTACH_CAP) -> dict:
    """Split an attach list at ``attach_cap``; the pure half of :func:`build`.

    Returns ``{"attach", "overAttach", "truncated", "dropped"}`` where ``dropped``
    is how many entries the cap pushed out of the inline list. Kept separate (and
    public) because the *enforcement* is worth testing without also asserting the
    prose of the block.
    """
    entries = [entry for entry in (attachable or []) if isinstance(entry, dict)]
    cap = max(0, int(attach_cap))
    attach = [dict(entry) for entry in entries[:cap]]
    over = [dict(entry) for entry in entries[cap:]]
    return {
        "attach": attach,
        "overAttach": over,
        "truncated": bool(over),
        "dropped": len(over),
    }
