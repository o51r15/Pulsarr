FROM mcr.microsoft.com/powershell:7-ubuntu-22.04

LABEL org.opencontainers.image.title="Trackarr" \
      org.opencontainers.image.description="Automated BitTorrent tracker management for qBittorrent" \
      org.opencontainers.image.source="https://github.com/o51r15/trackarr"

WORKDIR /app

COPY trackerping.ps1        .
COPY tracker-discovery.ps1  .
COPY trackarr-bridge.ps1    .
COPY trackarr-gui.html      .
COPY tracker_urls.txt       .

# Runtime directories
RUN mkdir -p /app/tracker-data /data

# Default bridge port config
RUN echo '{"port":7374}' > /app/bridge-config.json

EXPOSE 7374

# /app/tracker-data  - sleep, history, sources, cache
# /data              - config dir (trackerping.log, combined_raw.txt, active_raw.txt, working_trackers.txt)
VOLUME ["/app/tracker-data", "/data"]

ENTRYPOINT ["pwsh", "-NonInteractive", "-ExecutionPolicy", "Bypass", "-File", "/app/trackarr-bridge.ps1"]
