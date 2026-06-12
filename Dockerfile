FROM python:3.12-slim

ENV PYTHONUNBUFFERED=1 \
    PYTHONDONTWRITEBYTECODE=1 \
    PIP_NO_CACHE_DIR=1

WORKDIR /app

COPY requirements.txt .
RUN pip install -r requirements.txt

COPY app.py units.py storage.py sabnzbd.py jellyfin.py jdownloader.py resolver.py ./
COPY templates/ ./templates/

VOLUME /config

EXPOSE 6811

HEALTHCHECK --interval=30s --timeout=5s --start-period=10s --retries=3 \
    CMD python -c "import urllib.request,sys; sys.exit(0 if urllib.request.urlopen('http://localhost:6811/health',timeout=3).status==200 else 1)"

CMD ["python", "app.py"]
