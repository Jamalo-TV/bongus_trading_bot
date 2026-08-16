from __future__ import annotations

import pytest

from scripts.verify_pr_classification import (
    checked_classifications,
    validate_classification,
)


@pytest.mark.parametrize("classification", ("SAFETY", "PNL", "RESEARCH", "OPERATIONS"))
def test_exactly_one_template_classification_is_accepted(classification: str) -> None:
    body = "\n".join(
        f"- [{'x' if candidate == classification else ' '}] `{candidate}`"
        for candidate in ("SAFETY", "PNL", "RESEARCH", "OPERATIONS")
    )

    assert validate_classification(body) == classification


@pytest.mark.parametrize(
    "body",
    (
        "",
        "This is a SAFETY change.",
        "- [ ] `SAFETY`\n- [ ] `PNL`\n- [ ] `RESEARCH`\n- [ ] `OPERATIONS`",
        "- [x] `SAFETY`\n- [X] `PNL`",
        "- [x] `SAFETY`\n- [x] `SAFETY`",
    ),
)
def test_missing_or_multiple_classifications_fail_closed(body: str) -> None:
    with pytest.raises(ValueError, match="exactly one"):
        validate_classification(body)


def test_parser_ignores_unchecked_boxes_and_prose_mentions() -> None:
    body = "SAFETY PNL RESEARCH OPERATIONS\n- [ ] `PNL`\n- [x] `OPERATIONS`"

    assert checked_classifications(body) == ("OPERATIONS",)
