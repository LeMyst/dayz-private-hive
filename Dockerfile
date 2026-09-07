FROM python:3.14-alpine

COPY hive.py config.py mission.py wire.py /app/

# Mount the DayZ server's mpmissions folder at /data/mpmissions and
# a volume for /data/hive_store. Settings come from HIVE_* environment variables.
RUN adduser -D -u 1000 hive && mkdir -p /data/hive_store && chown -R hive /data
USER hive
WORKDIR /data
EXPOSE 8080

ENTRYPOINT ["python", "/app/hive.py", "--host", "0.0.0.0"]
