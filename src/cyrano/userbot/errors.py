from __future__ import annotations

LOGIN_HINT = "Run `make login` (or `make login-docker` in Docker) to create the session."


class UserbotError(Exception):
    pass


class SessionMissing(UserbotError):
    def __init__(self) -> None:
        super().__init__(f"The Telegram session file does not exist. {LOGIN_HINT}")


class SessionNotAuthorized(UserbotError):
    def __init__(self) -> None:
        super().__init__(f"The Telegram session is not authorized or was revoked. {LOGIN_HINT}")


class ConnectTimedOut(UserbotError):
    def __init__(self, seconds: float) -> None:
        super().__init__(f"Could not connect to Telegram within {seconds:g} seconds.")


class ConnectFailed(UserbotError):
    def __init__(self, reason: str) -> None:
        super().__init__(f"Could not connect to Telegram ({reason}).")


class Disconnected(UserbotError):
    def __init__(self) -> None:
        super().__init__("The connection to Telegram was lost and could not be restored.")


class LoginFailed(UserbotError):
    def __init__(self, reason: str) -> None:
        super().__init__(f"Login failed ({reason}). Nothing was saved.")
