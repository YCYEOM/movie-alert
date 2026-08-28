#!/usr/bin/env bash
# 서버에서 한 번 실행하면 상시 감시가 켜진다.
#   curl -fsSL https://raw.githubusercontent.com/YCYEOM/movie-alert/main/setup.sh | sudo bash -s -- '<디스코드 웹훅 URL>'
set -euo pipefail

REPO=https://github.com/YCYEOM/movie-alert.git
DIR=/opt/movie-alert
WEBHOOK=${1:-}

[ "$(id -u)" -eq 0 ] || { echo "sudo 로 실행하세요"; exit 1; }
[ -n "$WEBHOOK" ] || { echo "사용법: sudo bash setup.sh '<디스코드 웹훅 URL>'"; exit 1; }

echo "== 패키지 설치"
if command -v dnf >/dev/null; then dnf install -y git python3
else apt-get update -qq && apt-get install -y git python3; fi

echo "== 코드 배치"
if [ -d "$DIR/.git" ]; then git -C "$DIR" pull --ff-only; else git clone --depth 1 "$REPO" "$DIR"; fi

echo "== 전용 계정"
id movie-alert >/dev/null 2>&1 || useradd -r -M -d "$DIR" -s /sbin/nologin movie-alert
install -d -o movie-alert -g movie-alert /var/lib/movie-alert
chown -R movie-alert:movie-alert "$DIR"

echo "== 웹훅 등록"
umask 077
printf 'DISCORD_WEBHOOK_URL=%s\nSTATE_PATH=/var/lib/movie-alert/state.json\n' "$WEBHOOK" > /etc/movie-alert.env
chown movie-alert:movie-alert /etc/movie-alert.env
chmod 600 /etc/movie-alert.env

echo "== 서비스 등록"
install -m 644 "$DIR/movie-alert.service" /etc/systemd/system/movie-alert.service
systemctl daemon-reload
systemctl enable --now movie-alert

echo "== 상태"
sleep 3
systemctl --no-pager -l status movie-alert | head -12
echo
echo "로그 보기 : journalctl -u movie-alert -f"
echo "재시작    : systemctl restart movie-alert"
