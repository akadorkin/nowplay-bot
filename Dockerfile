FROM python:3.13-alpine
RUN adduser -D -h /app bot && mkdir /data && chown bot /data
USER bot
WORKDIR /app
COPY nowplay.py .
ENV NOWPLAY_STATE=/data/state.json PYTHONUNBUFFERED=1
VOLUME /data
CMD ["python3", "nowplay.py"]
