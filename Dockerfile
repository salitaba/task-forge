FROM node:24-slim

RUN apt-get update \
    && apt-get install -y --no-install-recommends python3 git openssh-client ca-certificates \
    && npm install -g @openai/codex \
    && npm cache clean --force \
    && rm -rf /var/lib/apt/lists/*

WORKDIR /app
COPY . /app

ENV PYTHONUNBUFFERED=1
EXPOSE 8080

CMD ["python3", "-m", "taskforge", "serve"]
