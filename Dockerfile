FROM debian:bookworm-slim

ENV DEBIAN_FRONTEND=noninteractive \
    PANEL_DATA=/data \
    PANEL_PORT=8080 \
    STEAMCMD=/opt/steamcmd/steamcmd.sh \
    PYTHONUNBUFFERED=1

# steamcmd is 32-bit; TSRDedicated.exe is a 32-bit Windows exe run via Wine (win32 prefix) + virtual display.
RUN dpkg --add-architecture i386 \
 && apt-get update \
 && apt-get install -y --no-install-recommends \
      ca-certificates curl lib32gcc-s1 lib32stdc++6 libc6-i386 \
      wine wine32:i386 wine64 libwine libwine:i386 xvfb xauth procps tini \
      python3 python3-venv \
 && rm -rf /var/lib/apt/lists/* \
 && (command -v wine64 || command -v wine || test -x /usr/lib/wine/wine64)

RUN mkdir -p /opt/steamcmd \
 && curl -fsSL https://media.steampowered.com/client/installer/steamcmd_linux.tar.gz | tar -xz -C /opt/steamcmd

WORKDIR /app
COPY requirements.txt .
RUN python3 -m venv /opt/venv && /opt/venv/bin/pip install --no-cache-dir -r requirements.txt
COPY panel ./panel

# Unraid default user is nobody:users (99:100)
RUN mkdir -p /data /home/steam && chown -R 99:100 /data /home/steam /opt/steamcmd
ENV HOME=/home/steam
USER 99:100
VOLUME /data

# panel UI + The Ship ports (TCP/UDP 7776-7778, 443)
EXPOSE 8080/tcp 7776-7778/tcp 7776-7778/udp 443/tcp 443/udp
ENTRYPOINT ["/usr/bin/tini", "--"]
CMD ["/opt/venv/bin/python", "-m", "panel.main"]
