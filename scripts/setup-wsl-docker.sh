#!/usr/bin/env bash
# Run as root in an E:-backed Ubuntu WSL2 distribution.
set -euo pipefail
if [[ ${EUID} -ne 0 ]]; then echo 'Run with sudo or wsl -u root.' >&2; exit 1; fi
if ! grep -qi microsoft /proc/sys/kernel/osrelease; then echo 'This setup targets WSL2.' >&2; exit 1; fi
apt-get update
apt-get install -y ca-certificates curl gnupg
install -m 0755 -d /etc/apt/keyrings
curl -fsSL https://download.docker.com/linux/ubuntu/gpg -o /etc/apt/keyrings/docker.asc
chmod a+r /etc/apt/keyrings/docker.asc
source /etc/os-release
printf 'deb [arch=%s signed-by=/etc/apt/keyrings/docker.asc] https://download.docker.com/linux/ubuntu %s stable\n' "$(dpkg --print-architecture)" "$VERSION_CODENAME" > /etc/apt/sources.list.d/docker.list
curl -fsSL https://nvidia.github.io/libnvidia-container/gpgkey | gpg --dearmor --yes -o /usr/share/keyrings/nvidia-container-toolkit-keyring.gpg
curl -fsSL https://nvidia.github.io/libnvidia-container/stable/deb/nvidia-container-toolkit.list | sed 's#deb https://#deb [signed-by=/usr/share/keyrings/nvidia-container-toolkit-keyring.gpg] https://#' > /etc/apt/sources.list.d/nvidia-container-toolkit.list
apt-get update
# Prevent package installation from starting a daemon on Desktop's socket.
if [[ -e /usr/sbin/policy-rc.d ]]; then
  echo 'Existing policy-rc.d: inspect it before running this installer.' >&2; exit 1
fi
printf '#!/bin/sh\nexit 101\n' > /usr/sbin/policy-rc.d
chmod +x /usr/sbin/policy-rc.d
trap 'rm -f /usr/sbin/policy-rc.d' EXIT
apt-get install -y docker-ce docker-ce-cli containerd.io docker-buildx-plugin docker-compose-plugin nvidia-container-toolkit
rm -f /usr/sbin/policy-rc.d
trap - EXIT
systemctl disable docker.service docker.socket
install -d /etc/docker /var/lib/persona-docker
if [[ ! -e /etc/docker/persona-daemon.json ]]; then
  cat > /etc/docker/persona-daemon.json <<'JSON'
{
  "data-root": "/var/lib/persona-docker",
  "hosts": ["unix:///run/persona-docker.sock"],
  "bip": "10.231.0.1/24",
  "default-address-pools": [{"base": "10.232.0.0/16", "size": 24}],
  "log-driver": "local",
  "log-opts": {"max-size": "10m", "max-file": "3"}
}
JSON
fi
nvidia-ctk runtime configure --runtime=docker --config=/etc/docker/persona-daemon.json
cat > /etc/systemd/system/persona-docker.service <<'UNIT'
[Unit]
Description=Independent Docker Engine for Persona Graph AI in WSL
After=network-online.target
Wants=network-online.target
[Service]
Type=notify
ExecStart=/usr/bin/dockerd --config-file=/etc/docker/persona-daemon.json --exec-root=/run/persona-docker --pidfile=/run/persona-docker.pid
Restart=on-failure
RestartSec=5
Delegate=yes
LimitNOFILE=infinity
TasksMax=infinity
[Install]
WantedBy=multi-user.target
UNIT
systemctl daemon-reload
systemctl enable --now persona-docker
docker --host unix:///run/persona-docker.sock info --format '{{.Name}}: {{.DockerRootDir}}'
