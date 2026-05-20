FROM python:3.12-slim

WORKDIR /app
COPY daily_news_bot.py /app/daily_news_bot.py

ENV PYTHONUNBUFFERED=1
CMD ["python", "daily_news_bot.py"]
