# MAX-бот «Установка ИИ-агента» — деплой на Яндекс.Клауд (VM 24/7)

Файлы для разворачивания бота мессенджера MAX на постоянной VM
(Яндекс.Клауд Compute, Ubuntu 24.04) — webhook работает поверх domain
`max.<ваш-домен>` через nginx + Let's Encrypt.

Состав:
- `max-bot.service`          — systemd-юнит (автозапуск, рестарт)
- `max-domain.conf`          — конфиг nginx (HTTPS reverse-proxy на 127.0.0.1:8000)
- `README.md`                — этот файл
- `_deploy.sh`               — скрипт установки (выполняется один раз на чистой VM)

## Что делает webhook-бот

MAX шлёт POST-события на `https://DOMAIN/webhook`. nginx проксирует их на
uvicorn (`bot_max.py`, порт 8000). HTTPS-сертификат — Let's Encrypt (certbot).
PDF методички выдаётся ботом через MAX API при нажатии админом «✅ Выдать».

## Переменные `.env` (на сервере, не в git)

Создаётся в рабочей папке (`/home/<user>/max-bot/.env`, права 600):

```
MAX_ACCESS_TOKEN=<токен из business.max.ru>
ADMIN_USER_ID=<user_id админа в MAX>
WALLET=4100119578034128
PRICE=1490
PDF_PATH=установка AI агента.pdf
PORT=8000
```

## Порядок установки на чистую VM (Ubuntu 24.04 + публичный IP)

```bash
# 0) Домен: A-запись   DOMAIN   -> <публичный IP VM>
#    (в панели ispmanager -> «Управление DNS»)

# 1) SSH на VM и стейдж
sudo apt update
sudo apt install -y python3-venv git nginx certbot python3-certbot-nginx
sudo mkdir -p /opt/...   # рабочая папка — git clone репозитория

# 2) venv + зависимости
python3 -m venv .venv
.venv/bin/pip install -r requirements.txt

# 3) .env (секреты), права
chmod 600 .env

# 4) сертификат (верификация домена по :80)
sudo systemctl stop nginx
sudo certbot certonly --standalone --agree-tos -m admin@DOMAIN -d DOMAIN
sudo systemctl start nginx

# 5) systemd-юнит + запуск
sudo cp deploy/max-bot.service /etc/systemd/system/
sudo systemctl daemon-reload
sudo systemctl enable --now max-bot

# 6) nginx reverse-proxy
sudo cp deploy/max-domain.conf  /etc/nginx/sites-available/DOMAIN
sudo ln -sf /etc/nginx/sites-available/DOMAIN /etc/nginx/sites-enabled/
#   (замените DOMAIN на фактический домен в conf)
sudo systemctl reload nginx
```

## Проверки

```
curl -s https://DOMAIN/healthz          # → ok
curl -sX POST https://DOMAIN/webhook -H 'Content-Type: application/json' -d '{}'
                                        # → {"ok":true}
sudo systemctl status max-bot
```
