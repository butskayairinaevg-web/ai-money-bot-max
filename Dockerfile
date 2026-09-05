FROM python:3.11-slim

ENV TZ=Europe/Moscow
WORKDIR /app

COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt

COPY . .

RUN useradd --create-home --shell /bin/bash bot && chown -R bot:bot /app
USER bot

CMD ["python", "bot_max.py"]
