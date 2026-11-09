FROM python:3.12-slim
ENV PYTHONDONTWRITEBYTECODE=1 PYTHONUNBUFFERED=1
WORKDIR /app
COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt && useradd --create-home bot
COPY --chown=bot:bot . .
RUN install -d -m 0750 -o bot -g bot /app/data /app/backups
USER bot
CMD ["python", "bot.py"]
