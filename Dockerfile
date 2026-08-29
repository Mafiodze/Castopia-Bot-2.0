FROM python:3.12-slim

ENV PYTHONDONTWRITEBYTECODE=1     PYTHONUNBUFFERED=1     PIP_NO_CACHE_DIR=1

WORKDIR /app

RUN apt-get update     && apt-get install -y --no-install-recommends         build-essential         libxml2-dev         libxslt1-dev     && rm -rf /var/lib/apt/lists/*

COPY requirements.txt ./

RUN python -m pip install --no-cache-dir -r requirements.txt

COPY cogs ./cogs
COPY dsc ./dsc
COPY tg ./tg
COPY familiarbot ./familiarbot
COPY tests ./tests
COPY .env.example .gitignore LICENSE.txt README.md      docker-compose.yml railway.json runtime.txt start.sh start.bat health_check.py ./

RUN chmod +x /app/start.sh

CMD ["./start.sh"]
