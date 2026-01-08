# استخدام نسخة بايثون خفيفة وحديثة
FROM python:3.10-slim

# ضبط إعدادات بايثون لتظهر السجلات (Logs) فوراً
ENV PYTHONUNBUFFERED=1

# تحديد مجلد العمل داخل السيرفر
WORKDIR /app

# نسخ ملف المتطلبات وتثبيتها
COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt

# نسخ كود البوت
COPY bot.py .

# أمر تشغيل البوت
CMD ["python", "bot.py"]
