"""Task environment grammar never admits installer flags or alternate channels."""

import pytest
from pydantic import ValidationError

from forge.domain.environment import Environment
from forge.domain.environment.tools import LANGUAGES, SOURCES, VERSIONS
from forge.domain.yaml_models import problems_of


def refusals(entry: dict[str, object]) -> list[tuple[str, str]]:
    with pytest.raises(ValidationError) as caught:
        Environment.model_validate(entry)
    return [(problem["path"], problem["message"]) for problem in problems_of(caught.value)]


def test_each_language_accepts_one_of_its_versions_and_conda_packages() -> None:
    parsed = Environment.model_validate(
        {
            "c": {"version": "16"},
            "cpp": {"version": "16", "conda": ["libboost-headers", "gtest"]},
            "java": {"version": "25", "packages": ["org.junit.jupiter:junit-jupiter:5.+"]},
            "python": {"version": "3.15", "packages": ["numpy==2.3.*"]},
            "javascript": {"version": "24", "packages": ["jest@30", "@types/node@^24"]},
            "typescript": {"version": "6.0", "packages": ["chai", "JSONStream@1", "@Legacy/Name"]},
            "kotlin": {"packages": ["org.jetbrains.kotlin:kotlin-test-junit5:2.4.20"]},
            "csharp": {"version": "10.0"},
            "rust": {"version": "1.99"},
            "r": {"version": "4.5", "packages": ["testthat", "data.table=1.17"]},
            "ruby": {"version": "4.0"},
        }
    )
    assert set(parsed.requirements()) == set(LANGUAGES)
    assert parsed.requirements()["r"] == {
        "version": "4.5",
        "packages": ["testthat", "data.table=1.17"],
    }


def test_every_language_offers_versions_and_only_four_sources_take_packages() -> None:
    assert set(VERSIONS) == set(LANGUAGES)
    assert set(SOURCES) == {
        "python",
        "r",
        "java",
        "kotlin",
        "javascript",
        "typescript",
    }


@pytest.mark.parametrize(
    ("entry", "found"),
    [
        ({"ipynb": {}}, ("", "Notebooks use the python entry")),
        (
            {"go": {}},
            (
                "go",
                "Unicon has no go toolchain. Name one of c, cpp, java, python, javascript, "
                "typescript, kotlin, csharp, rust, r and ruby.",
            ),
        ),
        (
            {"cpp": {"packages": ["numpy"]}},
            (
                "cpp.packages",
                "C++ takes no `packages` yet; use `conda` for conda-forge packages",
            ),
        ),
        (
            {"rust": {"packages": ["serde"]}},
            (
                "rust.packages",
                "Rust takes no `packages` yet; use `conda` for conda-forge packages",
            ),
        ),
        ({"python": {"version": ">=3.13"}}, ("python.version", "Name one version, such as 3.13")),
        (
            {"python": {"version": "3.13"}},
            ("python.version", "Python offers 3.15. Name one of them, or none for the newest."),
        ),
        (
            {"javascript": {"version": "22"}},
            (
                "javascript.version",
                "JavaScript offers 26 or 24. Name one of them, or none for the newest.",
            ),
        ),
        (
            {"python": {"conda": ["NumPy"], "packages": ["numpy>=2"]}},
            ("python", "numpy is named in both python.packages and python.conda"),
        ),
        (
            {"r": {"conda": ["r-testthat"], "packages": ["testthat"]}},
            ("r", "testthat is named in both r.packages and r.conda"),
        ),
    ],
)
def test_invalid_entries_are_refused_at_their_paths(
    entry: dict[str, object], found: tuple[str, str]
) -> None:
    assert found in refusals(entry)


@pytest.mark.parametrize("entry", ["other::boost", "https://example.com/a", "boost 1.80 build_0"])
def test_a_conda_entry_is_a_name_and_optional_version(entry: str) -> None:
    assert refusals({"cpp": {"conda": [entry]}})[0][0] == "cpp.conda[0]"


@pytest.mark.parametrize(
    ("language", "entry"),
    [
        ("java", "org.junit.jupiter:junit-jupiter"),
        ("java", "org.junit.jupiter:junit-jupiter:latest.release"),
        ("java", "org.junit.jupiter:junit-jupiter:[5.0,6.0)"),
        ("kotlin", "org.junit.jupiter:junit-jupiter:5.14.4:tests"),
        ("kotlin", "https://repo1.maven.org/maven2/a.jar"),
        ("javascript", "jest@git+https://github.com/jestjs/jest.git"),
        ("javascript", "github:jestjs/jest"),
        ("typescript", "alias@npm:chai@6"),
        ("typescript", "./local"),
        ("typescript", ".chai"),
        ("typescript", "@types/.node"),
        ("r", "r-testthat"),
        ("r", "testthat>=3"),
        ("r", "https://cran.r-project.org/a.tar.gz"),
    ],
)
def test_a_package_is_refused_unless_its_source_names_it_plainly(language: str, entry: str) -> None:
    (problem,) = refusals({language: {"packages": [entry]}})
    assert problem[0] == f"{language}.packages[0]"
    assert problem[1].startswith(f"{entry} is not")


def test_c_and_cpp_versions_must_agree(monkeypatch: pytest.MonkeyPatch) -> None:
    import forge.domain.environment as environment

    monkeypatch.setattr(environment, "VERSIONS", {**VERSIONS, "cpp": ("16", "15")})
    assert refusals({"c": {"version": "16"}, "cpp": {"version": "15"}}) == [
        ("cpp.version", "C version 16 and C++ version 15 must agree")
    ]
