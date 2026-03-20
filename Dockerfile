FROM python:3.11-slim

RUN apt-get update && apt-get install -y \
    ffmpeg curl unzip git nodejs npm \
    && rm -rf /var/lib/apt/lists/*

# Instalar Deno
RUN curl -fsSL https://deno.land/install.sh | sh
ENV DENO_INSTALL="/root/.deno"
ENV PATH="$DENO_INSTALL/bin:$PATH"

# Instalar bgutil-ytdlp-pot-provider server (para PO Token con Deno)
RUN git clone https://github.com/coletdjnz/bgutil-ytdlp-pot-provider.git /opt/bgutil && \
    cd /opt/bgutil/server && \
    npm install --ignore-scripts 2>/dev/null || true
ENV BGUTIL_SERVER_HOME="/opt/bgutil/server"

WORKDIR /app

COPY backend/requirements.txt .
RUN pip install --no-cache-dir --upgrade pip && \
    pip install --no-cache-dir -r requirements.txt && \
    pip install --no-cache-dir --upgrade yt-dlp bgutil-ytdlp-pot-provider

COPY backend ./backend
COPY frontend ./frontend

ENV FRONTEND_DIR=/app/frontend
RUN mkdir -p /app/backend/downloads

EXPOSE 10000

CMD ["sh", "-c", "cd backend && uvicorn main:app --host 0.0.0.0 --port ${PORT:-10000}"]
