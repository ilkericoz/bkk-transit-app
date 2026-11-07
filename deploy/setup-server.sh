#!/usr/bin/env bash
# One-time setup of a fresh Oracle Cloud Ubuntu VM. Run as the default 'ubuntu' user.
set -euo pipefail

# Docker Engine + compose plugin (official convenience script)
curl -fsSL https://get.docker.com | sudo sh
sudo usermod -aG docker "$USER"

# Oracle's Ubuntu images ship iptables rules that drop everything except SSH,
# even after you open the ports in the cloud console. Allow HTTP/HTTPS.
sudo iptables -I INPUT 6 -m state --state NEW -p tcp --dport 80 -j ACCEPT
sudo iptables -I INPUT 6 -m state --state NEW -p tcp --dport 443 -j ACCEPT
sudo netfilter-persistent save

# Automatic security updates
sudo apt-get install -y unattended-upgrades
echo "Done. Log out and back in so the docker group applies."
