FROM python:3.12-slim

# 安裝 ffmpeg（音樂播放必要）
RUN apt-get update && apt-get install -y \
    ffmpeg \
    && rm -rf /var/lib/apt/lists/*

WORKDIR /app

COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt

COPY cookies.txt .
COPY bot.py .

CMD ["python", "bot.py"]