FROM python:3.13.7-slim

# System deps: ffmpeg for audio, libopus for Discord voice
RUN apt-get update && apt-get install -y --no-install-recommends \
    ffmpeg libopus0 \
 && rm -rf /var/lib/apt/lists/*

# App dir
WORKDIR /app

# Copy and install Python deps
# (requirements.txt should contain: discord.py[voice] and yt-dlp)
COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt

# Copy bot code
COPY . .

# Make logs unbuffered
ENV PYTHONUNBUFFERED=1

# Default: ffmpeg in PATH
ENV FFMPEG_BIN=ffmpeg

# Run
CMD ["python", "bot.py"]