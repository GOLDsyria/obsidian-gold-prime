FROM python:3.12-slim

WORKDIR /app
COPY requirements.txt /app/
RUN pip install --no-cache-dir -r requirements.txt

COPY main.py /app/main.py

ENV PORT=8000
EXPOSE 8000

CMD ["bash", "-lc", "uvicorn main:app --host 0.0.0.0 --port ${PORT}"]
