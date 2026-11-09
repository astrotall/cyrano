from __future__ import annotations

from cyrano.domain.enums import (
    AutopilotPauseReason,
    AutopilotStopReason,
    ContactGender,
    ContactStatus,
    GenderSource,
    MediaKind,
    TriggerClass,
)

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

LABEL_GENDER = "Пол"
GENDER_LINE = "{label}: {value} ({source})"
GENDER_UNKNOWN_LINE = f"{LABEL_GENDER}: не определён"
GENDER_VALUE_NAMES = {ContactGender.FEMALE: "девушка", ContactGender.MALE: "парень"}
GENDER_SOURCE_NAMES = {
    GenderSource.OWNER: "выбран вами",
    GenderSource.SUMMARY: "из переписки",
    GenderSource.NAME: "по имени",
    GenderSource.DEFAULT: "по умолчанию",
}
LABEL_STATUS = "Статус"
STATUS_LINE = "{label}: {status}"
STATUS_NAMES = {
    ContactStatus.NEW: "новый",
    ContactStatus.PENDING: "ждёт решения",
    ContactStatus.ACTIVE: "отвечаем",
    ContactStatus.PAUSED: "на паузе",
    ContactStatus.IGNORED: "игнорируем",
}
GENDER_FEMALE_BUTTON = "Девушка"
GENDER_MALE_BUTTON = "Парень"
GENDER_CHECK_MARK = "✓ "

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
ALERT_GENDER_REQUIRED = "Сначала выберите пол контакта: «Девушка» или «Парень»."
ALERT_FAILED = "Не получилось, попробуйте ещё раз."

MANUAL_PAUSE_NOTICE = (
    "Вы написали контакту #{contact_id} сами: автоответы для него приостановлены, "
    "запланированные ответы отменены. Когда будете готовы, возобновите автоответы кнопкой."
)
RESUME_BUTTON = "Возобновить автоответы"
RESUMED_TEXT = "Автоответы для контакта #{contact_id} возобновлены."
ALERT_RESUME_REFUSED = "Автоответы для этого контакта сейчас возобновить нельзя."

HAND_OVER_TITLE = "Нужно ваше решение: контакт #{contact_id}"
HAND_OVER_NAME = "Имя: {name}"
HAND_OVER_PAUSED = (
    "Разговор передан вам: автоответы для контакта приостановлены, запланированные ответы "
    "отменены. Контакту ничего не отправлено."
)
HAND_OVER_REASON = "Причина: {classes}."
HAND_OVER_SOURCE_TEXT = "Сработало по тексту сообщения контакта."
HAND_OVER_SOURCE_MODEL = "Сработало при подготовке ответа моделью."
HAND_OVER_NO_CLASSES = "ассистент не может ответить сам, нужно ваше решение"
HAND_OVER_MEDIA_TITLE = "Контакт #{contact_id} прислал {what}"
HAND_OVER_MEDIA_PAUSED = (
    "Автоответы для контакта приостановлены, запланированные ответы отменены. "
    "Контакту ничего не отправлено. Ассистент не видит вложение: посмотрите его в чате "
    "второго аккаунта."
)
HAND_OVER_MEDIA_KINDS = {
    MediaKind.VOICE: "голосовое сообщение",
    MediaKind.VIDEO_NOTE: "кружок",
    MediaKind.PHOTO: "фото",
    MediaKind.VIDEO: "видео",
    MediaKind.STICKER: "стикер",
    MediaKind.OTHER: "вложение",
}
HAND_OVER_LINES_TITLE = "Последние сообщения:"
HAND_OVER_NO_LINES = "Сообщения недоступны."
HAND_OVER_LINE = "{number}. {speaker}: {text}"
HAND_OVER_SPEAKER_CONTACT = "Контакт"
HAND_OVER_SPEAKER_OWNER = "Вы"
HAND_OVER_EMPTY_TEXT = "[без текста]"
HAND_OVER_DECIDE = "Ответьте контакту сами или выберите действие кнопкой."
TRIGGER_CLASS_NAMES = {
    TriggerClass.MEETING: "предложение встретиться",
    TriggerClass.CONTACT_EXCHANGE: "обмен контактами или звонок",
    TriggerClass.MONEY: "деньги",
    TriggerClass.PHOTO: "фото или интимное",
    TriggerClass.BOT_QUESTION: "вопрос, бот ли это",
    TriggerClass.EXES: "бывшие и прошлые отношения",
    TriggerClass.CRISIS: "кризисная тема (самоповреждение, суицид)",
    TriggerClass.MEDIA: "вложение, которое ассистент не видит",
}
TRIGGER_CLASS_SEPARATOR = ", "
KEEP_BUTTON = "Оставить на паузе"
HAND_OVER_IGNORE_BUTTON = "Игнорировать"
KEPT_TEXT = "Контакт #{contact_id} оставлен на паузе."
IGNORED_TEXT = "Контакт #{contact_id} теперь игнорируется: автоответов ему больше не будет."
ALERT_IGNORE_REFUSED = "Этот контакт сейчас нельзя перевести в игнор."

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
    "Telegram просит подождать {seconds} с: вся отправка приостановлена, сообщение #{row_id} "
    "контакту #{contact_id} отложено. Отправка продолжится сама."
)
OUTBOX_FLOOD_WAIT_EARLY_READ = (
    "Telegram просит подождать {seconds} с: вся отправка приостановлена. Отправка продолжится сама."
)
OUTBOX_ACCOUNT_STOPPED = (
    "Вся отправка остановлена: Telegram сообщил о проблеме с аккаунтом ({kind}). "
    "Запланированные сообщения ждут в очереди. Сначала проверьте аккаунт вручную, затем снимите "
    "остановку командой: python -m cyrano outbox resume --yes"
)
OUTBOX_CONTACT_PAUSED = (
    "Контакт #{contact_id} поставлен на паузу: Telegram отказал в отправке ({error}). "
    "Его запланированные сообщения отменены."
)
OUTBOX_LIMIT_NOTE = (
    "Ничего не потеряно: сообщения ждут в очереди и уйдут, когда слот освободится. "
    "Сообщение, которое к тому времени будет ждать дольше 6 часов после запланированного "
    "времени, отменяется как устаревшее. "
    "Счётчики показывает команда: python -m cyrano outbox status"
)
OUTBOX_LIMIT_TIME_FORMAT = "%d.%m %H:%M"
OUTBOX_LIMIT_NUMBERS = (
    "Отправлено за окно: {counted}, лимит: {limit}. Следующий слот освободится "
    "в {free_at} ({timezone})."
)
OUTBOX_STALE_SUMMARY = (
    "Отменено как устаревших сообщений: {count}. Они ждали дольше 6 часов после "
    "запланированного времени и не отправлены. Контакты (до 5): {contact_ids}. "
    "Список: python -m cyrano outbox list --state cancelled"
)
OUTBOX_STOP_REMINDER = (
    "Вся отправка по-прежнему остановлена ({kind}). Сначала проверьте аккаунт вручную, "
    "затем снимите остановку командой: python -m cyrano outbox resume --yes"
)
OUTBOX_LIMIT_GLOBAL_HOUR = (
    "Достигнут общий лимит отправки за час (настройка SEND_LIMIT_HOUR). "
    f"{OUTBOX_LIMIT_NUMBERS} {OUTBOX_LIMIT_NOTE}"
)
OUTBOX_LIMIT_GLOBAL_DAY = (
    "Достигнут общий лимит отправки за сутки (настройка SEND_LIMIT_DAY). "
    f"{OUTBOX_LIMIT_NUMBERS} {OUTBOX_LIMIT_NOTE}"
)
OUTBOX_LIMIT_CONTACT_HOUR = (
    "Достигнут лимит отправки за час для контакта #{contact_id} "
    f"(настройка SEND_LIMIT_CONTACT_HOUR). {OUTBOX_LIMIT_NUMBERS} {OUTBOX_LIMIT_NOTE}"
)
OUTBOX_LIMIT_CONTACT_DAY = (
    "Достигнут лимит отправки за сутки для контакта #{contact_id} "
    f"(настройка SEND_LIMIT_CONTACT_DAY). {OUTBOX_LIMIT_NUMBERS} {OUTBOX_LIMIT_NOTE}"
)

AUTOPILOT_STOPPED = (
    "Автопилот остановлен: {reason} ({error}). Новые ответы не готовятся, контактам ничего "
    "нового не пишется. Устраните причину, затем снимите остановку командой: "
    "python -m cyrano autopilot resume --yes"
)
AUTOPILOT_PAUSED = (
    "Автопилот на паузе {minutes} мин: {reason} ({error}). После паузы он сам ответит на "
    "сообщения, которым не больше 6 часов. Снять паузу раньше: python -m cyrano autopilot resume"
)
AUTOPILOT_STOP_REASONS = {
    AutopilotStopReason.PROVIDER_AUTH: "провайдер LLM не принимает ключ",
    AutopilotStopReason.PROVIDER_REQUEST: (
        "провайдер LLM отклоняет запросы, например закончился баланс"
    ),
    AutopilotStopReason.PERSONA_MISSING: "персона не импортирована",
}
AUTOPILOT_PAUSE_REASONS = {
    AutopilotPauseReason.PROVIDER_UNAVAILABLE: (
        "провайдер LLM недоступен или ответ не удалось получить после нескольких попыток"
    ),
}
