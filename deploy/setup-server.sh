#!/usr/bin/env bash
# Подготовка чистого сервера Ubuntu (проверено на Oracle Cloud Free Tier).
# Запуск: sudo bash deploy/setup-server.sh
set -euo pipefail

# 1. Файл подкачки на 2 ГБ — на машинах с 1 ГБ памяти без него может не хватить памяти при сборке
if ! swapon --show | grep -q /swapfile; then
  fallocate -l 2G /swapfile
  chmod 600 /swapfile
  mkswap /swapfile
  swapon /swapfile
  echo '/swapfile none swap sw 0 0' >> /etc/fstab
fi

# 2. Docker
if ! command -v docker >/dev/null; then
  curl -fsSL https://get.docker.com | sh
fi
usermod -aG docker "${SUDO_USER:-ubuntu}"

# 3. Порты 80 и 443. В образах Ubuntu от Oracle входящие соединения закрыты iptables.
for port in 80 443; do
  if ! iptables -C INPUT -p tcp --dport "$port" -m state --state NEW -j ACCEPT 2>/dev/null; then
    iptables -I INPUT 5 -p tcp --dport "$port" -m state --state NEW -j ACCEPT
  fi
done
if command -v netfilter-persistent >/dev/null; then
  netfilter-persistent save
fi

echo "Сервер готов. Перезайдите по SSH, затем: cd approval-bot && docker compose up -d --build"
