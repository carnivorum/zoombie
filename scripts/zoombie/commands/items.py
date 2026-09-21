"""``items``: enumerate the items in a workspace, compactly.

This is the *only* sanctioned way to ask "what summaries exist here?". The point is
token economy: an agent that lists directories and opens each ``summary.md`` to
learn what it holds spends thousands of tokens to find out that three items exist.
One call returns the same facts -- number, date, title, whether a summary,
transcript, source or images are present, and what number the next item takes --
in a single JSON line.

It is also the resolution of a contradiction in the previous inputs: the
``library`` module documented that a skill calls :func:`scan_library` directly,
while the summarize skill forbade hand-assembling a Python command. Now there is a
command to call, so neither text has to be wrong.

**Read-only, always.** Nothing here writes, creates or renames: the naming verdict
is a *measurement* (:mod:`zoombie.item.registry`) and the proposal is a
*recommendation*. Choosing a name is the agent's and the user's job, and this
command exists so that choice is made against facts rather than a guess.
"""

from __future__ import annotations

from ..cli import Outcome
from ..item import paths as item_paths, registry, scan
from ..lib import paths, process
from ..lib.errors import ZoombieError

__all__ = ["run", "proposed_names"]


def _proposal_titles(explicit: str | None, verdict: registry.Verdict) -> list[str]:
    """Titles to render proposals from: the caller's, else nothing."""
    if explicit:
        return [part.strip() for part in explicit.split("|") if part.strip()]
    return []


def proposed_names(
    root: str,
    *,
    title: str | None,
    date: str | None,
    verdict: registry.Verdict,
) -> list[str]:
    """2-4 concrete folder-name options for a new item in ``root``.

    Rendered in the workspace's OWN convention where one was measured, and in the
    recommended default otherwise. A name already taken is dropped rather than
    offered, because the offer is supposed to be executable.
    """
    titles = _proposal_titles(title, verdict)
    if not titles:
        return []

    existing = {name.lower() for name in registry.child_names(root)}
    options: list[str] = []

    def _add(name: str) -> None:
        if name and name.lower() not in existing and name not in options:
            options.append(name)

    for candidate in titles:
        if verdict.convention_id is None:
            # No convention and no siblings: recommend the default, plus the two
            # shapes a user is most likely to want instead.
            _add(registry.render("date-title", title=candidate, date=date))
            _add(registry.render("num-date-title", title=candidate, date=date,
                                 number=verdict.next_number))
            _add(registry.render("title-only", title=candidate))
        else:
            _add(registry.propose(candidate, verdict=verdict, date=date))

    return options[:4]


def resolve_depth(args) -> int:
    """How deep to scan, from ``-Depth`` and its ``-Recurse`` alias.

    The default is ONE level: a library is a folder of items, and looking deeper
    than the caller asked would silently pull in nested collections and change the
    count, the naming verdict and ``nextNumber`` along with it.

    An explicit ``-Depth`` wins over ``-Recurse``, which exists only as a readable
    alias for the common "two levels" case. ``0`` and negatives are handed to
    :func:`zoombie.item.scan.scan`, which clamps them -- a flag must not be able to
    ask for a walk of unbounded depth.
    """
    if args.depth is not None:
        return args.depth
    return 2 if args.recurse else 1


def run(args) -> Outcome:
    """Entry point behind ``zoombie items``."""
    root = paths.absolute(args.root) if args.root else paths.absolute(".")
    if not paths.is_dir(root):
        raise ZoombieError(f"Directory not found: {root}")
    paths.assert_fits(root, "The workspace root")

    result = scan.scan(root, resolve_depth(args))

    proposals = proposed_names(
        root,
        title=getattr(args, "title", None),
        date=getattr(args, "date", None),
        verdict=registry.Verdict(
            convention_id=result.naming.get("convention"),
            confidence=result.naming.get("confidence") or "none",
            samples=result.naming.get("samples") or 0,
            total=result.naming.get("total") or 0,
            next_number=result.naming.get("nextNumber"),
            next_letter=result.naming.get("nextLetter"),
        ),
    )

    if not args.json:
        scan.log_scan(result)
        process.log(
            f"items: {len(result.items)} item(s), {len(result.skipped)} skipped "
            f"under {root}"
        )
        process.log(
            f"naming: {result.naming.get('description')} "
            f"(confidence={result.naming.get('confidence')}, "
            f"samples={result.naming.get('samples')}/{result.naming.get('total')})"
        )
        if result.naming.get("nextNumber") is not None:
            process.log(f"next number: {result.naming['nextNumber']}")
        for name in proposals:
            process.log(f"proposed name: {name}")

    data = result.to_dict()
    data["proposals"] = proposals
    return Outcome(ok=True, data=data)
