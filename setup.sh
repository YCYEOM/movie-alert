#!/usr/bin/env bash
# 서버에서 한 번 실행하면 상시 감시가 켜진다. 재실행하면 최신 코드로 갱신된다.
#   curl -fsSL https://raw.githubusercontent.com/YCYEOM/movie-alert/main/setup.sh | sudo bash -s -- '<디스코드 웹훅 URL>'
#
# 패키지를 설치하지 않는다. Oracle E2.1.Micro(1 OCPU/1GB)에서 dnf 가 메모리를 다
# 먹고 sshd 까지 멈춰버려서, 기본 탑재된 curl + python3 만 쓰도록 바꿨다.
set -euo pipefail

TARBALL=https://codeload.github.com/YCYEOM/movie-alert/tar.gz/refs/heads/main
DIR=/opt/movie-alert
WEBHOOK=${1:-}

[ "$(id -u)" -eq 0 ] || { echo "sudo 로 실행하세요"; exit 1; }
[ -n "$WEBHOOK" ] || { echo "사용법: sudo bash setup.sh '<디스코드 웹훅 URL>'"; exit 1; }
command -v python3 >/dev/null || { echo "python3 가 없습니다"; exit 1; }

echo "== 코드 내려받기"
tmp=$(mktemp -d)
curl -fsSL "$TARBALL" | tar xz -C "$tmp" --strip-components=1
mkdir -p "$DIR"
cp "$tmp"/cgv_alert.py "$tmp"/config.json "$tmp"/movie-alert.service "$DIR"/
rm -rf "$tmp"

echo "== 전용 계정"
id movie-alert >/dev/null 2>&1 || useradd -r -M -d "$DIR" -s /sbin/nologin movie-alert
install -d -o movie-alert -g movie-alert /var/lib/movie-alert
chown -R movie-alert:movie-alert "$DIR"

echo "== 환경 설정"
umask 077
{
  printf 'DISCORD_WEBHOOK_URL=%s\n' "$WEBHOOK"
  printf 'STATE_PATH=/var/lib/movie-alert/state.json\n'
  # CGV 가 이 서버 IP 를 막으면 프록시 경유가 필요하다 (CGV_* 를 넘겨주면 기록된다)
  [ -n "${CGV_HOST:-}" ]       && printf 'CGV_HOST=%s\n' "$CGV_HOST"
  [ -n "${CGV_API_PREFIX:-}" ] && printf 'CGV_API_PREFIX=%s\n' "$CGV_API_PREFIX"
  [ -n "${CGV_PROXY_KEY:-}" ]  && printf 'CGV_PROXY_KEY=%s\n' "$CGV_PROXY_KEY"
  true
} > /etc/movie-alert.env
chown movie-alert:movie-alert /etc/movie-alert.env
chmod 600 /etc/movie-alert.env

echo "== 서비스 등록"
install -m 644 "$DIR/movie-alert.service" /etc/systemd/system/movie-alert.service
systemctl daemon-reload
systemctl enable --now movie-alert
systemctl restart movie-alert

echo "== 상태"
sleep 3
systemctl --no-pager -l status movie-alert | head -10
echo
echo "로그 보기 : journalctl -u movie-alert -f"
