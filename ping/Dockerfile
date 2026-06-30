FROM python:3.11-alpine

LABEL org.opencontainers.image.title="Trackarr Ping" \
      org.opencontainers.image.description="BitTorrent tracker connectivity checker — part of Trackarr" \
      org.opencontainers.image.source="https://github.com/o51r15/trackarr"

WORKDIR /app

COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt

COPY trackerping.py /usr/local/bin/trackerping
RUN chmod +x /usr/local/bin/trackerping

# /data is mounted from the host by trackerping.ps1:
#   docker run -v "$configDir:/data" local-trackerping trackerping -l -o /data/working_trackers.txt /data/active_raw.txt
VOLUME ["/data"]

ENTRYPOINT []
CMD ["trackerping", "--help"]
