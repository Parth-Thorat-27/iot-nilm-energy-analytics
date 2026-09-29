# Runtime for the Python services (bridge, nilm, anomaly, simulator).
# The code itself is bind-mounted from ./backend by docker-compose, so edits apply on restart
# without rebuilding. Rebuild only when requirements.txt changes: docker compose build
FROM python:3.11-slim

ENV PYTHONUNBUFFERED=1 \
    PIP_NO_CACHE_DIR=1 \
    PIP_DISABLE_PIP_VERSION_CHECK=1

WORKDIR /app
COPY requirements.txt .
RUN pip install -r requirements.txt

COPY . .
CMD ["python", "bridge.py"]
