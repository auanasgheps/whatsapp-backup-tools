# Archiver and viewer for servers: pick the command at run time.
#   wab-archiver archive --config /config/config.toml
#   wab-viewer /archive --host 0.0.0.0 --port 5000 --no-browser
FROM python:3.13-slim

# tzdata: IANA timezones for --timezone (slim images ship none)
COPY . /src
RUN pip install --no-cache-dir /src tzdata \
    && rm -rf /src \
    && useradd --uid 1000 --create-home wab

USER 1000:1000
WORKDIR /home/wab
EXPOSE 5000
