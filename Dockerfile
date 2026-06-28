FROM python:3.12-slim

RUN apt-get update \
    && apt-get install -y --no-install-recommends \
       restic \
       systemd \
       wget \
    && rm -rf /var/lib/apt/lists/*

WORKDIR /app

COPY app.py /app/app.py

EXPOSE 5000

HEALTHCHECK --interval=30s --timeout=5s --start-period=5s --retries=3 \
  CMD python -c "import urllib.request; urllib.request.urlopen('http://127.0.0.1:5000/health', timeout=2).read()" || exit 1

CMD ["python", "/app/app.py"]