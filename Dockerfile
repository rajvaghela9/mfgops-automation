# syntax=docker/dockerfile:1

FROM python:3.12-slim

WORKDIR /app

RUN useradd --system --no-create-home appuser

COPY requirements.txt ./
RUN pip install --no-cache-dir -r requirements.txt

COPY src ./src

USER appuser
EXPOSE 8080
ENTRYPOINT ["python", "-m", "src.app"]
