#!/usr/bin/env bash
# Install or update Pantry on a Debian/Ubuntu machine or container (run as root).
#
#   curl -fsSL https://raw.githubusercontent.com/ashishkhatri/pantry/main/install/linux.sh | bash
#
# or, from a downloaded copy of the project:  bash install/linux.sh
#
# Code goes to /opt/pantry, data to /var/lib/pantry (kept across updates), and a "pantry"
# systemd service listens on port 8790. Run it again to update to the latest release;
# `pantry-update` does the same.
#
# Options (environment variables):
#   PANTRY_VERSION=v0.2.0   install a specific release tag (default: latest release)
#   PANTRY_PORT=8790        port to listen on
#   PANTRY_REPO=owner/repo  install from a fork
set -euo pipefail

REPO="${PANTRY_REPO:-ashishkhatri/pantry}"
APP=/opt/pantry
DATA=/var/lib/pantry
PORT="${PANTRY_PORT:-8790}"

say() { printf '\033[1;32m==\033[0m %s\n' "$*"; }
die() { printf '\033[1;31m!!\033[0m %s\n' "$*" >&2; exit 1; }

[ "$(id -u)" -eq 0 ] || die "Run this as root (or with sudo)."
command -v apt-get >/dev/null || die "This installer needs a Debian or Ubuntu system. For anything else, use Docker."

say "Installing packages"
export DEBIAN_FRONTEND=noninteractive
apt-get update -qq
apt-get install -y -qq python3 python3-venv curl ca-certificates tar >/dev/null

# Where the code comes from: this checkout, or a release download.
HERE="$(cd "$(dirname "${BASH_SOURCE[0]:-$0}")" 2>/dev/null && pwd || true)"
TMP=""
if [ -n "$HERE" ] && [ -f "$HERE/../pantry/__main__.py" ]; then
  SRC="$(cd "$HERE/.." && pwd)"
  say "Installing from $SRC"
else
  TAG="${PANTRY_VERSION:-}"
  if [ -z "$TAG" ]; then
    TAG="$(curl -fsSL "https://api.github.com/repos/$REPO/releases/latest" 2>/dev/null \
           | sed -n 's/.*"tag_name": *"\([^"]*\)".*/\1/p' | head -n1 || true)"
  fi
  TMP="$(mktemp -d)"
  if [ -n "$TAG" ]; then
    say "Downloading Pantry $TAG"
    URL="https://github.com/$REPO/archive/refs/tags/$TAG.tar.gz"
  else
    say "No release found; downloading the main branch"
    URL="https://github.com/$REPO/archive/refs/heads/main.tar.gz"
  fi
  curl -fsSL "$URL" | tar -xz -C "$TMP" || die "Download failed: $URL"
  SRC="$(find "$TMP" -mindepth 1 -maxdepth 1 -type d | head -n1)"
fi

say "Copying the app to $APP"
id pantry >/dev/null 2>&1 || useradd --system --home "$DATA" --shell /usr/sbin/nologin pantry
mkdir -p "$APP" "$DATA"
# Replace the code but keep the Python environment, so updates are quick.
find "$APP" -mindepth 1 -maxdepth 1 ! -name .venv -exec rm -rf {} +
(cd "$SRC" && tar --exclude=./data --exclude=./.venv --exclude=./.git --exclude='__pycache__' -cf - .) | tar -xf - -C "$APP"
chown -R pantry:pantry "$DATA"
[ -n "$TMP" ] && rm -rf "$TMP"

say "Setting up Python"
[ -x "$APP/.venv/bin/python" ] || python3 -m venv "$APP/.venv"
"$APP/.venv/bin/pip" install -q --disable-pip-version-check -r "$APP/requirements.txt"

say "Starting the service"
sed -e "s|@PORT@|$PORT|" "$APP/install/pantry.service" > /etc/systemd/system/pantry.service
cat > /usr/local/bin/pantry-update <<EOF
#!/usr/bin/env bash
# Update Pantry to the latest release (your data is kept).
curl -fsSL https://raw.githubusercontent.com/$REPO/main/install/linux.sh | PANTRY_PORT=$PORT PANTRY_REPO=$REPO bash
EOF
chmod +x /usr/local/bin/pantry-update
systemctl daemon-reload
systemctl enable pantry >/dev/null 2>&1
systemctl restart pantry

for _ in $(seq 1 20); do
  curl -fsS "http://127.0.0.1:$PORT/healthz" >/dev/null 2>&1 && break
  sleep 1
done
curl -fsS "http://127.0.0.1:$PORT/healthz" >/dev/null 2>&1 || die "Pantry didn't start. Check: journalctl -u pantry -n 50"

IP="$(hostname -I 2>/dev/null | awk '{print $1}')"
VERSION="$("$APP/.venv/bin/python" -c 'import sys; sys.path.insert(0, "/opt/pantry"); import pantry; print(pantry.__version__)')"
say "Pantry $VERSION is running at http://${IP:-this-machine}:$PORT"
echo "   Open it to create the first account. For the phone camera and notifications,"
echo "   put it behind https (a reverse proxy such as Nginx Proxy Manager, Caddy or Tailscale)."
echo "   Update later with: pantry-update"
