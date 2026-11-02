"""Task environment grammar never admits installer flags or alternate channels."""

import pytest
from pydantic import ValidationError

from forge.domain.environment import Environment


def test_each_language_accepts_one_version_and_conda_packages() -> None:
    parsed = Environment.model_validate(
        {
            "c": {"version": "14"},
            "cpp": {"version": "14", "conda": ["libboost-headers"]},
            "java": {"version": "21"},
            "python": {"version": "3.13", "packages": ["numpy==2.3.*"]},
        }
    )
    assert set(parsed.model_dump(exclude_none=True)) == {"c", "cpp", "java", "python"}


@pytest.mark.parametrize(
    ("entry", "words"),
    [
        ({"ipynb": {}}, "Notebooks use the python entry"),
        ({"cpp": {"packages": ["numpy"]}}, "Only Python takes"),
        ({"python": {"version": ">=3.13"}}, "Name one version"),
        ({"c": {"version": "14"}, "cpp": {"version": "15"}}, "must agree"),
        ({"python": {"conda": ["NumPy"], "packages": ["numpy>=2"]}}, "both"),
        ({"cpp": {"conda": ["other::boost"]}}, "name and optional version"),
        ({"cpp": {"conda": ["https://example.com/a"]}}, "name and optional version"),
        ({"cpp": {"conda": ["boost 1.80 build_0"]}}, "name and optional version"),
        ({"cpp": {"conda": ["boost>=1.80"]}}, "name and optional version"),
    ],
)
def test_invalid_entries_are_refused(entry: dict[str, object], words: str) -> None:
    with pytest.raises(ValidationError, match=words):
        Environment.model_validate(entry)
