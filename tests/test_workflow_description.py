from pathlib import Path

import pytest

from forge.domain import workflow_description
from forge.domain.workflow_description import description_of
from tests.conftest import sibling


@pytest.mark.parametrize(
    ("readme", "expected"),
    [
        (
            "# Title\n\nA **useful** [workflow](https://example.test).\n\nSecond paragraph.",
            "A useful workflow.",
        ),
        ("# Title\n\n", ""),
        ("Title\n=====\n\nThe first paragraph.", "The first paragraph."),
        ("Use `my_func` and the value input_file.", "Use my_func and the value input_file."),
        ("Keep 2*3 and ~~old~~ **new** _words_.", "Keep 2*3 and old new words."),
        ("`Code` and _words_\non two lines.", "Code and words on two lines."),
    ],
)
def test_readme_description(readme: str, expected: str) -> None:
    assert description_of(readme) == expected


def test_description_is_cut_at_a_word_boundary() -> None:
    found = description_of("word " * 600)
    assert len(found) <= 2048
    assert found.endswith("word")


def test_bootstrap_uses_the_same_description_rule() -> None:
    assert (
        Path(workflow_description.__file__).read_text()
        == sibling("deploy", "bootstrap", "workflow_description.py").read_text()
    )


@pytest.mark.parametrize("marker", ["[", "<", "[x]("])
def test_unclosed_markup_is_plain_text_and_bounded(marker: str) -> None:
    assert len(description_of(marker * 16_000)) <= 2048
