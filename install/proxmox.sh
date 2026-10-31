#!/usr/bin/env bash
# Pantry: create a Proxmox VE container and install Pantry inside.
# Run in the Proxmox host shell:
#
#   bash -c "$(curl -fsSL https://raw.githubusercontent.com/ashishkhatri/pantry/main/install/proxmox.sh)"
#
# You'll get the choice of default or advanced settings, like the community helper scripts.
# For unattended installs, set any of these and add NONINTERACTIVE=1:
#   CTID CT_HOSTNAME STORAGE TEMPLATE_STORAGE BRIDGE CORES MEMORY DISK IP GW PANTRY_VERSION PANTRY_PORT
set -euo pipefail

APP="Pantry"
REPO="${PANTRY_REPO:-ashishkhatri/pantry}"
OS_TEMPLATE_PATTERN='^debian-12-standard_.*_amd64\.tar\.(zst|gz|xz)$'

# ---------- look ----------
YW=$'\033[33m'; GN=$'\033[1;92m'; RD=$'\033[01;31m'; BL=$'\033[36m'; BGN=$'\033[4;92m'; DGN=$'\033[32m'
CL=$'\033[m'; BOLD=$'\033[1m'; TAB="  "
CM="${TAB}✔️${TAB}"; CROSS="${TAB}✖️${TAB}"; INFO="${TAB}💡${TAB}"
SPINNER_PID=""

header_info() {
  clear 2>/dev/null || true
  cat <<"EOF"
    ____              __
   / __ \____ _____  / /________  __
  / /_/ / __ `/ __ \/ __/ ___/ / / /
 / ____/ /_/ / / / / /_/ /  / /_/ /
/_/    \__,_/_/ /_/\__/_/   \__, /
                           /____/
EOF
  echo -e "${TAB}${DGN}Household pantry tracker · github.com/${REPO}${CL}\n"
}

spinner() {
  local frames=('⠋' '⠙' '⠹' '⠸' '⠼' '⠴' '⠦' '⠧' '⠇' '⠏') i=0
  while true; do printf "\r${TAB}%s${TAB}${YW}%s${CL}" "${frames[i]}" "$1"; i=$(( (i + 1) % 10 )); sleep 0.1; done
}
msg_info() { stop_spinner; if [ -t 1 ]; then spinner "$1" & SPINNER_PID=$!; else echo -e "${TAB}…${TAB}${YW}$1${CL}"; fi; }
stop_spinner() { if [ -n "$SPINNER_PID" ]; then kill "$SPINNER_PID" 2>/dev/null || true; wait "$SPINNER_PID" 2>/dev/null || true; SPINNER_PID=""; printf "\r\033[K"; fi; }
msg_ok() { stop_spinner; echo -e "${CM}${GN}$1${CL}"; }
msg_error() { stop_spinner; echo -e "${CROSS}${RD}$1${CL}" >&2; }
die() { msg_error "$1"; exit 3; }
trap 'rc=$?; stop_spinner; [ "$rc" -eq 3 ] || msg_error "Stopped at line $LINENO: $BASH_COMMAND"' ERR
trap 'stop_spinner' EXIT

INTERACTIVE=1
{ [ "${NONINTERACTIVE:-0}" = "1" ] || ! [ -r /dev/tty ] || ! command -v whiptail >/dev/null; } && INTERACTIVE=0
wt() { whiptail --backtitle "Pantry for Proxmox VE" "$@" 3>&1 1>&2 2>&3 </dev/tty; }

# ---------- checks ----------
header_info
command -v pct >/dev/null || die "Run this on a Proxmox VE host (pct not found)."
[ "$(id -u)" -eq 0 ] || die "Run this as root."
case "$(dpkg --print-architecture 2>/dev/null || uname -m)" in amd64|x86_64) ;; *) die "Only amd64 Proxmox hosts are supported." ;; esac

# ---------- storage ----------
# Storages that can hold container disks (content type rootdir) and templates (vztmpl).
list_storage() {  # $1 = content type -> "name type free" lines
  pvesm status -content "$1" 2>/dev/null | awk 'NR>1 && $3=="active" {printf "%s %s %.1fG\n", $1, $2, $6/1048576}'
}
pick_storage() {  # $1 = content type, $2 = label, $3 = preset value
  local content="$1" label="$2" preset="$3" lines n menu=() name type free
  if [ -n "$preset" ]; then
    list_storage "$content" | awk '{print $1}' | grep -qx "$preset" \
      || die "Storage '$preset' can't hold $label. Choose one of: $(list_storage "$content" | awk '{print $1}' | paste -sd' ')"
    echo "$preset"; return
  fi
  lines="$(list_storage "$content")"
  [ -n "$lines" ] || die "No active storage can hold $label. Enable that content type on a storage (Datacenter → Storage)."
  n=$(echo "$lines" | wc -l)
  if [ "$n" -eq 1 ] || [ "$INTERACTIVE" -eq 0 ]; then echo "$lines" | head -n1 | awk '{print $1}'; return; fi
  while read -r name type free; do menu+=("$name" "$type, $free free" "OFF"); done <<<"$lines"
  menu[2]="ON"
  wt --title "Storage" --radiolist "Where should the ${label} go?\n(Space selects, Enter confirms)" 16 60 6 "${menu[@]}" \
    || die "Cancelled."
}

# ---------- settings ----------
CTID="${CTID:-$(pvesh get /cluster/nextid)}"
CT_HOSTNAME="${CT_HOSTNAME:-pantry}"
CORES="${CORES:-1}"
MEMORY="${MEMORY:-512}"
DISK="${DISK:-4}"
BRIDGE="${BRIDGE:-vmbr0}"
IP="${IP:-dhcp}"
GW="${GW:-}"
PORT="${PANTRY_PORT:-8790}"
STORAGE="${STORAGE:-}"
TEMPLATE_STORAGE="${TEMPLATE_STORAGE:-}"

advanced_settings() {
  CTID=$(wt --title "Container ID" --inputbox "Container ID" 8 58 "$CTID") || die "Cancelled."
  CT_HOSTNAME=$(wt --title "Hostname" --inputbox "Hostname" 8 58 "$CT_HOSTNAME") || die "Cancelled."
  DISK=$(wt --title "Disk size" --inputbox "Disk size in GB" 8 58 "$DISK") || die "Cancelled."
  CORES=$(wt --title "CPU cores" --inputbox "CPU cores" 8 58 "$CORES") || die "Cancelled."
  MEMORY=$(wt --title "RAM" --inputbox "RAM in MB" 8 58 "$MEMORY") || die "Cancelled."
  BRIDGE=$(wt --title "Bridge" --inputbox "Network bridge" 8 58 "$BRIDGE") || die "Cancelled."
  IP=$(wt --title "IP address" --inputbox "\"dhcp\", or a static address with prefix, e.g. 192.168.1.50/24" 9 58 "$IP") || die "Cancelled."
  if [ "$IP" != "dhcp" ]; then
    GW=$(wt --title "Gateway" --inputbox "Gateway, e.g. 192.168.1.1" 8 58 "$GW") || die "Cancelled."
  fi
  PORT=$(wt --title "Port" --inputbox "Port Pantry listens on" 8 58 "$PORT") || die "Cancelled."
}

show_settings() {
  echo -e "${TAB}🆔${TAB}${BOLD}Container ID:${CL} ${BGN}${CTID}${CL}"
  echo -e "${TAB}🏠${TAB}${BOLD}Hostname:${CL} ${BGN}${CT_HOSTNAME}${CL}"
  echo -e "${TAB}🧠${TAB}${BOLD}Resources:${CL} ${BGN}${CORES} CPU, ${MEMORY} MB RAM, ${DISK} GB disk${CL}"
  echo -e "${TAB}💾${TAB}${BOLD}Storage:${CL} ${BGN}${STORAGE}${CL} (templates on ${BGN}${TEMPLATE_STORAGE}${CL})"
  echo -e "${TAB}🌉${TAB}${BOLD}Network:${CL} ${BGN}${BRIDGE}, ${IP}${GW:+ via $GW}${CL}"
  echo -e "${TAB}🔌${TAB}${BOLD}Port:${CL} ${BGN}${PORT}${CL}\n"
}

if [ "$INTERACTIVE" -eq 1 ]; then
  if wt --title "Pantry" --yesno "Use default settings?\n\n1 CPU, 512 MB RAM, 4 GB disk, DHCP on vmbr0, port 8790.\n\nChoose No for advanced settings." 13 58; then
    :
  else
    advanced_settings
  fi
fi
STORAGE="$(pick_storage rootdir "container disk" "$STORAGE")"
TEMPLATE_STORAGE="$(pick_storage vztmpl "Debian template" "$TEMPLATE_STORAGE")"

[[ "$CTID" =~ ^[0-9]+$ ]] || die "Container ID must be a number."
pct status "$CTID" >/dev/null 2>&1 && die "Container $CTID already exists. Pick another ID (advanced settings, or CTID=...)."
ip link show "$BRIDGE" >/dev/null 2>&1 || die "Network bridge '$BRIDGE' not found."

show_settings
if [ "$INTERACTIVE" -eq 1 ]; then
  wt --title "Pantry" --yesno "Create container ${CTID} (${CT_HOSTNAME}) on ${STORAGE}?" 9 58 || die "Cancelled."
fi

# ---------- template ----------
msg_info "Updating the template list"
pveam update >/dev/null 2>&1 || true
TEMPLATE="$(pveam available --section system 2>/dev/null | awk '{print $2}' | grep -E "$OS_TEMPLATE_PATTERN" | sort -V | tail -n1)"
[ -n "$TEMPLATE" ] || die "Couldn't find a Debian 12 template (pveam available --section system)."
msg_ok "Using ${TEMPLATE}"
if ! pveam list "$TEMPLATE_STORAGE" 2>/dev/null | grep -q "$TEMPLATE"; then
  msg_info "Downloading ${TEMPLATE}"
  pveam download "$TEMPLATE_STORAGE" "$TEMPLATE" >/dev/null
  msg_ok "Downloaded the template"
fi

# ---------- container ----------
NET="name=eth0,bridge=$BRIDGE,ip=$IP"
[ -n "$GW" ] && NET="$NET,gw=$GW"
msg_info "Creating container ${CTID}"
pct create "$CTID" "$TEMPLATE_STORAGE:vztmpl/$TEMPLATE" \
  --hostname "$CT_HOSTNAME" --cores "$CORES" --memory "$MEMORY" --swap 512 \
  --rootfs "$STORAGE:$DISK" --net0 "$NET" \
  --unprivileged 1 --features nesting=1 --onboot 1 --timezone host --tags pantry \
  --description "<div align='center'><h2>Pantry</h2><a href='https://github.com/$REPO'>github.com/$REPO</a></div>" >/dev/null
msg_ok "Created container ${CTID}"

msg_info "Starting the container"
pct start "$CTID"
for _ in $(seq 1 30); do
  pct exec "$CTID" -- bash -c "getent hosts github.com >/dev/null 2>&1" && break
  sleep 2
done
pct exec "$CTID" -- bash -c "getent hosts github.com >/dev/null 2>&1" \
  || die "Container ${CTID} started but has no internet access. Check its network settings."
msg_ok "Container is online"

msg_info "Installing Pantry (a minute or two)"
LOG="/tmp/pantry-install-${CTID}.log"
if ! pct exec "$CTID" -- bash -c "apt-get update -qq && DEBIAN_FRONTEND=noninteractive apt-get install -y -qq curl ca-certificates >/dev/null \
    && curl -fsSL https://raw.githubusercontent.com/$REPO/main/install/linux.sh \
    | PANTRY_REPO='$REPO' PANTRY_PORT='$PORT' PANTRY_VERSION='${PANTRY_VERSION:-}' bash" >"$LOG" 2>&1; then
  die "Installing Pantry failed. Details: $LOG"
fi
msg_ok "Installed $(grep -o 'Pantry [0-9][^ ]* is running' "$LOG" | head -n1 | sed 's/ is running//' || echo Pantry)"

CTIP="$(pct exec "$CTID" -- hostname -I 2>/dev/null | awk '{print $1}')"
echo
msg_ok "Completed successfully!"
echo -e "${INFO}${YW}Pantry is reachable at:${CL}"
echo -e "${TAB}🌐${TAB}${BGN}http://${CTIP:-<container-ip>}:${PORT}${CL}"
echo -e "${INFO}${YW}For the phone camera and notifications, put it behind https (reverse proxy or Tailscale).${CL}"
echo -e "${INFO}${YW}Update later:${CL} pct exec ${CTID} -- pantry-update"
