FROM python:3.12-slim
ENV PYTHONDONTWRITEBYTECODE=1 PYTHONUNBUFFERED=1
WORKDIR /app
COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt && useradd --create-home bot
COPY --chown=bot:bot . .
RUN mkdir -p /app/data && chown bot:bot /app/data
USER bot
CMD ["python", "bot.py"]
