"""
Бот-продавец методички «Установка ИИ-агента» для мессенджера MAX.

Логика (ручное подтверждение оплаты — как в Telegram-версии, но через MAX API):
  1. Покупатель начинает диалог с ботом -> бот шлёт цену, кошелёк и просит чек
     (скриншот).
  2. Покупатель присылает фото-чек -> бот пересылает чек админу (в MAX диалог
     с админом) с кнопками «Выдать» / «Отклонить».
  3. Админ жмёт «Выдать» -> бот через POST /uploads загружает PDF и шлёт его
     покупателю через POST /messages (файл до 4 ГБ, PDF поддерживается).

MAX API (официальная документация dev.max.ru):
  - Запросы к https://platform-api2.max.ru, Authorization: <token>
  - Обновления приходят ТОЛЬКО через Webhook (MAX шлёт POST на ваш HTTPS URL).
    GET /updates (Long Polling) существует, но официально не для production.
  - Кнопки: attachments type=inline_keyboard; нажатие -> событие message_callback.
  - Файлы: POST /uploads?type=file -> токен -> в POST /messages attachments.payload.token
  - Лимит: не более 2 сообщений в секунду в один диалог.

ВАЖНО: это каркас по официальной документации. Пока реального токена бота нет
(нужен верифицированный профиль самозанятого/ИП на business.max.ru), пустые
точки, которые проверятся на живом токене, помечены # TODO(probe).

Все секреты (MAX_ACCESS_TOKEN, ADMIN_USER_ID, WALLET, PRICE, PDF_PATH) — в .env.
"""

import os
import json
import logging
import threading
import asyncio
import mimetypes
from pathlib import Path

import httpx
from dotenv import load_dotenv

# Дополнительные зависимости: fastapi, uvicorn, httpx, python-multipart
from fastapi import FastAPI, Request
from fastapi.responses import JSONResponse

load_dotenv()

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(levelname)s] %(name)s: %(message)s",
)
logger = logging.getLogger("max_bot")

# ── Конфигурация из окружения ────────────────────────────────────────────────
MAX_TOKEN = os.environ.get("MAX_ACCESS_TOKEN", "").strip()   # токен бота в MAX
ADMIN_USER_ID = os.environ.get("ADMIN_USER_ID", "").strip()  # user_id админа в MAX
WALLET = os.environ.get("WALLET", "").strip()
PRICE = os.environ.get("PRICE", "1490")
PDF_PATH = Path(os.environ.get("PDF_PATH", "установка AI агента.pdf"))

MAX_API = "https://platform-api2.max.ru"
HEADERS = {"Authorization": MAX_TOKEN, "Content-Type": "application/json"}

# Верификация TLS к platform-api2.max.ru: у MAX своя CA-цепочка (в т.ч.
# Минцифры). Если хост не имеет нужного корневого сертификата — ставьте
# в .env MAX_TLS_VERIFY=false (при доступном CA рекомендуем true).
_TLS_VERIFY = os.environ.get("MAX_TLS_VERIFY", "true").strip().lower() not in ("0", "false", "no")


def _api(timeout: float = 20.0) -> httpx.AsyncClient:
    """HTTP-клиент к MAX API с учётом настройки TLS-верификации."""
    return httpx.AsyncClient(timeout=timeout, verify=_TLS_VERIFY)

# ── Маленькая очередь на лимит 2 msg/сек в один диалог ──────────────────────
_send_lock = threading.Lock()
_last_send_time: dict = {}


def _rate_limit(chat_key: str) -> None:
    """Грубый интервал ~0.6 c между сообщениями в один диалог."""
    with _send_lock:
        last = _last_send_time.get(chat_key, 0)
        import time

        now = time.monotonic()
        wait = max(0.0, last + 0.65 - now)
        if wait:
            time.sleep(wait)
        _last_send_time[chat_key] = time.monotonic()


# ── Прямые вызовы MAX API ────────────────────────────────────────────────────
async def send_text(user_id: str, text: str) -> None:
    """Отправить простое текстовое сообщение пользователю."""
    _rate_limit(f"u{user_id}")
    payload = {"text": text, "format": "markdown"}
    async with _api(20) as client:
        r = await client.post(
            f"{MAX_API}/messages?user_id={user_id}",
            headers=HEADERS,
            json=payload,
        )
        r.raise_for_status()
        logger.info("send_text -> user %s: %s", user_id, r.status_code)


async def send_image_to_admin(image_token: str, caption: str) -> None:
    """Переслать чек-изображение админу с блоком кнопок."""
    _rate_limit(f"u{ADMIN_USER_ID}")
    payload = {
        "text": caption,
        "format": "markdown",
        "attachments": [
            {
                "type": "image",
                "payload": {"token": image_token},   # токен входящего изображения
            },
            {
                "type": "inline_keyboard",
                "payload": {
                    "buttons": [
                        [
                            {
                                "type": "callback",
                                "text": "✅ Выдать методичку",
                                "callback_data": f"approve:",  # см. ниже payload
                            },
                            {
                                "type": "callback",
                                "text": "❌ Отклонить",
                                "callback_data": f"reject:",
                            },
                        ]
                    ]
                },
            },
        ],
    }
    # TODO(probe): структура кнопки-callback и передача id покупателя уточняется.
    async with _api(20) as client:
        r = await client.post(
            f"{MAX_API}/messages?user_id={ADMIN_USER_ID}",
            headers=HEADERS,
            json=payload,
        )
        r.raise_for_status()


async def upload_pdf() -> str:
    """Загружает PDF через POST /uploads?type=file, возвращает upload-токен."""
    async with _api(60) as client:
        # Шаг 1: получаем URL загрузки и upload-токен
        r = await client.post(
            f"{MAX_API}/uploads?type=file",
            headers={"Authorization": MAX_TOKEN},
        )
        r.raise_for_status()
        data = r.json()  # {"url": ..., "token": ...}  (точные ключи — probe)
        upload_url = data["url"]
        upload_token = data["token"]

        # Шаг 2: загружаем сам файл (multipart)
        with open(PDF_PATH, "rb") as f:
            files = {"file": (PDF_PATH.name, f, "application/pdf")}
            rr = await client.post(upload_url, files=files)
        rr.raise_for_status()
    logger.info("PDF загружен: %s (%s)", PDF_PATH.name, r.status_code)
    return upload_token


async def send_file_to_user(user_id: str, caption: str) -> None:
    """Загрузить PDF и выслать пользователю файлом."""
    token = await upload_pdf()
    _rate_limit(f"u{user_id}")
    payload = {
        "text": caption,
        "attachments": [
            {"type": "file", "payload": {"token": token}}
        ],
    }
    async with _api(60) as client:
        r = await client.post(
            f"{MAX_API}/messages?user_id={user_id}",
            headers=HEADERS,
            json=payload,
        )
        r.raise_for_status()


def payment_text() -> str:
    return (
        "📘 *Методичка «Установка ИИ-агента»*\n\n"
        f"Цена: *{PRICE} ₽*\n\n"
        "Оплата через Юмани:\n"
        f"`{WALLET}`\n\n"
        "После перевода пришлите сюда *скриншот чека* — "
        "проверю и пришлю методичку."
    )


# ── Фреймворк обновлений (FastAPI webhook) ───────────────────────────────────
app = FastAPI(title="ai-methodichka-max-bot")


@app.post("/webhook")
async def webhook(request: Request):
    """Точка входа для событий MAX (POST на /webhook).

    MAX требует HTTP 200 в течение 30 c, поэтому отвечаем сразу,
    а обработку событий выполняем в фоне через asyncio.
    """
    try:
        payload = await request.json()
    except Exception:
        return JSONResponse({"ok": False, "error": "bad json"}, status_code=400)

    updates = payload if isinstance(payload, list) else [payload]
    for upd in updates:
        asyncio.create_task(_handle_safe(upd))
    return JSONResponse({"ok": True})


async def _handle_safe(upd: dict) -> None:
    """Фоновая обёртка: не рушит webhook при ошибке обработки."""
    try:
        await handle_update(upd)
    except Exception:
        logger.exception("Ошибка обработки update")


async def handle_update(upd: dict) -> None:
    kind = upd.get("update_type")
    # Полный входящий объект пишем в лог — на живом токене по нему
    # сверяем реальную структуру событий (поля user, message и т.п.).
    logger.info("UPDATE type=%s payload=%s", kind, json.dumps(upd, ensure_ascii=False, default=str)[:2000])

    if kind == "bot_started":
        # Возобновление/начало диалога (аналог /start). Сообщаем цену.
        user_id = upd["user"]["user_id"] if upd.get("user") else None
        if user_id:
            await send_text(str(user_id), payment_text())
        return

    if kind == "message_created":
        # Обрабатываем входящее сообщение покупателя.
        # TODO(probe): как структурировано сообщение внутри update — здесь
        # используется принятая гипотеза (проверить на живых данных):
        msg = upd.get("message") or {}
        sender = msg.get("sender") or upd.get("user") or {}
        text = (msg.get("body") or {}).get("text") or ""
        attachments = (msg.get("body") or {}).get("attachments") or []

        user_id = sender.get("user_id")
        if user_id is None:
            return
        user_id = str(user_id)

        has_photo = any(a.get("type") == "image" for a in attachments)

        if not has_photo and not text.strip():
            return

        # Покупатель прислал чек (текст или картинка) — пробрасываем админу.
        image_token = None
        for a in attachments:
            if a.get("type") == "image":
                image_token = (a.get("payload") or {}).get("token")
                break
        caption = f"📩 Новый чек от @{sender.get('username') or user_id}\n(ID: {user_id})"
        if text.strip():
            caption += f"\nТекст: {text}"
        if image_token:
            await send_image_to_admin(image_token, caption)
        else:
            await send_text(ADMIN_USER_ID, caption)
        await send_text(user_id, "✅ Чек получен! Проверю оплату — методичка придёт.")

    if kind == "message_callback":
        # Нажатие кнопки админом. Разбор callback_data.
        # TODO(probe): где лежит callback_data и кто нажал — проверить.
        cb = upd.get("callback_query") or upd
        data = (cb.get("payload") or {}).get("callback_data") or ""
        from_user_id = (cb.get("user") or {}).get("user_id")
        if str(from_user_id) != ADMIN_USER_ID:
            return

        # Формат ожидается: "approve:<buyer_id>" или "reject:<buyer_id>"
        action, _, buyer = data.partition(":")
        if action == "approve" and buyer:
            try:
                await send_file_to_user(buyer, "📘 Ваша методичка! Спасибо за покупку.")
                await send_text(ADMIN_USER_ID, "✅ Методичка выдана покупателю.")
            except Exception as e:
                logger.exception("Ошибка выдачи PDF %s: %s", buyer, e)
                await send_text(ADMIN_USER_ID, f"⚠️ Не удалось выдать: {e}")
        elif action == "reject" and buyer:
            if buyer:
                await send_text(buyer, "❌ Оплата не подтверждена. Пришлите чек ещё раз.")
            await send_text(ADMIN_USER_ID, "❌ Чек отклонён.")


if __name__ == "__main__":
    if not MAX_TOKEN:
        raise SystemExit("MAX_ACCESS_TOKEN не задан в .env")
    if not ADMIN_USER_ID:
        raise SystemExit("ADMIN_USER_ID не задан в .env")
    import uvicorn

    port = int(os.environ.get("PORT", "8000"))
    uvicorn.run(app, host="0.0.0.0", port=port)
