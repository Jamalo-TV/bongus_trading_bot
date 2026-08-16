"""Reject pull requests that do not declare one safety-program classification."""

from __future__ import annotations

import os
import re
import sys


CLASSIFICATIONS = ("SAFETY", "PNL", "RESEARCH", "OPERATIONS")
_CHECKED_CLASSIFICATION = re.compile(
    r"^\s*-\s*\[[xX]\]\s*`?(SAFETY|PNL|RESEARCH|OPERATIONS)`?\s*$",
    re.MULTILINE,
)


def checked_classifications(body: str) -> tuple[str, ...]:
    """Return checked classification rows without inferring from prose."""

    return tuple(match.group(1).upper() for match in _CHECKED_CLASSIFICATION.finditer(body))


def validate_classification(body: str) -> str:
    """Return the sole classification or raise a stable validation error."""

    checked = checked_classifications(body)
    if len(checked) != 1:
        rendered = ", ".join(checked) if checked else "none"
        raise ValueError(
            "pull request must check exactly one ALPHA_FREEZE classification "
            f"({', '.join(CLASSIFICATIONS)}); checked: {rendered}"
        )
    return checked[0]


def main() -> int:
    body = os.environ.get("BONGUS_PR_BODY", "")
    try:
        classification = validate_classification(body)
    except ValueError as exc:
        print(str(exc), file=sys.stderr)
        return 2
    print(f"change classification: {classification}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
