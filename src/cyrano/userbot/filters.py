"""Pure decisions about which Telegram events are real private messages from real users."""

from __future__ import annotations

from enum import StrEnum

from cyrano.userbot.protocols import IncomingEvent, SenderLike

TELEGRAM_SERVICE_ACCOUNT_ID = 777000


class Rejection(StrEnum):
    OUTGOING = "outgoing"
    GROUP = "group"
    CHANNEL = "channel"
    NOT_PRIVATE = "not_private"
    NO_SENDER = "no_sender"
    SERVICE_ACCOUNT = "service_account"
    SELF = "self"
    BOT = "bot"
    DELETED = "deleted"
    NOT_OUTGOING = "not_outgoing"
    NO_PEER = "no_peer"


def event_rejection(event: IncomingEvent) -> Rejection | None:
    if event.out:
        return Rejection.OUTGOING
    if event.is_group:
        return Rejection.GROUP
    if event.is_channel:
        return Rejection.CHANNEL
    if not event.is_private:
        return Rejection.NOT_PRIVATE
    if event.sender_id == TELEGRAM_SERVICE_ACCOUNT_ID:
        return Rejection.SERVICE_ACCOUNT
    return None


def sender_rejection(sender: SenderLike) -> Rejection | None:
    if sender.id == TELEGRAM_SERVICE_ACCOUNT_ID:
        return Rejection.SERVICE_ACCOUNT
    if sender.is_self:
        return Rejection.SELF
    if sender.bot:
        return Rejection.BOT
    if sender.deleted:
        return Rejection.DELETED
    return None


def outgoing_event_rejection(event: IncomingEvent) -> Rejection | None:
    if not event.out:
        return Rejection.NOT_OUTGOING
    if event.is_group:
        return Rejection.GROUP
    if event.is_channel:
        return Rejection.CHANNEL
    if not event.is_private:
        return Rejection.NOT_PRIVATE
    if event.chat_id is None or event.chat_id <= 0:
        return Rejection.NO_PEER
    if event.chat_id == TELEGRAM_SERVICE_ACCOUNT_ID:
        return Rejection.SERVICE_ACCOUNT
    return None
