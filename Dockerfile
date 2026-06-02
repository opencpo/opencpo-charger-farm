# ─── Build stage ─────────────────────────────────────────────
FROM python:3.12-slim AS builder

WORKDIR /build
COPY requirements.txt .
RUN pip install --no-cache-dir --prefix=/install -r requirements.txt

# ─── Runtime stage ───────────────────────────────────────────
FROM python:3.12-slim

WORKDIR /app

COPY --from=builder /install /usr/local

COPY . .

ENV HOST=0.0.0.0
ENV FARM_UI_PORT=8087
ENV OCPP16_URL=ws://localhost:9100/ocpp
ENV OCPP201_URL=ws://localhost:9201/ocpp

EXPOSE 8087

ENTRYPOINT ["python", "-u", "farm.py"]
