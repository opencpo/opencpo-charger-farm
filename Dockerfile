# ─── Build stage ─────────────────────────────────────────────
FROM python:3.12-slim AS builder

WORKDIR /build
COPY requirements.txt .
RUN pip install --no-cache-dir --prefix=/install -r requirements.txt

# ─── Runtime stage ───────────────────────────────────────────
FROM python:3.12-slim

WORKDIR /app

COPY --from=builder /install /usr/local

COPY control.py .
COPY charger16.py .
COPY charger201.py .
COPY profiles.py .
COPY physics.py .
COPY metrics.py .
COPY network.py .
COPY environment.py .
COPY pnc.py .
COPY scenarios.py .
COPY reports.py .
COPY static/ static/

ENV HOST=0.0.0.0
ENV PORT=8086
ENV OCPP16_URL=ws://localhost:9100/ocpp
ENV OCPP201_URL=ws://localhost:9201/ocpp

EXPOSE 8086

ENTRYPOINT ["python", "-u", "control.py"]
