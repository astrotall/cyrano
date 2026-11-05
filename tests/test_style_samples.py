from __future__ import annotations

from cyrano.domain.enums import TurnRole
from cyrano.services.style_files import parse_examples, parse_persona, read_import_file
from tests.style_samples import REPO_ROOT, SAMPLE_EXAMPLES, SAMPLE_PERSONA, SAMPLES_DIR

GITIGNORE = REPO_ROOT / ".gitignore"
DOCKERIGNORE = REPO_ROOT / ".dockerignore"
GUIDE = REPO_ROOT / ".claude" / "docs" / "persona-and-examples.md"
SAMPLE_SCHEDULE = SAMPLES_DIR / "schedule.sample.json"
IGNORED_PATTERNS = ("data/", "persona*.md", "style-examples*.json", "schedule*.json")
SAMPLE_NEGATIONS = (
    "!examples/persona.sample.md",
    "!examples/style-examples.sample.json",
    "!examples/schedule.sample.json",
)
GUIDE_NEGATION = "!.claude/docs/persona-and-examples.md"


def _lines(path: str) -> list[str]:
    return [
        line.strip()
        for line in (REPO_ROOT / path).read_text(encoding="utf-8").splitlines()
        if line.strip() and not line.startswith("#")
    ]


def test_the_samples_live_under_the_examples_directory() -> None:
    assert SAMPLE_PERSONA.parent == SAMPLES_DIR
    assert SAMPLE_EXAMPLES.parent == SAMPLES_DIR
    assert SAMPLE_PERSONA.is_file()
    assert SAMPLE_EXAMPLES.is_file()


def test_the_sample_persona_passes_the_real_validator() -> None:
    persona = parse_persona(read_import_file(SAMPLE_PERSONA))

    assert persona.text


def test_the_sample_examples_pass_the_real_validator_and_cover_the_cases() -> None:
    examples = parse_examples(read_import_file(SAMPLE_EXAMPLES))

    tags = {tag for example in examples for tag in example.tags}
    assert {"default", "flirty"} <= tags
    assert any(not example.tags for example in examples)
    assert any(len(example.turns) > 2 for example in examples)
    assert all(example.turns[-1].role is TurnRole.ME for example in examples)
    assert len({example.content_hash for example in examples}) == len(examples)


def test_the_gitignore_keeps_real_files_out_and_lets_the_samples_in() -> None:
    lines = _lines(".gitignore")

    for pattern in IGNORED_PATTERNS:
        assert pattern in lines
    for negation in (*SAMPLE_NEGATIONS, GUIDE_NEGATION):
        assert negation in lines
        for pattern in ("persona*.md", "style-examples*.json", "schedule*.json"):
            assert lines.index(pattern) < lines.index(negation)


def test_the_negations_name_exactly_the_committed_files() -> None:
    lines = _lines(".gitignore")
    negated = {line.removeprefix("!") for line in lines if line.startswith("!")}

    assert negated == {
        str(SAMPLE_PERSONA.relative_to(REPO_ROOT)),
        str(SAMPLE_EXAMPLES.relative_to(REPO_ROOT)),
        str(SAMPLE_SCHEDULE.relative_to(REPO_ROOT)),
        str(GUIDE.relative_to(REPO_ROOT)),
    }
    assert not any("*" in name for name in negated)


def test_the_guide_exists_because_its_name_matches_an_ignore_pattern() -> None:
    assert GUIDE.is_file()
    assert GUIDE.name.startswith("persona")
    assert GUIDE.suffix == ".md"


def test_the_dockerignore_excludes_the_data_directory() -> None:
    assert "data/" in _lines(".dockerignore")
