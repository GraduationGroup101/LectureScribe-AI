#!/usr/bin/env bash
set -euo pipefail

# Install an uploaded release on an Ubuntu VM; state stays outside the code tree.
if [[ $EUID -ne 0 || $# -ne 2 ]]; then
    echo "Usage: sudo bash install.sh /absolute/release.tar.gz /absolute/app.env" >&2
    exit 1
fi
source /etc/os-release
if [[ $ID != ubuntu || $VERSION_ID != 24.04 ]]; then
    echo "This installer requires Ubuntu 24.04." >&2
    exit 1
fi
archive=$(realpath "$1")
env_file=$(realpath "$2")
[[ -f $archive && -s $env_file ]] || exit 1
if (( $(awk '/MemTotal/ {print $2}' /proc/meminfo) < 10000000 )); then
    echo "Use the A1 VM with 12 GB RAM, not the 1 GB micro VM." >&2
    exit 1
fi

# Updating while a worker is busy would interrupt a lecture.
check_idle() {
    if ! systemctl is-active --quiet lecturescribe; then
        return
    fi
    curl --fail --silent http://127.0.0.1:8000/jobs | python3 -c '
import json, sys
payload = json.load(sys.stdin)
jobs = payload.get("jobs", [])
if any(job.get("status") in {"queued", "running"} for job in jobs):
    sys.exit("Wait for running/queued jobs before deploying.")
'
}
check_idle

export DEBIAN_FRONTEND=noninteractive
apt-get update
apt-get install -y python3 python3-venv ffmpeg curl ca-certificates unzip
work=$(mktemp -d)
trap 'rm -rf -- "$work"' EXIT
case $(dpkg --print-architecture) in
    arm64) deno_target=aarch64-unknown-linux-gnu ;;
    amd64) deno_target=x86_64-unknown-linux-gnu ;;
    *) echo "Unsupported CPU architecture." >&2; exit 1 ;;
esac
curl --fail --location --retry 3 \
    "https://github.com/denoland/deno/releases/latest/download/deno-${deno_target}.zip" \
    --output "$work/deno.zip"
unzip -q "$work/deno.zip" -d "$work/deno"
install -m 0755 "$work/deno/deno" /usr/local/bin/deno
curl --fail --location --retry 3 \
    "https://github.com/cloudflare/cloudflared/releases/latest/download/cloudflared-linux-$(dpkg --print-architecture).deb" \
    --output "$work/cloudflared.deb"
dpkg -i "$work/cloudflared.deb"

id lecturescribe >/dev/null 2>&1 || \
    useradd --system --user-group --home-dir /var/lib/lecturescribe --shell /usr/sbin/nologin lecturescribe
install -d -m 0755 /opt/lecturescribe
install -d -m 0750 -o lecturescribe -g lecturescribe \
    /var/lib/lecturescribe /var/cache/lecturescribe
install -d -m 0750 -o root -g lecturescribe /etc/lecturescribe
# Downloads/install preparation can take time; recheck just before shutdown.
check_idle
systemctl stop lecturescribe.service 2>/dev/null || true
tar --extract --gzip --file "$archive" --directory /opt/lecturescribe --no-same-owner
chmod -R go-w /opt/lecturescribe
python3 -m venv /opt/lecturescribe/.venv
/opt/lecturescribe/.venv/bin/pip install --upgrade pip
/opt/lecturescribe/.venv/bin/pip install -r /opt/lecturescribe/requirements.txt
# The companion EJS package is needed in addition to the Deno runtime.
/opt/lecturescribe/.venv/bin/pip install 'yt-dlp[default]>=2026.8.19'
install -m 0640 -o root -g lecturescribe "$env_file" /etc/lecturescribe/app.env

# Only adapt the server's copy of .env. Never echo API keys or change laptop files.
/opt/lecturescribe/.venv/bin/python - /etc/lecturescribe/app.env <<'PY'
from pathlib import Path
import sys

path = Path(sys.argv[1])
lines = path.read_text(encoding="utf-8-sig").splitlines()
server_keys = {"WHISPER_BACKEND", "WHISPER_DEVICE", "WHISPER_COMPUTE_TYPE",
               "WHISPER_LOCAL_FALLBACK", "WHISPER_MODEL_PATH", "FFMPEG_BIN"}
lines = [line for line in lines if line.split("=", 1)[0].strip() not in server_keys]
lines += ["WHISPER_BACKEND=openrouter", "WHISPER_DEVICE=cpu",
          "WHISPER_COMPUTE_TYPE=int8", "WHISPER_LOCAL_FALLBACK=true"]
path.write_text("\n".join(lines) + "\n", encoding="utf-8")
PY
ln -sfn /opt/lecturescribe/front /var/lib/lecturescribe/front
ln -sfn /etc/lecturescribe/app.env /var/lib/lecturescribe/.env
for directory in downloads OutputForWhisper OutputForOllama; do
    install -d -m 0750 -o lecturescribe -g lecturescribe "/var/lib/lecturescribe/$directory"
done
install -m 0644 /opt/lecturescribe/deploy/oracle/lecturescribe.service \
    /etc/systemd/system/lecturescribe.service

# Load and test from the actual service working directory, without live API calls.
cd /var/lib/lecturescribe
runuser -u lecturescribe -- env \
    PYTHONPATH=/opt/lecturescribe PYTHONDONTWRITEBYTECODE=1 \
    /opt/lecturescribe/.venv/bin/python -m unittest discover \
    -s /opt/lecturescribe/tests -v
runuser -u lecturescribe -- env PYTHONPATH=/opt/lecturescribe PYTHONDONTWRITEBYTECODE=1 \
    /opt/lecturescribe/.venv/bin/python -c 'import api; print("API import OK")'
systemctl daemon-reload
systemctl enable --now lecturescribe.service
for attempt in {1..30}; do
    if curl --fail --silent http://127.0.0.1:8000/health; then
        printf '\nAPI ready. Configure the Cloudflare connector before switching the domain.\n'
        exit 0
    fi
    sleep 2
done
echo "API health check failed. Inspect: journalctl -u lecturescribe -n 100" >&2
exit 1
