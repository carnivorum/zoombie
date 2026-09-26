"""The naming conventions, as MEASURED from a workspace rather than imposed.

The toolchain used to declare one convention -- ``<number> - <DD.MM.YYYY> - <title>``
-- parse it with one regex, and treat any folder that did not match as not-an-item.
That made the name load-bearing: an agent with no library to read invented a
placeholder number, and a user whose folders are ``a <title>``, ``b <title>``,
``c <title>`` got an empty index.

This module inverts that. A convention is a *hypothesis about a directory*, tested
against what is actually there:

* :func:`measure` reads the sibling folders and reports which convention the
  directory already follows, how many samples support it, and what the next
  number or letter is. It writes nothing and decides nothing.
* :func:`propose` renders one concrete name from that verdict.
* The agent applies judgement and the user vetoes; the toolchain only supplies the
  measurement, so the offer is auditable instead of invisible.

**No convention is inferred from a single sample.** Two folders sharing a hyphen is
a coincidence; the thresholds are explicit arguments (:func:`measure`), not magic.

``date`` is exchanged as ISO (``2020-05-06``) and rendered as ``DD.MM.YYYY``. The
divide matters: the stored form sorts correctly, the rendered form is the Russian
convention this project's own reference library uses.
"""

from __future__ import annotations

import os
import re
from dataclasses import dataclass, field
from typing import Sequence

from ..lib import paths

__all__ = [
    "LEGACY_FOLDER_RE",
    "Convention",
    "Verdict",
    "CONVENTIONS",
    "CONVENTION_IDS",
    "DEFAULT_CONVENTION_ID",
    "date_display",
    "date_iso",
    "measure",
    "next_number",
    "child_names",
    "propose",
    "render",
    "item_directory",
]

# The historical folder-name pattern ``<number> - <DD.MM.YYYY> - <title>``. Kept
# because the past is full of these names: the scan reads a number out of one when
# measuring the convention, and a sort uses it as the reading-order key.
# Recognition itself never consults it -- see :func:`zoombie.item.paths.is_item`.
LEGACY_FOLDER_RE = re.compile(r"^(\d+)\s*-\s*(\d{2}\.\d{2}\.\d{4})\s*-\s*(.*)$")

# Separators a human actually types between the parts. The old pattern accepted
# only the ASCII hyphen with optional spaces, so a name "tidied" to an em dash --
# the dash this project's own prose uses in ``HH:MM:SS — Title`` -- silently
# stopped being an item.
_SEP = r"\s*[-—–]\s*"

_DISPLAY_DATE = r"\d{2}\.\d{2}\.\d{4}"
_ISO_DATE = r"\d{4}-\d{2}-\d{2}"


@dataclass(frozen=True)
class Convention:
    """One naming hypothesis, with the fields it is able to extract."""

    id: str
    description: str
    regex: re.Pattern[str]
    fields: tuple[str, ...]
    #: Renders a name from the fields this convention carries.
    template: str

    def parse(self, name: str) -> dict | None:
        """The fields ``name`` yields under this convention, or ``None``."""
        match = self.regex.match(name)
        return match.groupdict() if match else None

    def render_name(self, *, title: str, date: str | None = None,
                    number: int | None = None, letter: str | None = None) -> str:
        """A concrete folder name from this convention's fields."""
        values = {
            "title": title,
            "date": date_display(date) if date else "",
            "number": "" if number is None else str(number),
            "letter": letter or "",
        }
        name = self.template.format(**values)
        # Collapse the artefacts of an unfilled placeholder: doubled separators, a
        # trailing/leading one, doubled spaces.
        name = re.sub(r"\s+", " ", name).strip()
        name = re.sub(rf"^({_SEP}|{_SEP}$)", "", name).strip()
        return name


# Ordered by strength: an unambiguous convention is tested before a looser one, so
# ``12 - 06.05.2020 - Заметки`` is read as num-date-title rather than num-title.
CONVENTIONS: tuple[Convention, ...] = (
    Convention(
        id="num-date-title",
        description="<number> - <DD.MM.YYYY> - <title>",
        regex=re.compile(
            rf"^(?P<number>\d+){_SEP}(?P<date>{_DISPLAY_DATE}){_SEP}(?P<title>.*)$"
        ),
        fields=("number", "date", "title"),
        template="{number} - {date} - {title}",
    ),
    Convention(
        id="date-title",
        description="<DD.MM.YYYY> - <title>",
        regex=re.compile(rf"^(?P<date>{_DISPLAY_DATE}){_SEP}(?P<title>.+)$"),
        fields=("date", "title"),
        template="{date} - {title}",
    ),
    Convention(
        id="iso-date-title",
        description="<YYYY-MM-DD> - <title>",
        regex=re.compile(rf"^(?P<date>{_ISO_DATE}){_SEP}(?P<title>.+)$"),
        fields=("date", "title"),
        template="{date} - {title}",
    ),
    Convention(
        id="num-title",
        description="<number> - <title>",
        regex=re.compile(rf"^(?P<number>\d+){_SEP}(?P<title>.+)$"),
        fields=("number", "title"),
        template="{number} - {title}",
    ),
    Convention(
        id="letter-title",
        description="<letter> - <title>",
        # The letter must be a SEPARATE TOKEN, so whitespace between it and the
        # title is REQUIRED. Without that, every ordinary word matched -- "Первый"
        # parsed as letter "П" plus title "ервый", and a workspace of plainly-named
        # folders was misread as a letter convention.
        regex=re.compile(r"^(?P<letter>[A-Za-zА-Яа-я])[.)]?[ \t]+(?P<title>.+)$"),
        fields=("letter", "title"),
        template="{letter}. {title}",
    ),
    # Last, and deliberately the loosest: a workspace of plainly-named folders is
    # itself a convention, and it is the one a user is most likely to have.
    Convention(
        id="title-only",
        description="<title>",
        regex=re.compile(r"^(?P<title>.+)$"),
        fields=("title",),
        template="{title}",
    ),
)

CONVENTION_IDS: tuple[str, ...] = tuple(item.id for item in CONVENTIONS)

# What to recommend when the directory holds no evidence at all.
DEFAULT_CONVENTION_ID = "date-title"


def _convention(convention_id: str) -> Convention:
    for candidate in CONVENTIONS:
        if candidate.id == convention_id:
            return candidate
    raise KeyError(convention_id)


# --------------------------------------------------------------------------- #
# dates
# --------------------------------------------------------------------------- #

def date_display(iso: str | None) -> str:
    """``2020-05-06`` -> ``06.05.2020``; a non-ISO value is returned unchanged."""
    if not iso:
        return ""
    match = re.match(rf"^({_ISO_DATE})", iso.strip())
    if match is None:
        return iso.strip()
    year, month, day = match.group(1).split("-")
    return f"{day}.{month}.{year}"


def date_iso(display: str | None) -> str | None:
    """``06.05.2020`` -> ``2020-05-06``; ``None`` when the shape is not that.

    Returns ``None`` rather than raising, so a caller deciding "is this a date?"
    is a truth test. An already-ISO value is passed through, which makes
    :func:`measure` tolerant of a workspace that mixed the two styles.
    """
    if not display:
        return None
    text = display.strip()
    match = re.match(rf"^({_ISO_DATE})$", text)
    if match is not None:
        return text
    match = re.match(r"^(\d{2})\.(\d{2})\.(\d{4})$", text)
    if match is None:
        return None
    day, month, year = match.groups()
    return f"{year}-{month}-{day}"


# --------------------------------------------------------------------------- #
# measuring a directory
# --------------------------------------------------------------------------- #

def child_names(root: str) -> list[str]:
    """Names of every immediate subdirectory of ``root``, excluding our own data.

    ``.data`` is excluded because it is the item's internals, not a sibling item --
    a detector that counted it would find a "convention" in every item folder.
    Hidden entries generally are skipped: a user's ``.git`` is not a naming sample.
    """
    if not paths.is_dir(root):
        return []
    names: list[str] = []
    for entry in paths.list_dir(root, dirs=True):
        if entry.name.startswith("."):
            continue
        names.append(entry.name)
    return names


# --------------------------------------------------------------------------- #
# the naming verdict
# --------------------------------------------------------------------------- #


@dataclass
class Verdict:
    """What a directory's naming says about itself. Report-only."""

    convention_id: str | None
    confidence: str  # "strong" | "weak" | "none"
    samples: int
    total: int
    matched: dict[str, int] = field(default_factory=dict)
    next_number: int | None = None
    next_letter: str | None = None

    @property
    def convention(self) -> Convention | None:
        return _convention(self.convention_id) if self.convention_id else None

    @property
    def description(self) -> str:
        """The convention's shape, for a proposal a human reads."""
        if self.convention_id is None:
            return f"(no convention detected; recommending {DEFAULT_CONVENTION_ID})"
        return _convention(self.convention_id).description

    def to_dict(self) -> dict:
        return {
            "convention": self.convention_id,
            "description": self.description,
            "confidence": self.confidence,
            "samples": self.samples,
            "total": self.total,
            "matched": dict(self.matched),
            "nextNumber": self.next_number,
            "nextLetter": self.next_letter,
        }


def measure(names: Sequence[str], *, min_samples: int = 3, weak_samples: int = 2) -> Verdict:
    """Which convention ``names`` follow, and what the next number is.

    A convention is adopted only with ``min_samples`` agreeing samples; with
    ``weak_samples`` it is reported as weak so a caller can present it and ask;
    below that, no convention is claimed and the caller falls back to
    :data:`DEFAULT_CONVENTION_ID`.

    The thresholds are parameters, not constants, because they are a judgement:
    three agreeing folders is evidence, two is a coincidence worth mentioning, one
    is nothing.
    """
    matched: dict[str, int] = {}
    for convention in CONVENTIONS:
        count = sum(1 for name in names if convention.parse(name) is not None)
        if count:
            matched[convention.id] = count

    total = len(names)
    verdict: Verdict | None = None

    # Strongest evidence first: the highest-priority convention that enough
    # siblings agree on wins, even if a looser one matches more of them -- a
    # workspace of dated folders is a date convention, not a "title-only" one.
    for convention in CONVENTIONS:
        if matched.get(convention.id, 0) >= min_samples:
            verdict = Verdict(
                convention_id=convention.id,
                confidence="strong",
                samples=matched[convention.id],
                total=total,
                matched=matched,
            )
            break

    if verdict is None:
        for convention in CONVENTIONS:
            if matched.get(convention.id, 0) >= weak_samples:
                verdict = Verdict(
                    convention_id=convention.id,
                    confidence="weak",
                    samples=matched[convention.id],
                    total=total,
                    matched=matched,
                )
                break

    if verdict is None:
        # No structured evidence. If there are siblings at all, the honest reading
        # is "plainly named", which IS a convention -- say so weakly rather than
        # falling back to the default and proposing a dated name nobody uses.
        convention_id = "title-only" if total else None
        verdict = Verdict(
            convention_id=convention_id,
            confidence="none",
            samples=0,
            total=total,
            matched=matched,
        )

    verdict.next_number, verdict.next_letter = _successors(names)
    return verdict


def _successors(names: Sequence[str]) -> tuple[int | None, str | None]:
    """``(next number, next letter)`` for ``names``: ``max + 1``, or ``None``.

    The increment is computed, never inferred by an agent. That is the specific
    repair for the incident this redesign started from: a task with no library to
    read emitted a placeholder ``NN`` because nothing in the toolchain could tell
    it what its number was. Now the scan reports ``nextNumber`` as a fact.
    """
    numbers: list[int] = []
    letters: list[str] = []
    for convention in CONVENTIONS:
        for name in names:
            parsed = convention.parse(name)
            if parsed is None:
                continue
            raw_number = parsed.get("number")
            if raw_number and raw_number.isdigit():
                numbers.append(int(raw_number))
            raw_letter = parsed.get("letter")
            if raw_letter and raw_letter.isalpha():
                letters.append(raw_letter)
    # Letters only count when the convention that produced them is the letter one;
    # otherwise an ordinary word's first character would register. The guard is the
    # presence of at least two distinct single letters.
    next_number = max(numbers) + 1 if numbers else None
    distinct = {item.lower() for item in letters}
    next_letter = chr(ord(max(letters)) + 1) if len(distinct) >= 2 and letters else None
    return next_number, next_letter


def next_number(existing: Sequence[int | None]) -> int:
    """The number a new item should take: ``max + 1``, starting at 1."""
    values = [value for value in existing if isinstance(value, int)]
    return max(values) + 1 if values else 1


# --------------------------------------------------------------------------- #
# proposing a name
# --------------------------------------------------------------------------- #

def propose(
    title: str,
    *,
    verdict: Verdict | None = None,
    date: str | None = None,
    number: int | None = None,
    letter: str | None = None,
    convention_id: str | None = None,
) -> str:
    """One concrete folder name, following ``verdict``'s convention.

    ``verdict`` supplies the convention and the next number when the caller has
    none; explicit ``convention_id`` / ``number`` / ``letter`` override it, which
    is how a caller answers "use option 2" from a proposal.
    """
    if convention_id is None:
        convention_id = (
            verdict.convention_id
            if verdict is not None and verdict.convention_id
            else DEFAULT_CONVENTION_ID
        )
    if number is None and verdict is not None:
        number = verdict.next_number
    if letter is None and verdict is not None:
        letter = verdict.next_letter
    return render(convention_id, title=title, date=date, number=number, letter=letter)


def render(
    convention_id: str,
    *,
    title: str,
    date: str | None = None,
    number: int | None = None,
    letter: str | None = None,
) -> str:
    """Render a name under an explicit convention id.

    Falls back to the default convention when ``convention_id`` is unknown, so a
    recorded id from an older toolchain degrades to the recommended style instead
    of raising inside a rename.
    """
    try:
        convention = _convention(convention_id)
    except KeyError:
        convention = _convention(DEFAULT_CONVENTION_ID)
    return convention.render_name(title=title, date=date, number=number, letter=letter)


def item_directory(root: str, name: str) -> str:
    """``root/name`` as an absolute path -- the one place a name becomes a path."""
    return paths.absolute(os.path.join(root, name))
