FROM python:3.11-slim

RUN apt-get update && apt-get install -y \
    ffmpeg \
    curl \
    unzip \
    nodejs \
    npm \
    && rm -rf /var/lib/apt/lists/*

# Install Deno
RUN curl -fsSL https://deno.land/install.sh | sh
ENV DENO_INSTALL="/root/.deno"
ENV PATH="$DENO_INSTALL/bin:$PATH"

# Pre-instalar el challenge solver de yt-dlp (evita descarga en runtime)
RUN yt-dlp --update-to nightly 2>/dev/null || true
RUN pip install --no-cache-dir yt-dlp 2>/dev/null || true

WORKDIR /app

COPY backend/requirements.txt .
RUN pip install --no-cache-dir --upgrade pip && \
    pip install --no-cache-dir -r requirements.txt && \
    pip install --no-cache-dir --upgrade yt-dlp

# Descargar componentes EJS en build time
RUN yt-dlp --no-download \
    --extractor-args "youtube:player_client=web" \
    --remote-components ejs:npm \
    "https://www.youtube.com/watch?v=dQw4w9WgXcQ" 2>/dev/null || true

COPY backend ./backend
COPY frontend ./frontend

ENV FRONTEND_DIR=/app/frontend
RUN mkdir -p /app/backend/downloads

EXPOSE 10000

CMD ["sh", "-c", "cd backend && uvicorn main:app --host 0.0.0.0 --port ${PORT:-10000}"]
