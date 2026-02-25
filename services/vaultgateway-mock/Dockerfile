FROM python:3.12-slim AS builder
WORKDIR /build
COPY requirements.txt .
RUN pip install --no-cache-dir --prefix=/install -r requirements.txt

FROM python:3.12-slim
RUN groupadd -r appuser && useradd -r -g appuser -u 1000 appuser
WORKDIR /app
COPY --from=builder /install /usr/local
COPY app/ ./app/
USER 1000
EXPOSE 8080
ENTRYPOINT ["python", "-m", "uvicorn", "app.main:app", "--host", "0.0.0.0", "--port", "8080"]
