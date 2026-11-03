from __future__ import annotations

import re

MIN_MARKER_DIGITS = 12
MIN_ASSERTED_MARKER_DIGITS = 10

FAKE_TELEGRAM_ID = 987654321012
FAKE_OWNER_TELEGRAM_ID = 123456789098
FAKE_STRANGER_TELEGRAM_ID = 555000111222
FAKE_API_ID = 246801357913
FAKE_PHONE = "+15550001234567"
FAKE_LOGIN_CODE = "135792468013"
FAKE_INTERNAL_ID = 909090909090
FAKE_OTHER_INTERNAL_ID = 818181818181

NUMERIC_MARKER = re.compile(r"\+?\d+")


def assert_absent(marker: str | int, haystack: str) -> None:
    text = str(marker)
    if NUMERIC_MARKER.fullmatch(text) and len(text.lstrip("+")) < MIN_ASSERTED_MARKER_DIGITS:
        raise AssertionError(
            f"numeric marker is shorter than {MIN_ASSERTED_MARKER_DIGITS} digits "
            "and can collide with timestamps, thread ids or process ids"
        )
    if text in haystack:
        raise AssertionError("marker found in the captured output")
