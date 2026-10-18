#!/usr/bin/env bash
# Runs INSIDE the container (pushed and started by install/usb-exporter.sh on
# the Proxmox host). Installs/updates the web app from GitHub.
#   bash lxc-install.sh <owner/repo> <branch>
set -euo pipefail
REPO="${1:-bara-bba/itunes-usb-exporter}"
BRANCH="${2:-main}"
APP_DIR=/opt/usb-exporter

export DEBIAN_FRONTEND=noninteractive
apt-get update -qq
apt-get install -y -qq python3 git ca-certificates >/dev/null

if [ -d "$APP_DIR/.git" ]; then
    git -C "$APP_DIR" fetch -q origin "$BRANCH"
    git -C "$APP_DIR" reset -q --hard "origin/$BRANCH"
else
    git clone -q --branch "$BRANCH" "https://github.com/$REPO.git" "$APP_DIR"
fi

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
# Update the exporter to the latest $BRANCH from GitHub.
set -e
bash /opt/usb-exporter/install/lxc-install.sh "$REPO" "$BRANCH"
echo "usb-exporter updated to \$(git -C /opt/usb-exporter log --oneline -1)"
EOF
chmod 755 /usr/local/bin/update

mkdir -p /mnt/music /mnt/usb /mnt/usb-ctl
systemctl daemon-reload
systemctl enable -q usb-exporter
systemctl restart usb-exporter
echo "usb-exporter: $(git -C "$APP_DIR" log --oneline -1)"
