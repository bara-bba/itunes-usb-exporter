#!/usr/bin/env bash
# Runs INSIDE the container. Installs/updates the web app from git.
#   bash lxc-install.sh <owner/repo on GitHub | full git URL> [branch or tag]
# Called by install/usb-exporter.sh on the Proxmox host, by `update [ref]`,
# and by the Gitea Actions deploy workflow (`update <tag>`).
set -euo pipefail
SRC="${1:-bara-bba/itunes-usb-exporter}"
REF="${2:-main}"
APP_DIR=/opt/usb-exporter
case "$SRC" in
    *://*|*@*:*) URL="$SRC" ;;
    *)           URL="https://github.com/$SRC.git" ;;
esac

export DEBIAN_FRONTEND=noninteractive
command -v git >/dev/null && command -v python3 >/dev/null || {
    apt-get update -qq
    apt-get install -y -qq python3 git ca-certificates >/dev/null
}

if [ -d "$APP_DIR/.git" ]; then
    git -C "$APP_DIR" remote set-url origin "$URL"
else
    git clone -q "$URL" "$APP_DIR"
fi
git -C "$APP_DIR" fetch -q --tags --force origin "$REF"
git -C "$APP_DIR" reset -q --hard FETCH_HEAD

cat > /etc/systemd/system/usb-exporter.service <<'EOF'
[Unit]
Description=USB playlist exporter (web UI)
After=network-online.target

[Service]
EnvironmentFile=/etc/usb-exporter.env
ExecStart=/usr/bin/python3 /opt/usb-exporter/server/app.py
Restart=always
RestartSec=3

[Install]
WantedBy=multi-user.target
EOF

cat > /usr/local/bin/update <<EOF
#!/usr/bin/env bash
# update [branch|tag] - update the exporter from $URL (default: main)
set -e
bash /opt/usb-exporter/install/lxc-install.sh "$URL" "\${1:-main}"
EOF
chmod 755 /usr/local/bin/update

mkdir -p /mnt/music /mnt/usb /mnt/usb-ctl /etc/usb-exporter/fonts
systemctl daemon-reload
systemctl enable -q usb-exporter
systemctl restart usb-exporter
echo "usb-exporter: $REF = $(git -C "$APP_DIR" log --oneline -1)"
