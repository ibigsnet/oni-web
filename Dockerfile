FROM node:24-bookworm-slim AS frontend
WORKDIR /build
COPY frontend/package*.json ./
RUN npm ci --no-audit --no-fund
COPY frontend/ ./
RUN npm run build

FROM python:3.12-slim
RUN apt-get update && apt-get install -y --no-install-recommends openssh-client \
    && rm -rf /var/lib/apt/lists/* \
    && (groupadd -f operator && useradd --create-home --uid 10001 -g operator operator)
WORKDIR /app
COPY requirements.txt ./
RUN pip install --no-cache-dir -r requirements.txt
COPY backend/ ./backend/
COPY run.py LICENSE NOTICE ./
COPY --from=frontend /build/dist ./frontend/dist/
RUN mkdir /app/data && chown operator:operator /app/data
USER operator
ENV ONI_WEB_HOST=0.0.0.0 ONI_WEB_PORT=8000 ONI_WEB_DATA_DIR=/app/data
EXPOSE 8000
CMD ["python", "run.py"]
