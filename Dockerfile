FROM python:3.13-slim
ENV PYTHONDONTWRITEBYTECODE=1 PYTHONUNBUFFERED=1 DATA_DIR=/app/data MPLCONFIGDIR=/tmp/matplotlib
WORKDIR /app
COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt \
    && useradd --uid 10001 --create-home bot \
    && mkdir /app/data && chown bot:bot /app/data
COPY bot ./bot
COPY scripts ./scripts
USER 10001:10001
CMD ["python", "-m", "bot.main"]
