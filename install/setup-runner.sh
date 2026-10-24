#!/usr/bin/env bash
# Runs INSIDE the usb-exporter container. Installs Gitea's act_runner in host
# mode (no Docker) so tag pushes deploy here and pushes mirror to GitHub.
# Idempotent: re-running keeps an existing registration (delete .runner to
# re-register). Register with an INSTANCE-level token
# (gitea actions generate-runner-token, no --scope).
#   bash /opt/usb-exporter/install/setup-runner.sh <gitea-url> <registration-token>
set -euo pipefail

GITEA_URL="${1:?gitea url, e.g. http://192.168.42.73:3000}"
TOKEN="${2:-}"
VERSION=0.2.13
SHA256=3acac8b506ac8cadc88a55155b5d6378f0fab0b8f62d1e0c0450f4ccd69733e2
DIR=/var/lib/act_runner
BIN=/usr/local/bin/act_runner
LABEL=usb-exporter

command -v curl >/dev/null && command -v ssh >/dev/null \
    || { apt-get update -qq; apt-get install -y -qq curl openssh-client >/dev/null; }   # ssh: GitHub mirror
if ! "$BIN" --version 2>/dev/null | grep -q "$VERSION"; then
    echo "==> installing act_runner $VERSION"
    curl -fsSL "https://dl.gitea.com/act_runner/$VERSION/act_runner-$VERSION-linux-amd64" -o /tmp/act_runner
    echo "$SHA256  /tmp/act_runner" | sha256sum -c --quiet
    install -m 755 /tmp/act_runner "$BIN"
    rm -f /tmp/act_runner
fi

mkdir -p "$DIR"
cd "$DIR"
cat > config.yaml <<EOF
log:
  level: info
runner:
  file: .runner
  capacity: 1
  timeout: 1h
  labels:
    - "$LABEL:host"
host:
  workdir_parent: /var/lib/act_runner/work
EOF

if [ ! -f .runner ]; then
    [ -n "$TOKEN" ] || { echo "no registration yet: pass the runner token as 2nd argument" >&2; exit 1; }
    echo "==> registering with $GITEA_URL"
    "$BIN" register --no-interactive --config config.yaml \
        --instance "$GITEA_URL" --token "$TOKEN" --name "$LABEL" --labels "$LABEL:host"
fi

cat > /etc/systemd/system/act_runner.service <<EOF
[Unit]
Description=Gitea Actions runner ($LABEL deploys + GitHub mirror)
After=network-online.target
Wants=network-online.target

[Service]
WorkingDirectory=$DIR
# Without HOME the runner builds relative cache paths and host-mode steps fail.
Environment=HOME=/root
ExecStart=$BIN daemon --config $DIR/config.yaml
Restart=always
RestartSec=5

[Install]
WantedBy=multi-user.target
EOF
systemctl daemon-reload
systemctl enable -q --now act_runner
systemctl restart act_runner
sleep 2
systemctl is-active act_runner && echo "act_runner $VERSION running as label $LABEL"
