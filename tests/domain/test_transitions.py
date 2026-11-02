from __future__ import annotations

import pytest

from cyrano.domain.enums import ContactStatus
from cyrano.domain.errors import ActivationWithoutMode, InvalidStatusTransition
from cyrano.domain.transitions import ALLOWED_TRANSITIONS, transition_needed
from tests.domain.cases import ALLOWED, FORBIDDEN


def test_matrix_covers_every_status_pair() -> None:
    assert len(ALLOWED) + len(FORBIDDEN) + len(ContactStatus) == len(ContactStatus) ** 2
    assert set(ALLOWED_TRANSITIONS) == set(ContactStatus)


def test_rules_match_the_decided_table() -> None:
    declared = {
        (current, requested)
        for current, targets in ALLOWED_TRANSITIONS.items()
        for requested in targets
    }
    assert declared == set(ALLOWED)


@pytest.mark.parametrize(("current", "requested"), ALLOWED)
def test_allowed_transition_needs_a_change(
    current: ContactStatus, requested: ContactStatus
) -> None:
    assert transition_needed(current, requested, has_mode=True) is True


@pytest.mark.parametrize(("current", "requested"), FORBIDDEN)
def test_forbidden_transition_raises(current: ContactStatus, requested: ContactStatus) -> None:
    with pytest.raises(InvalidStatusTransition) as caught:
        transition_needed(current, requested, has_mode=True)
    assert caught.value.current is current
    assert caught.value.requested is requested


@pytest.mark.parametrize("status", list(ContactStatus))
@pytest.mark.parametrize("has_mode", [True, False])
def test_same_status_is_a_no_op(status: ContactStatus, has_mode: bool) -> None:
    assert transition_needed(status, status, has_mode=has_mode) is False


@pytest.mark.parametrize("current", [ContactStatus.PENDING, ContactStatus.PAUSED])
def test_activation_without_mode_is_refused(current: ContactStatus) -> None:
    with pytest.raises(ActivationWithoutMode):
        transition_needed(current, ContactStatus.ACTIVE, has_mode=False)


def test_activation_without_mode_is_an_invalid_transition() -> None:
    assert issubclass(ActivationWithoutMode, InvalidStatusTransition)


def test_forbidden_activation_reports_the_transition_not_the_mode() -> None:
    with pytest.raises(InvalidStatusTransition) as caught:
        transition_needed(ContactStatus.NEW, ContactStatus.ACTIVE, has_mode=False)
    assert not isinstance(caught.value, ActivationWithoutMode)


def test_ignored_is_terminal() -> None:
    assert ALLOWED_TRANSITIONS[ContactStatus.IGNORED] == frozenset()
