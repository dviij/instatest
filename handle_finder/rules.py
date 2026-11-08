"""Instagram username rules.

Every candidate passes through these checks *before* any network call is made, so
the tool never wastes a request (or risks a block) on a name Instagram would reject
outright.

Rules enforced:

* allowed characters: ``a-z``, ``0-9``, period (``.``) and underscore (``_``)
* case-insensitive: names are normalised to lowercase
* no two periods in a row, and no leading or trailing period
* at most 30 characters (Instagram's maximum)
"""

from __future__ import annotations

import string

ALLOWED_CHARS: frozenset[str] = frozenset(string.ascii_lowercase + string.digits + "._")
MAX_LENGTH = 30


def normalize(raw: str) -> str:
    """Return ``raw`` in the canonical stored form.

    Strips surrounding whitespace and one leading ``@`` (people often paste
    handles that way), then lowercases the result. It does *not* validate.

    >>> normalize("  @Some.Name ")
    'some.name'
    """
    name = raw.strip()
    if name.startswith("@"):
        name = name[1:]
    return name.lower()


def violations(name: str) -> list[str]:
    """List every rule ``name`` breaks, in plain English.

    ``name`` should already be normalised; uppercase letters are reported as
    disallowed characters so un-normalised input is never silently accepted.
    An empty list means the name is valid.
    """
    if not name:
        return ["is empty"]

    problems: list[str] = []
    if len(name) > MAX_LENGTH:
        problems.append(f"is longer than {MAX_LENGTH} characters")

    bad_chars = sorted({c for c in name if c not in ALLOWED_CHARS})
    if bad_chars:
        shown = " ".join(repr(c) for c in bad_chars)
        problems.append(f"contains characters Instagram doesn't allow: {shown}")

    if name.startswith("."):
        problems.append("starts with a period")
    if name.endswith("."):
        problems.append("ends with a period")
    if ".." in name:
        problems.append("has two periods in a row")
    return problems


def is_valid(name: str) -> bool:
    """Return ``True`` if the (normalised) ``name`` satisfies every rule."""
    return not violations(name)


def clean(raw: str) -> str | None:
    """Normalise ``raw`` and return it, or ``None`` if it breaks a rule."""
    name = normalize(raw)
    return name if is_valid(name) else None
