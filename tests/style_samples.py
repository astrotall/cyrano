from __future__ import annotations

import json
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parents[1]
SAMPLES_DIR = REPO_ROOT / "examples"
SAMPLE_PERSONA = SAMPLES_DIR / "persona.sample.md"
SAMPLE_EXAMPLES = SAMPLES_DIR / "style-examples.sample.json"
MIN_TEXT_LENGTH = 12


def sample_texts() -> list[str]:
    document = json.loads(SAMPLE_EXAMPLES.read_text(encoding="utf-8"))
    turns = [turn["text"] for dialogue in document["dialogues"] for turn in dialogue["turns"]]
    persona_lines = [
        line.strip()
        for line in SAMPLE_PERSONA.read_text(encoding="utf-8").splitlines()
        if len(line.strip()) >= MIN_TEXT_LENGTH
    ]
    return [*turns, *persona_lines]
