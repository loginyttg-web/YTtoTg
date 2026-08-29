FROM python:3.11-slim

# Install Node.js (for yt-dlp n-challenge solving) and FFmpeg (for merging video+audio)
# apt installs node to /usr/bin (already on PATH) — no PATH hack needed.
RUN apt-get update && apt-get install -y \
    nodejs \
    npm \
    ffmpeg \
    && rm -rf /var/lib/apt/lists/*

WORKDIR /app

# Install Python dependencies first (better layer caching)
COPY ytbot/requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt

# Copy application code
COPY ytbot/ ./ytbot/

# Run the bot
CMD ["python", "ytbot/main.py"]
