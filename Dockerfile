FROM python:3.12-slim

WORKDIR /app
COPY requirements.txt /app/requirements.txt
RUN pip install --no-cache-dir -r /app/requirements.txt

COPY bot.py /app/bot.py

ENV PORT=8000
EXPOSE 8000

CMD ["uvicorn", "bot:app", "--host", "0.0.0.0", "--port", "8000"]
