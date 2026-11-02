from __future__ import annotations

from itertools import product

from cyrano.domain.enums import ContactStatus as S

ALLOWED: tuple[tuple[S, S], ...] = (
    (S.NEW, S.PENDING),
    (S.PENDING, S.ACTIVE),
    (S.PENDING, S.IGNORED),
    (S.ACTIVE, S.PAUSED),
    (S.ACTIVE, S.IGNORED),
    (S.PAUSED, S.ACTIVE),
    (S.PAUSED, S.IGNORED),
)
FORBIDDEN: tuple[tuple[S, S], ...] = tuple(
    (current, requested)
    for current, requested in product(S, S)
    if current is not requested and (current, requested) not in ALLOWED
)
