FROM python:3.13-slim-bookworm

ENV LANG=C.UTF-8 \
    LC_ALL=C.UTF-8 \
    TZ=Asia/Shanghai \
    T2I_RENDER_WAIT_UNTIL=domcontentloaded \
    T2I_SKIP_FONT_READY=true

WORKDIR /app

COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt \
    && playwright install --with-deps chromium \
    && apt-get update \
    && DEBIAN_FRONTEND=noninteractive apt-get install -y --no-install-recommends \
        fontconfig \
        fonts-noto-cjk \
        fonts-noto-color-emoji \
        tzdata \
    && fc-cache -f \
    && ln -snf "/usr/share/zoneinfo/$TZ" /etc/localtime \
    && printf '%s\n' "$TZ" > /etc/timezone \
    && rm -rf /var/lib/apt/lists/*

COPY . .

RUN python -m compileall -q main.py src

HEALTHCHECK --interval=30s --timeout=5s --start-period=20s --retries=3 \
    CMD python -c "import os, urllib.request; urllib.request.urlopen('http://127.0.0.1:' + os.getenv('PORT', '8999') + '/healthz', timeout=3)"

CMD ["python", "main.py"]
