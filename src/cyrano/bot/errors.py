from __future__ import annotations


class ControlBotError(Exception):
    pass


class BotTokenRejected(ControlBotError):
    def __init__(self) -> None:
        super().__init__("Telegram rejected BOT_TOKEN. Check the token of the control bot.")


class PollingFailed(ControlBotError):
    def __init__(self, reason: str) -> None:
        super().__init__(f"The control bot stopped polling Telegram ({reason}).")
