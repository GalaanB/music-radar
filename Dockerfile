FROM python:3.12-slim

WORKDIR /app
ENV PYTHONUNBUFFERED=1 TZ=Europe/Paris

COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt

COPY radar.py bot.py ./
# artists.txt et data/ sont montés en volume (voir docker-compose.yml).

CMD ["python", "bot.py"]
