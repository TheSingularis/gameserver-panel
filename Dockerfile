FROM debian:bookworm-slim

# Set by CI so the UI can show which build is running (see ci.yml); "dev" for local builds
ARG PANEL_COMMIT=dev
ARG PANEL_BUILT=
ENV DEBIAN_FRONTEND=noninteractive \
    PANEL_COMMIT=$PANEL_COMMIT \
    PANEL_BUILT=$PANEL_BUILT \
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

# Minecraft needs a different Java per game version (8 for <=1.16, 17 for 1.18-1.20.4, 21 for 1.20.5-1.21.x, 25 for 26.x), and
# Debian bookworm only ships 17, so install Temurin JREs side by side as /opt/java/<major>; the panel picks one per server.
RUN case "$(uname -m)" in x86_64) A=x64;; aarch64) A=aarch64;; *) echo "no Java build for $(uname -m)" >&2; exit 1;; esac \
 && for V in 8 17 21 25; do \
      mkdir -p /opt/java/$V \
      && curl -fsSL "https://api.adoptium.net/v3/binary/latest/$V/ga/linux/${A}/jre/hotspot/normal/eclipse" | tar -xz -C /opt/java/$V --strip-components=1 \
      && /opt/java/$V/bin/java -version; \
    done
ENV PATH=/opt/java/25/bin:$PATH

WORKDIR /app
COPY requirements.txt .
RUN python3 -m venv /opt/venv && /opt/venv/bin/pip install --no-cache-dir -r requirements.txt
COPY panel ./panel

# Unraid default user is nobody:users (99:100)
RUN mkdir -p /data /home/steam && chown -R 99:100 /data /home/steam /opt/steamcmd
ENV HOME=/home/steam
USER 99:100
VOLUME /data

# panel UI + The Ship ports (TCP/UDP 7776-7778) + Minecraft (TCP 25565)
EXPOSE 8080/tcp 7776-7778/tcp 7776-7778/udp 25565/tcp
ENTRYPOINT ["/usr/bin/tini", "--"]
CMD ["/opt/venv/bin/python", "-m", "panel.main"]
