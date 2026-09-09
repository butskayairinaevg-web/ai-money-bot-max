"""
Бот-продавец методички «Установка ИИ-агента» для мессенджера MAX.

Логика (ручное подтверждение оплаты — как в Telegram-версии, но через MAX API):
  1. Покупатель начинает диалог с ботом -> бот шлёт цену, кошелёк и просит чек
     (скриншот).
  2. Покупатель присылает фото-чек -> бот пересылает чек админу (в MAX диалог
     с админом) с кнопками «✅ Выдать методичку» / «❌ Отклонить».
  3. Админ жмёт «Выдать» -> бот через POST /uploads загружает PDF и шлёт его
     покупателю через POST /messages (файл до 4 ГБ, PDF поддерживается).

MAX API (проверено на живом токене 09.09, актуальные структуры):
  - База: https://platform-api2.max.ru, заголовок Authorization: <token>.
  - Обновления — ТОЛЬКО Webhook (MAX шлёт POST на ваш HTTPS URL).
  - Исходящие сообщения: POST /messages?user_id=<id> (или ?chat_id=<id>).
  - Кнопки типа callback в исходящем: {type: callback, text, payload: <строка>}.
    Результат нажатия приходит в событии message_callback как callback.payload
    строкой, нажавший — callback.user.user_id.
  - Изображение в исходящем слали по payload.url (ссылка из входящего события)
    или payload.token (токен от POST /uploads).
  - Файл по документации отправляется ТОЛЬКО в связке с inline_keyboard
    (в одиночку/с картинкой не примет) -> сообщение с PDF дополнено кнопкой.
  - Лимит: не более 2 сообщений/сек в один диалог.

Все секреты (MAX_ACCESS_TOKEN, ADMIN_USER_ID, WALLET, PRICE, PDF_PATH,
MAX_TLS_VERIFY) — в .env. Токены/номера не хранятся в коде и не в git.
"""

import os
import json
import logging
import threading
import asyncio
import time
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
        now = time.monotonic()
        wait = max(0.0, last + 0.65 - now)
        if wait:
            time.sleep(wait)
        _last_send_time[chat_key] = time.monotonic()


# ── Прямые вызовы MAX API ────────────────────────────────────────────────────
async def _post_message(target_user_id: str, payload: dict) -> httpx.Response:
    """POST /messages?user_id= для человека; при не-200 логируем тело ошибки."""
    async with _api(30) as client:
        r = await client.post(
            f"{MAX_API}/messages?user_id={target_user_id}",
            headers=HEADERS,
            json=payload,
        )
        if r.status_code != 200:
            logger.warning(
                "POST messages user=%s -> HTTP %s body=%s",
                target_user_id, r.status_code, r.text,
            )
        r.raise_for_status()
        return r


async def send_text(user_id: str, text: str) -> None:
    """Отправить простое текстовое сообщение пользователю."""
    _rate_limit(f"u{user_id}")
    payload = {"text": text, "format": "markdown"}
    await _post_message(user_id, payload)
    logger.info("send_text -> user %s", user_id)


async def send_image_to_admin(image_payload: dict, caption: str, buyer_id: str) -> None:
    """Переслать чек-изображение админу с кнопками «Выдать / Отклонить».

    image_payload: {"url": <прямая ссылка на картинку из входящего события>}
    или {"token": <токен от POST /uploads>}. Токен входящего события НЕ
    пересылается в исходящее (валиден только для получения файла).
    В сообщении с картинкой допускается одно вложение inline_keyboard.
    """
    _rate_limit(f"u{ADMIN_USER_ID}")
    payload = {
        "text": caption,
        "format": "markdown",
        "attachments": [
            {"type": "image", "payload": image_payload},
            {
                "type": "inline_keyboard",
                "payload": {
                    "buttons": [
                        [
                            {
                                "type": "callback",
                                "text": "✅ Выдать методичку",
                                "payload": f"approve:{buyer_id}",
                            },
                            {
                                "type": "callback",
                                "text": "❌ Отклонить",
                                "payload": f"reject:{buyer_id}",
                            },
                        ]
                    ]
                },
            },
        ],
    }
    await _post_message(str(ADMIN_USER_ID), payload)


async def upload_file() -> str:
    """Загрузить PDF через MAX API, вернуть upload-токен для вложения.

    Реальный цикл (проверено на живом токене 09.09):
      1. POST /uploads?type=file
         -> ответ СОДЕРЖИТ ТОЛЬКО поле url (token в нём нет для type=file):
            {"url": "https://fu.oneme.ru/api/upload.do?sig=..."}
      2. Загрузить сам файл на этот url ПОЛЕМ multipart по имени data
         (curl: -F "data=@файл.pdf") 
         -> ответ возвращает токен: {"fileId": ..., "token": "..."}
      Этот token из шага 2 и подставляется в attachments.payload.token.
    """
    async with _api(90) as client:
        # Шаг 1: получить URL загрузки
        r = await client.post(
            f"{MAX_API}/uploads?type=file",
            headers={"Authorization": MAX_TOKEN},
        )
        if r.status_code != 200:
            logger.warning("POST /uploads -> HTTP %s body=%s", r.status_code, r.text)
        r.raise_for_status()
        data = r.json()
        upload_url = data.get("url") or data.get("upload_url")
        if not upload_url:
            raise RuntimeError(f"Некорректный ответ POST /uploads (нет url): {data}")

        # Шаг 2: загрузить файл (multipart, имя поля data) и забрать токен
        with open(PDF_PATH, "rb") as f:
            files = {"data": (PDF_PATH.name, f, "application/pdf")}
            rr = await client.post(upload_url, files=files)
        if rr.status_code != 200:
            logger.warning("file upload -> HTTP %s body=%s", rr.status_code, rr.text)
        rr.raise_for_status()
        resp = rr.json()
        upload_token = resp.get("token")
        if not upload_token:
            raise RuntimeError(f"Некорректный ответ загрузки файла (нет token): {resp}")
    logger.info("PDF загружен: %s", PDF_PATH.name)
    return upload_token


async def send_file_to_user(user_id: str, caption: str) -> None:
    """Загрузить PDF и выслать пользователю файлом.

    По документации MAX файл отправляется ТОЛЬКО в комбинации с вложением
    inline_keyboard (совместно с изображением/видео не поддерживается), поэтому
    сообщение с файлом дополнено кнопкой.

    Известная особенность: сразу после загрузки файл обрабатывается сервером
    ~несколько секунд; попытка прикрепить его раньше готовности возвращает
    400 attachment.not.ready (errors.process.attachment.file.not.processed).
    Поэтому делаем повторные попытки с паузой до появления готовности.
    """
    token = await upload_file()
    payload = {
        "text": caption,
        "attachments": [
            {"type": "file", "payload": {"token": token}},
            {
                "type": "inline_keyboard",
                "payload": {
                    "buttons": [
                        [
                            {
                                "type": "callback",
                                "text": "✅ Методичка получена",
                                "payload": "received",
                            }
                        ]
                    ]
                },
            },
        ],
    }
    # Ждём, пока MAX не обработает загруженный файл (повтор до ~30 c)
    for attempt in range(12):
        _rate_limit(f"u{user_id}")
        try:
            await _post_message(user_id, payload)
            return
        except httpx.HTTPStatusError as e:
            body = getattr(e.response, "text", "")
            not_ready = (
                "attachment.not.ready" in body
                or "not.processed" in body
            )
            if not not_ready:
                raise
            logger.warning(
                "PDF вложение ещё не готово (попытка %s), ждём 2.5 с", attempt + 1
            )
            await asyncio.sleep(2.5)
    raise RuntimeError("PDF так и не стал доступен для отправки (30 c)")


def welcome_text() -> str:
    """Первое рекламное сообщение бота (оффер про CodeWhale)."""
    return (
        "Твой компьютер станет УМНЕЕ тебя (и это хорошо)\n"
        "Представь:\n"
        "Ты просто говоришь своему ПК:\n"
        "«Сделай таблицу расходов»\n"
        "«Переименуй все фото из папки»\n"
        "«Напиши письмо клиенту»\n"
        "И он делает. Без копирования в ChatGPT. Без гуглежа. Без нервов.\n\n"
        "Это CodeWhale — твой личный ИИ-помощник\n"
        "💰 Сколько стоит? Бесплатно (платишь копейки за запросы — как за конфету)\n"
        "🛡️ Безопасно? Да. Ничего не делает без твоего «ОК». Всё можно отменить.\n"
        "⭐ Сложно? Нет. Говоришь по-русски — он понимает.\n\n"
        "Что ты получаешь:\n"
        "✅ Освобождаешь 2-3 часа в день\n"
        "✅ Перестаешь платить фрилансерам за простые задачи\n"
        "✅ Твой компьютер работает на тебя, а не ты на него\n"
        "✅ Никаких навыков программирования не нужно\n\n"
        "Установка за 5 минут:\n"
        "Подпишись на канал\n"
        "Напиши в комментариях «ХОЧУ»\n"
        "Я пришлю инструкцию с картинками\n"
        "Даже твоя бабушка справится\n\n"
        "⚡️ Каждый день без CodeWhale — это потерянное время."
    )


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



_PAYMENT_WORDS = ("перевел", "перевёл", "оплатил", "кинул", "перевод",
                  "чек", "скрин", "оплата", "квитанц", "перевод на", "с карты")


def looks_like_payment(text: str) -> bool:
    """Грубая эвристика: похоже ли текстовое сообщение на сообщение об оплате.

    Чек обычно присылают фотографией — картинку считаем чеком всегда (выше).
    Текстом «чек» считаем только если текст упоминает перевод/оплату/чек.
    Всё прочее (приветствия, вопросы «сколько стоит», «хочу консультацию»
    и т.п.) НЕ считается чеком и не должно превращаться в ложный «Новый чек»
    у админа.
    """
    low = text.lower()
    return any(w in low for w in _PAYMENT_WORDS)


async def handle_update(upd: dict) -> None:
    kind = upd.get("update_type")
    logger.info("UPDATE type=%s payload=%s", kind, json.dumps(upd, ensure_ascii=False, default=str)[:2000])

    if kind == "bot_started":
        # Возобновление/начало диалога (аналог /start). Сначала оффер про
        # CodeWhale, затем (для имеющих целью покупку) — методичка с ценой.
        user = upd.get("user") or {}
        user_id = user.get("user_id") or upd.get("user_id")
        if user_id:
            uid = str(user_id)
            await send_text(uid, welcome_text())
            await send_text(uid, payment_text())
        return

    if kind == "message_created":
        # Покупатель прислал сообщение (чек). Структура подтверждена на живом
        # событии: message.sender.user_id, message.body.text и
        # message.body.attachments[] с payload.url / payload.token.
        msg = upd.get("message") or {}
        sender = msg.get("sender") or {}
        body = msg.get("body") or {}
        text = body.get("text") or ""
        attachments = body.get("attachments") or []

        user_id = sender.get("user_id") or upd.get("user_id")
        if user_id is None:
            return
        user_id = str(user_id)

        has_photo = any(a.get("type") == "image" for a in attachments)

        # Чек: фото ИЛИ текст с явным упоминанием оплаты.
        is_check = has_photo or looks_like_payment(text)
        if not is_check and not text.strip():
            # ничего не прислали — игнор
            return
        if not is_check:
            # обычный вопрос/приветствие (не чек) — отвечаем человеку,
            # фейковый «чек» админу НЕ уходит
            await send_text(
                user_id,
                "👋 Привет! Чтобы купить методичку «Установка ИИ-агента» "
                f"({PRICE} ₽):\n\n"
                "ℹ️ Подпишитесь на канал и напишите в комментариях «ХОЧУ» — "
                "пришлю инструкцию и детали оплаты.\n\n"
                "Если вы уже перевели оплату — пришлите сюда *скриншот чека*,"
                " я проверю и отправлю методичку.",
            )
            return

        # Покупатель прислал чек — пробрасываем админу.
        caption = f"📩 Новый чек от @{sender.get('username') or user_id}\n(ID: {user_id})"
        if text.strip():
            caption += f"\nТекст: {text}"

        img_payload = None
        for a in attachments:
            if a.get("type") == "image":
                p = a.get("payload") or {}
                # url из входящего события надёжнее всего для пересылки админу
                img_payload = {"url": p["url"]} if p.get("url") else (
                    {"token": p["token"]} if p.get("token") else None
                )
                break

        if img_payload:
            await send_image_to_admin(img_payload, caption, user_id)
        else:
            await send_text(ADMIN_USER_ID, caption)
        await send_text(user_id, "✅ Чек получен! Проверю оплату — методичка придёт.")

    if kind == "message_callback":
        # Нажатие кнопки. Реальная структура (09.09):
        #   callback.user.user_id  — кто нажал
        #   callback.payload       — строка "approve:<buyer_id>" / "reject:<id>"
        cb = upd.get("callback") or {}
        from_user_id = (cb.get("user") or {}).get("user_id")
        if str(from_user_id) != ADMIN_USER_ID:
            logger.info("callback от не-админа user_id=%s — игнор", from_user_id)
            return

        data = cb.get("payload") or ""
        action, _, buyer = data.partition(":")
        if action == "approve" and buyer:
            try:
                await send_file_to_user(buyer, "📘 Ваша методичка! Спасибо за покупку.")
                await send_text(ADMIN_USER_ID, "✅ Методичка выдана покупателю.")
            except Exception as e:
                logger.exception("Ошибка выдачи PDF %s: %s", buyer, e)
                await send_text(ADMIN_USER_ID, f"⚠️ Не удалось выдать: {e}")
        elif action == "reject" and buyer:
            await send_text(buyer, "❌ Оплата не подтверждена. Пришлите чек ещё раз.")
            await send_text(ADMIN_USER_ID, "❌ Чек отклонён.")


if __name__ == "__main__":
    if not MAX_TOKEN:
        raise SystemExit("MAX_ACCESS_TOKEN не задан в .env")
    if not ADMIN_USER_ID or ADMIN_USER_ID == "PUT_ADMIN_USER_ID_HERE":
        raise SystemExit("ADMIN_USER_ID не задан в .env")
    import uvicorn

    port = int(os.environ.get("PORT", "8000"))
    uvicorn.run(app, host="0.0.0.0", port=port)
