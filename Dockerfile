FROM python:3.12-slim@sha256:aa943ce411423c7582288f18707ff66eddcbaeb510502d747a72aeb34f30d159
ENV PYTHONUNBUFFERED=1 PYTHONDONTWRITEBYTECODE=1 RECON_DATA_DIR=/data TMPDIR=/data/tmp
WORKDIR /app
COPY requirements.lock.txt .
RUN pip install --no-cache-dir -r requirements.lock.txt && useradd --uid 10001 --create-home recon && mkdir -p /data/tmp /scratch && chown -R recon:recon /data /scratch
COPY pyproject.toml README.md ./
COPY src ./src
RUN pip install --no-cache-dir --no-deps . && rm -rf build src/*.egg-info
USER recon
EXPOSE 8000
HEALTHCHECK --interval=30s --timeout=5s --start-period=15s CMD python -c "import urllib.request; urllib.request.urlopen('http://127.0.0.1:8000/healthz',timeout=3)"
CMD ["gunicorn", "--no-control-socket", "--workers", "1", "--threads", "8", "--bind", "0.0.0.0:8000", "--timeout", "120", "--graceful-timeout", "120", "--access-logfile", "-", "--access-logformat", "%(h)s %(m)s %(U)s %(s)s %(L)s", "reconcile.server:create_app()"]
