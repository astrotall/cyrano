from __future__ import annotations

from datetime import UTC, datetime, timedelta

from cyrano.domain.early_read import EARLY_READ_MIN_INTERVAL_SECONDS, EarlyReadRequest
from cyrano.services.early_read import EarlyReadQueue

T0 = datetime(2026, 11, 4, 12, 0, tzinfo=UTC)
FIRST = 11
SECOND = 12
THIRD = 13
INTERVAL = timedelta(seconds=EARLY_READ_MIN_INTERVAL_SECONDS)


def _at(seconds: float) -> datetime:
    return T0 + timedelta(seconds=seconds)


def test_the_minimum_interval_is_five_seconds() -> None:
    assert EARLY_READ_MIN_INTERVAL_SECONDS == 5.0


def test_a_registered_request_is_pending_until_it_is_due() -> None:
    queue = EarlyReadQueue()

    assert queue.register(FIRST, _at(2)) is True

    assert queue.pending() == [EarlyReadRequest(FIRST, _at(2))]
    assert queue.take_due(_at(1.9)) == []
    assert queue.pending() == [EarlyReadRequest(FIRST, _at(2))]


def test_a_due_request_is_returned_once_and_removed() -> None:
    queue = EarlyReadQueue()
    queue.register(FIRST, _at(2))

    assert queue.take_due(_at(2)) == [EarlyReadRequest(FIRST, _at(2))]

    assert queue.take_due(_at(60)) == []
    assert queue.pending() == []


def test_a_second_request_for_a_contact_is_ignored_and_does_not_move_the_first() -> None:
    queue = EarlyReadQueue()
    assert queue.register(FIRST, _at(1)) is True

    assert queue.register(FIRST, _at(9)) is False
    assert queue.register(FIRST, _at(0)) is False

    assert queue.pending() == [EarlyReadRequest(FIRST, _at(1))]
    assert queue.take_due(_at(1)) == [EarlyReadRequest(FIRST, _at(1))]


def test_a_contact_can_be_requested_again_once_its_request_was_taken() -> None:
    queue = EarlyReadQueue()
    queue.register(FIRST, _at(0))
    queue.take_due(_at(0))

    assert queue.register(FIRST, _at(20)) is True


def test_due_requests_come_back_in_due_order() -> None:
    queue = EarlyReadQueue()
    queue.register(THIRD, _at(3))
    queue.register(FIRST, _at(5))
    queue.register(SECOND, _at(1))

    taken = queue.take_due(_at(10))

    assert [request.contact_id for request in taken] == [SECOND, THIRD, FIRST]


def test_requests_with_the_same_due_time_come_back_in_contact_order() -> None:
    queue = EarlyReadQueue()
    queue.register(SECOND, _at(1))
    queue.register(FIRST, _at(1))

    assert [request.contact_id for request in queue.take_due(_at(1))] == [FIRST, SECOND]


def test_contacts_do_not_affect_each_other() -> None:
    queue = EarlyReadQueue()
    queue.register(FIRST, _at(1))
    queue.register(SECOND, _at(8))

    assert [request.contact_id for request in queue.take_due(_at(1))] == [FIRST]

    queue.record_executed(FIRST, _at(1))
    assert queue.register(THIRD, _at(2)) is True
    assert [request.contact_id for request in queue.take_due(_at(8))] == [THIRD, SECOND]


def test_a_request_right_after_an_executed_one_is_deferred_by_the_interval() -> None:
    queue = EarlyReadQueue()
    queue.record_executed(FIRST, _at(0))

    assert queue.register(FIRST, _at(1)) is True

    assert queue.pending() == [EarlyReadRequest(FIRST, _at(0) + INTERVAL)]
    assert queue.take_due(_at(4.9)) == []
    assert queue.take_due(_at(5)) == [EarlyReadRequest(FIRST, _at(5))]


def test_a_request_that_became_due_too_early_is_deferred_when_it_is_taken() -> None:
    queue = EarlyReadQueue()
    queue.register(FIRST, _at(1))
    queue.record_executed(FIRST, _at(0.5))

    assert queue.take_due(_at(2)) == []
    assert queue.pending() == [EarlyReadRequest(FIRST, _at(0.5) + INTERVAL)]
    assert queue.take_due(_at(5.5)) == [EarlyReadRequest(FIRST, _at(5.5))]


def test_the_interval_does_not_delay_a_request_that_is_far_enough_away() -> None:
    queue = EarlyReadQueue()
    queue.record_executed(FIRST, _at(0))

    queue.register(FIRST, _at(5))

    assert queue.pending() == [EarlyReadRequest(FIRST, _at(5))]


def test_the_interval_of_one_contact_does_not_delay_another() -> None:
    queue = EarlyReadQueue()
    queue.record_executed(FIRST, _at(0))

    queue.register(SECOND, _at(1))

    assert queue.pending() == [EarlyReadRequest(SECOND, _at(1))]


def test_the_memory_of_an_executed_read_fades_after_the_interval() -> None:
    queue = EarlyReadQueue()
    queue.record_executed(FIRST, _at(0))
    queue.take_due(_at(6))

    queue.register(FIRST, _at(6))

    assert queue.pending() == [EarlyReadRequest(FIRST, _at(6))]


def test_the_interval_can_be_set() -> None:
    queue = EarlyReadQueue(min_interval_seconds=2)
    queue.record_executed(FIRST, _at(0))

    queue.register(FIRST, _at(0))

    assert queue.pending() == [EarlyReadRequest(FIRST, _at(2))]


def test_a_new_queue_knows_nothing_of_an_old_one() -> None:
    old = EarlyReadQueue()
    old.register(FIRST, _at(1))
    old.record_executed(SECOND, _at(0))

    fresh = EarlyReadQueue()

    assert fresh.pending() == []
    assert fresh.take_due(_at(100)) == []
    fresh.register(SECOND, _at(1))
    assert fresh.pending() == [EarlyReadRequest(SECOND, _at(1))]
