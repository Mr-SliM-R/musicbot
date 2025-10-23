FROM python:3.13.7-slim

# Install system dependencies required by discord voice
RUN apt-get update && apt-get install -y --no-install-recommends \
    ffmpeg libopus0 \
 && rm -rf /var/lib/apt/lists/*

WORKDIR /app

# Install Python dependencies first for better caching
COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt

# Copy the rest of the application
COPY . .

ENV PYTHONUNBUFFERED=1 \
    FFMPEG_BIN=ffmpeg

CMD ["python", "bot.py"]
