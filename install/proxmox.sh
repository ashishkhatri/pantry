#!/usr/bin/env bash
# Create a Pantry container on Proxmox VE. Run this in the Proxmox host's shell:
#
#   bash -c "$(curl -fsSL https://raw.githubusercontent.com/ashishkhatri/pantry/main/install/proxmox.sh)"
#
# It makes a small unprivileged Debian 12 LXC (1 CPU, 512 MB RAM, 4 GB disk, DHCP on vmbr0),
# starts it on boot, and installs Pantry inside. Change the defaults with environment variables:
#
#   CTID=120 CT_HOSTNAME=pantry STORAGE=local-lvm TEMPLATE_STORAGE=local BRIDGE=vmbr0
#   CORES=1 MEMORY=512 DISK=4 IP=dhcp   (or IP=192.168.1.50/24 GW=192.168.1.1)
#   PANTRY_VERSION=v0.1.0 PANTRY_PORT=8790
set -euo pipefail

REPO="${PANTRY_REPO:-ashishkhatri/pantry}"
CTID="${CTID:-$(pvesh get /cluster/nextid 2>/dev/null || echo "")}"
HOSTNAME="${CT_HOSTNAME:-pantry}"
STORAGE="${STORAGE:-local-lvm}"
TEMPLATE_STORAGE="${TEMPLATE_STORAGE:-local}"
BRIDGE="${BRIDGE:-vmbr0}"
CORES="${CORES:-1}"
MEMORY="${MEMORY:-512}"
DISK="${DISK:-4}"
IP="${IP:-dhcp}"
GW="${GW:-}"
PORT="${PANTRY_PORT:-8790}"

say() { printf '\033[1;32m==\033[0m %s\n' "$*"; }
die() { printf '\033[1;31m!!\033[0m %s\n' "$*" >&2; exit 1; }

command -v pct >/dev/null || die "Run this on a Proxmox VE host (pct not found)."
[ "$(id -u)" -eq 0 ] || die "Run this as root."
[ -n "$CTID" ] || die "Couldn't pick a container ID. Set CTID=..."
pct status "$CTID" >/dev/null 2>&1 && die "Container $CTID already exists. Set CTID to a free ID."
pvesm status -storage "$STORAGE" >/dev/null 2>&1 || die "Storage '$STORAGE' not found. Set STORAGE=... (see: pvesm status)"

NET="name=eth0,bridge=$BRIDGE,ip=$IP"
[ -n "$GW" ] && NET="$NET,gw=$GW"

cat <<EOF

  Pantry container
  ----------------
  ID        $CTID
  Hostname  $HOSTNAME
  Resources $CORES CPU, ${MEMORY} MB RAM, ${DISK} GB disk on $STORAGE
  Network   $BRIDGE, $IP
  Port      $PORT

EOF
if [ -t 0 ] || [ -r /dev/tty ]; then
  read -r -p "Create it? [Y/n] " ok </dev/tty || ok=y
  case "${ok:-y}" in [nN]*) echo "Cancelled."; exit 0 ;; esac
fi

say "Finding the Debian 12 template"
pveam update >/dev/null 2>&1 || true
TEMPLATE="$(pveam available --section system 2>/dev/null | awk '{print $2}' | grep -E '^debian-12-standard_.*_amd64\.tar\.(zst|gz|xz)$' | sort -V | tail -n1)"
[ -n "$TEMPLATE" ] || die "Couldn't find a Debian 12 template (pveam available --section system)."
if ! pveam list "$TEMPLATE_STORAGE" 2>/dev/null | grep -q "$TEMPLATE"; then
  say "Downloading $TEMPLATE"
  pveam download "$TEMPLATE_STORAGE" "$TEMPLATE" >/dev/null
fi

say "Creating container $CTID"
pct create "$CTID" "$TEMPLATE_STORAGE:vztmpl/$TEMPLATE" \
  --hostname "$HOSTNAME" --cores "$CORES" --memory "$MEMORY" --swap 512 \
  --rootfs "$STORAGE:$DISK" --net0 "$NET" \
  --unprivileged 1 --features nesting=1 --onboot 1 --timezone host \
  --description "Pantry: household pantry tracker. https://github.com/$REPO" >/dev/null
pct start "$CTID"

say "Waiting for the network"
for _ in $(seq 1 30); do
  pct exec "$CTID" -- bash -c "getent hosts github.com >/dev/null 2>&1" && break
  sleep 2
done
pct exec "$CTID" -- bash -c "getent hosts github.com >/dev/null 2>&1" || die "Container $CTID has no internet access. Check its network settings."

say "Installing Pantry inside the container"
pct exec "$CTID" -- bash -c "apt-get update -qq && DEBIAN_FRONTEND=noninteractive apt-get install -y -qq curl ca-certificates >/dev/null \
  && curl -fsSL https://raw.githubusercontent.com/$REPO/main/install/linux.sh \
  | PANTRY_REPO='$REPO' PANTRY_PORT='$PORT' PANTRY_VERSION='${PANTRY_VERSION:-}' bash"

CTIP="$(pct exec "$CTID" -- hostname -I 2>/dev/null | awk '{print $1}')"
echo
say "Done. Pantry is at http://${CTIP:-<container-ip>}:$PORT"
echo "   Container $CTID ($HOSTNAME) starts on boot."
echo "   Update later from the Proxmox shell: pct exec $CTID -- pantry-update"
