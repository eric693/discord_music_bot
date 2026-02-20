FROM python:3.12-slim

# 安裝 ffmpeg + nodejs（yt-dlp 需要 JS runtime 解析 YouTube）
RUN apt-get update && apt-get install -y \
    ffmpeg \
    nodejs \
    && rm -rf /var/lib/apt/lists/*

WORKDIR /app

COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt

COPY cookies.txt .
COPY bot.py .

CMD ["python", "bot.py"]