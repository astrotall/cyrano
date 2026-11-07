from __future__ import annotations

from cyrano.domain.reply import MAX_REPLY_PARTS
from cyrano.prompts.texts import INSTRUCTIONS, NEEDS_OWNER_SIGNAL


def _bullets() -> list[str]:
    return [line for line in INSTRUCTIONS.splitlines() if line.startswith("- ")]


def test_the_length_rule_asks_for_one_line_per_message_and_names_the_part_limit() -> None:
    [rule] = [line for line in _bullets() if "short reply" in line]

    assert "typically about as long as the owner's replies in the examples" in rule
    assert "each message on its own line" in rule
    assert f"at most {MAX_REPLY_PARTS} lines" in rule
    assert "every line will be sent as a separate message" in rule
    assert "Otherwise write one line." in rule


def test_the_single_message_rule_is_gone() -> None:
    assert "ONE short message" not in INSTRUCTIONS


def test_every_other_rule_is_kept() -> None:
    bullets = _bullets()

    assert len(bullets) == 9
    assert any(f"Reply with exactly {NEEDS_OWNER_SIGNAL} and nothing else" in b for b in bullets)
    assert any("Plain text only" in bullet for bullet in bullets)
    assert any("Never claim to be human and never deny being an AI." in b for b in bullets)
    assert any("conversation DATA" in bullet for bullet in bullets)
    assert any("Answer in the language of the contact's last message" in b for b in bullets)
