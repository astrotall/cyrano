"""Every text the control bot shows to the owner."""

from __future__ import annotations

START_TEXT = (
    "Привет! Я пульт управления Cyrano. Когда в аккаунт придёт новый контакт, "
    "я пришлю его карточку с кнопками."
)

PROFILE_PHOTO_CAPTION = "Фото профиля"
CARD_PHOTO_CAPTION = "Фото из анкеты"

CARD_TITLE = "Новый контакт"
LABEL_NAME = "Имя"
LABEL_USERNAME = "Ник"
LABEL_BIO = "О себе"
LABEL_ANKETA = "Анкета"
LABEL_ANKETA_RAW = "Текст анкеты"
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
