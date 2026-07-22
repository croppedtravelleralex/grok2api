FROM python:3.12-slim

WORKDIR /app

COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt

COPY statsig.py pair.py app.py ./

ENV SIGNER_PORT=8788 \
    PYTHONUNBUFFERED=1

EXPOSE 8788

HEALTHCHECK --interval=30s --timeout=5s --start-period=5s --retries=3 \
  CMD python3 -c "import urllib.request; urllib.request.urlopen('http://127.0.0.1:8788/healthz', timeout=3).read()"

CMD ["python3", "app.py"]
