FROM python:3.13-slim

ENV PYTHONUNBUFFERED=1

WORKDIR /bot

COPY requirements.txt .

RUN pip install --no-cache-dir -r requirements.txt

COPY bot.py .

CMD ["python", "bot.py"]