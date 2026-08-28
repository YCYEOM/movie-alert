#!/usr/bin/env python3
"""CGV 예매 오픈 알리미 — 표준 라이브러리만 사용."""
import gzip
import json
import os
import sys
import time
import urllib.error
import urllib.request
from datetime import date, datetime, timedelta

API = "https://cgv.co.kr/api/v1"
CO_CD = "A420"
# Referer 가 없으면 Cloudflare WAF 가 403 을 돌려준다. 나머지 헤더는 장식.
HEADERS = {
    "accept": "application/json",
    "accept-encoding": "gzip",
    "accept-language": "ko-KR",
    "referer": "https://cgv.co.kr/cnm/movieBook/cinema",
    "user-agent": (
        "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/537.36 "
        "(KHTML, like Gecko) Chrome/151.0.0.0 Safari/537.36"
    ),
}
HERE = os.path.dirname(os.path.abspath(__file__))
STATE_PATH = os.path.join(HERE, "state.json")


def log(msg):
    print(f"[{datetime.now():%Y-%m-%d %H:%M:%S}] {msg}", flush=True)


def get(path, **params):
    qs = "&".join(f"{k}={v}" for k, v in {"coCd": CO_CD, **params}.items())
    req = urllib.request.Request(f"{API}/{path}?{qs}", headers=HEADERS)
    with urllib.request.urlopen(req, timeout=30) as resp:
        raw = resp.read()
        if resp.headers.get("content-encoding") == "gzip":
            raw = gzip.decompress(raw)
    return json.loads(raw).get("data") or []


def theaters():
    """지역별 극장 목록 -> [(siteNo, "서울 강남"), ...]"""
    return [
        (site["siteNo"], f'{region["regnGrpNm"]} {site["siteNm"]}')
        for region in get("booking/searchRegnList")
        for site in region.get("siteList") or []
    ]


def is_special(row, wanted):
    """wanted 가 비어 있으면 일반관이 아닌 모든 상영관을 특별관으로 본다."""
    grades = {row.get("tcscnsGradNm"), row.get("sascnsGradNm")} - {None, "일반"}
    return bool(grades & set(wanted)) if wanted else bool(grades)


def showtimes(site_no, days, wanted):
    """감시 대상 회차를 {key: 사람이 읽을 문구} 로 반환."""
    found = {}
    today = date.today()
    for offset in range(days):
        ymd = (today + timedelta(days=offset)).strftime("%Y%m%d")
        for row in get(
            "booking/searchMovScnInfo", siteNo=site_no, scnYmd=ymd, rtctlScopCd="01"
        ):
            if not is_special(row, wanted):
                continue
            key = f'{ymd}|{row["scnsNm"]}|{row["prodNm"]}|{row["scnsrtTm"]}'
            start = row["scnsrtTm"]
            found[key] = (
                f'{ymd[4:6]}/{ymd[6:8]} {start[:2]}:{start[2:]} '
                f'{row["prodNm"]} — {row["scnsNm"]}'
            )
        time.sleep(0.3)  # 극장 한 곳당 하루 1요청, 서버 배려용 간격
    return found


def send(text, cfg):
    """설정된 채널로 전송. Discord 와 Telegram 둘 다 켜져 있으면 둘 다 보낸다."""
    discord = os.environ.get("DISCORD_WEBHOOK_URL") or cfg.get("discord_webhook")
    token = os.environ.get("TELEGRAM_TOKEN") or cfg.get("telegram_token")
    chat = os.environ.get("TELEGRAM_CHAT_ID") or cfg.get("telegram_chat_id")
    if not discord and not token:
        log(f"(알림 채널 미설정) {text}")
        return
    for chunk in [text[i : i + 1900] for i in range(0, len(text), 1900)]:
        if discord:
            post(discord, {"content": chunk})
        if token and chat:
            post(
                f"https://api.telegram.org/bot{token}/sendMessage",
                {"chat_id": chat, "text": chunk},
            )


def post(url, payload):
    req = urllib.request.Request(
        url,
        data=json.dumps(payload).encode(),
        # user-agent 가 없으면 Discord 앞단 Cloudflare 가 403 error code 1010 을 낸다.
        headers={"content-type": "application/json", "user-agent": HEADERS["user-agent"]},
    )
    try:
        with urllib.request.urlopen(req, timeout=20) as resp:
            resp.read()
    except urllib.error.HTTPError as exc:
        log(f"알림 전송 실패 {exc.code}: {exc.read()[:200]!r}")
    except OSError as exc:
        log(f"알림 전송 실패: {exc}")


def load_config():
    with open(os.path.join(HERE, "config.json"), encoding="utf-8") as f:
        return json.load(f)


def load_state():
    try:
        with open(STATE_PATH, encoding="utf-8") as f:
            return json.load(f)
    except (OSError, ValueError):
        return {}


def poll_once(cfg, state):
    """대상별로 새로 생긴 회차를 찾아 알린다. state 를 제자리에서 갱신."""
    for target in cfg["targets"]:
        name = target["name"]
        try:
            current = showtimes(
                target["site_no"], cfg.get("days_ahead", 21), target.get("screens", [])
            )
        except (urllib.error.URLError, OSError, ValueError) as exc:
            log(f"{name} 조회 실패: {exc}")  # 다음 주기에 다시 시도
            continue

        previous = state.get(name)
        if previous is None:
            log(f"{name} 기준선 {len(current)}건 저장 (첫 실행이라 알림 없음)")
        else:
            fresh = [current[k] for k in current if k not in previous]
            log(f"{name} {len(current)}건 (신규 {len(fresh)}건)")
            if fresh:
                body = "\n".join(sorted(fresh))
                send(f"**{name} 예매 오픈**\n{body}", cfg.get("notify", {}))
        state[name] = {k: current[k] for k in current}

    with open(STATE_PATH, "w", encoding="utf-8") as f:
        json.dump(state, f, ensure_ascii=False)


def selftest():
    rows = [
        {"tcscnsGradNm": "아이맥스", "sascnsGradNm": "일반"},
        {"tcscnsGradNm": "일반", "sascnsGradNm": "일반"},
        {"tcscnsGradNm": "일반", "sascnsGradNm": "아트하우스"},
        {"tcscnsGradNm": None, "sascnsGradNm": None},
    ]
    assert [is_special(r, ["아이맥스"]) for r in rows] == [True, False, False, False]
    assert [is_special(r, []) for r in rows] == [True, False, True, False]

    prev = {"a": "1", "b": "2"}
    cur = {"b": "2", "c": "3"}
    assert [cur[k] for k in cur if k not in prev] == ["3"], "신규만 잡아야 함"
    assert [k for k in prev if k not in cur] == ["a"]  # 사라진 건 알리지 않음

    long_text = "가" * 4000
    assert len([long_text[i : i + 1900] for i in range(0, len(long_text), 1900)]) == 3

    live = get("booking/searchRegnList")
    assert any(s["siteNm"] == "용산아이파크몰" for r in live for s in r["siteList"])
    print("selftest ok")


if __name__ == "__main__":
    arg = sys.argv[1] if len(sys.argv) > 1 else ""
    if arg == "--selftest":
        selftest()
    elif arg == "--theaters":
        for site_no, label in theaters():
            print(f"{site_no}  {label}")
    else:
        config = load_config()
        saved = load_state()
        interval = config.get("poll_seconds", 180)
        log(f"감시 시작: {[t['name'] for t in config['targets']]} / {interval}초 주기")
        while True:
            poll_once(config, saved)
            if arg == "--once":
                break
            time.sleep(interval)
