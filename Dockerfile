FROM python:3.11-slim

# Install Node.js (for yt-dlp n-challenge solving) and FFmpeg (for merging video+audio)
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

# Ensure node is on PATH for yt-dlp JS challenge solving
ENV PATH="/usr/local/bin/node:${PATH}"

# Run the bot
CMD ["python", "ytbot/main.py"]
