"""Every text the control bot shows to the owner."""

from __future__ import annotations

START_TEXT = (
    "Привет! Я пульт управления Cyrano. Когда в аккаунт придёт новый контакт, "
    "я пришлю его карточку с кнопками."
)

PROFILE_PHOTO_CAPTION = "Фото профиля"
CARD_PHOTO_CAPTION = "Фото анкеты"
PROFILE_AND_CARD_PHOTOS_CAPTION = "Фото профиля и анкеты"

CARD_TITLE = "Новый контакт"
LABEL_NAME = "Имя"
LABEL_USERNAME = "Ник"
LABEL_BIO = "О себе"
LABEL_ANKETA = "Анкета"
LABEL_ANKETA_RAW = "Текст анкеты"
CARD_NOT_FOUND = "Анкета не найдена."
LABEL_MESSAGES = "Сообщения"
PROFILE_NOT_COLLECTED = "Профиль собрать не удалось, показано только то, что известно."
NO_MODES_NOTICE = "Нет включённых режимов, ответить нельзя."

REPLY_BUTTON = "Отвечать: {mode}"
IGNORE_BUTTON = "Игнорировать"

DECISION_MARKER = "\n\n➡️ Решение:"
DECISION_REPLY = "отвечаем в режиме «{mode}»"
DECISION_REPLY_NO_MODE = "отвечаем"
DECISION_IGNORE = "игнорируем"
DECISION_STATUS_FALLBACK = "статус контакта: {status}"

ALERT_REFUSED = "Контакт уже не ждёт решения."
ALERT_NOT_FOUND = "Контакт не найден."
ALERT_MODE_UNAVAILABLE = "Этот режим недоступен, выберите другой."
ALERT_FAILED = "Не получилось, попробуйте ещё раз."

OUTBOX_INTERRUPTED = (
    "Отправка сообщения #{row_id} контакту #{contact_id} прервана ({error}). "
    "Повторной отправки не будет. Проверьте чат вручную. Если сообщения там нет, "
    "повторите отправку командой: python -m cyrano outbox retry {row_id} --yes"
)
OUTBOX_FAILED = "Сообщение #{row_id} контакту #{contact_id} не отправлено: {error}."
OUTBOX_UNCERTAIN = (
    "Исход отправки сообщения #{row_id} контакту #{contact_id} неизвестен: оно могло дойти. "
    "Автоматического повтора не будет. Проверьте чат вручную. Если сообщения там нет, "
    "повторите отправку командой: python -m cyrano outbox retry {row_id} --yes"
)
OUTBOX_FLOOD_WAIT = (
    "Telegram просит подождать {seconds} с: сообщение #{row_id} контакту #{contact_id} отложено."
)
