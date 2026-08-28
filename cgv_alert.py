#!/usr/bin/env python3
"""CGV 예매 오픈 알리미 — 표준 라이브러리만 사용."""
import gzip
import http.client
import json
import os
import sys
import time
import urllib.error
import urllib.request
from datetime import date, datetime, timedelta

# CGV 는 일부 데이터센터 IP(오라클 등)를 Cloudflare 로 403 차단한다.
# 그런 곳에서는 CGV_HOST/CGV_API_PREFIX/CGV_PROXY_KEY 로 프록시를 경유한다.
HOST = os.environ.get("CGV_HOST", "cgv.co.kr")
API = os.environ.get("CGV_API_PREFIX", "/api/v1")
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
if os.environ.get("CGV_PROXY_KEY"):
    HEADERS["x-proxy-key"] = os.environ["CGV_PROXY_KEY"]

HERE = os.path.dirname(os.path.abspath(__file__))
# 서버 배포 시엔 저장소 밖(/var/lib)을 쓴다. 저장소 안이면 git pull 이 깨진다.
STATE_PATH = os.environ.get("STATE_PATH") or os.path.join(HERE, "state.json")

# 연결을 재사용하면 요청당 99ms -> 75ms. 빠른 감시에서 이 차이가 누적된다.
_conn = None


def drop_conn():
    global _conn
    if _conn is not None:
        try:
            _conn.close()
        except OSError:
            pass
        _conn = None


def log(msg):
    print(f"[{datetime.now():%Y-%m-%d %H:%M:%S}] {msg}", flush=True)


def get(path, **params):
    global _conn
    qs = "&".join(f"{k}={v}" for k, v in {"coCd": CO_CD, **params}.items())
    url = f"{API}/{path}?{qs}"
    for attempt in (1, 2):
        try:
            if _conn is None:
                _conn = http.client.HTTPSConnection(HOST, timeout=30)
            _conn.request("GET", url, headers=HEADERS)
            resp = _conn.getresponse()
            raw, status, enc = resp.read(), resp.status, resp.headers.get(
                "content-encoding"
            )
            break
        except (http.client.HTTPException, OSError):
            drop_conn()  # 서버가 끊은 연결일 수 있으니 한 번은 새로 맺어본다
            if attempt == 2:
                raise
    if status != 200:
        drop_conn()
        raise urllib.error.HTTPError(HOST + url, status, resp.reason, resp.headers, None)
    if enc == "gzip":
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


def fetch_day(site_no, ymd, wanted):
    """하루치 특별관 회차를 {key: 사람이 읽을 문구} 로."""
    found = {}
    for row in get(
        "booking/searchMovScnInfo", siteNo=site_no, scnYmd=ymd, rtctlScopCd="01"
    ):
        if not is_special(row, wanted):
            continue
        start = row["scnsrtTm"]
        found[f'{ymd}|{row["scnsNm"]}|{row["prodNm"]}|{start}'] = (
            f'{ymd[4:6]}/{ymd[6:8]} {start[:2]}:{start[2:]} '
            f'{row["prodNm"]} — {row["scnsNm"]}'
        )
    return found


def window(days):
    today = date.today()
    return [(today + timedelta(days=n)).strftime("%Y%m%d") for n in range(days)]


def scan(target, ymds, pause):
    found = {}
    for ymd in ymds:
        found.update(fetch_day(target["site_no"], ymd, target.get("screens", [])))
        if pause:
            time.sleep(pause)
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


def alert(name, fresh, cfg):
    log(f"{name} 신규 {len(fresh)}건")
    send(f"**{name} 예매 오픈**\n" + "\n".join(sorted(fresh)), cfg.get("notify", {}))


def sweep(cfg, state):
    """전체 창을 훑어 기준선을 다시 잡고, 비어 있는 날짜 목록을 갱신한다.

    ponytail: 스윕이 도는 ~50초 동안은 빠른 감시가 멈춘다(사각지대). 주기를 30분으로
    두어 노출을 시간당 2회로 줄였다. 이마저 아까우면 (극장, 날짜) 하나씩 빠른 감시
    사이에 끼워 넣는 커서 방식으로 바꾸면 사각지대가 사라진다.
    """
    ymds = window(cfg.get("days_ahead", 21))
    for target in cfg["targets"]:
        name = target["name"]
        try:
            shows = scan(target, ymds, pause=0.3)
        except (urllib.error.URLError, OSError, ValueError) as exc:
            log(f"{name} 전체조회 실패: {exc}")
            continue
        seen = {y for y in ymds if any(k.startswith(y) for k in shows)}
        entry = state.get(name)
        if entry is None:
            log(f"{name} 기준선 {len(shows)}건 (첫 실행이라 알림 없음)")
        else:
            fresh = [shows[k] for k in shows if k not in entry["shows"]]
            log(f"{name} 전체 {len(shows)}건 (신규 {len(fresh)}건)")
            if fresh:
                alert(name, fresh, cfg)
        # 회차가 하나도 없는 날짜 = 예매가 아직 안 열린 날. 빠른 감시는 여기만 본다.
        state[name] = {"shows": shows, "empty": [y for y in ymds if y not in seen]}
    save_state(state)


def fast_check(cfg, state):
    """아직 안 열린 날짜만 훑는다. 응답이 비어 있어 한 건당 ~80ms."""
    hit = False
    for target in cfg["targets"]:
        name = target["name"]
        entry = state.get(name)
        if not entry or not entry["empty"]:
            continue
        watch = entry["empty"][: cfg.get("fast_dates", 3)]
        try:
            shows = scan(target, watch, pause=0)
        except (urllib.error.URLError, OSError, ValueError) as exc:
            log(f"{name} 빠른조회 실패: {exc}")
            continue
        fresh = [shows[k] for k in shows if k not in entry["shows"]]
        if fresh:
            alert(name, fresh, cfg)
            entry["shows"].update(shows)
            opened = {k.split("|")[0] for k in shows}
            entry["empty"] = [y for y in entry["empty"] if y not in opened]
            hit = True
    if hit:
        save_state(state)


def heartbeat(cfg, state):
    """하루 한 번 생존 신호. 이게 끊기면 감시가 죽은 것이다.

    오라클 무료 인스턴스는 7일간 CPU/네트워크가 20% 미만이면 회수될 수 있는데,
    이 프로그램은 그 조건에 정확히 해당한다. 조용히 사라지는 걸 막는 장치.
    """
    hours = cfg.get("heartbeat_hours", 24)
    if not hours:
        return
    meta = state.setdefault("__meta__", {})
    if time.time() - meta.get("last", 0) < hours * 3600:
        return
    lines = []
    for target in cfg["targets"]:
        entry = state.get(target["name"])
        if not entry:
            continue
        days = sorted({k[:8] for k in entry["shows"]})
        upto = f'{days[-1][4:6]}/{days[-1][6:]}까지' if days else '없음'
        lines.append(f'{target["name"]} {len(entry["shows"])}건 · {upto}')
    send("🟢 감시 중\n" + "\n".join(lines), cfg.get("notify", {}))
    meta["last"] = time.time()
    save_state(state)


def save_state(state):
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

    # 빈 날짜(=예매 미오픈) 판정: 회차 키의 앞 8자리가 날짜다
    ymds = ["20260901", "20260902", "20260903"]
    shows = {"20260901|IMAX관|영화|1000": "x"}
    seen = {y for y in ymds if any(k.startswith(y) for k in shows)}
    assert [y for y in ymds if y not in seen] == ["20260902", "20260903"]

    # 빠른 감시가 오픈을 잡으면 그 날짜는 빈 목록에서 빠져야 한다
    entry = {"shows": dict(shows), "empty": ["20260902", "20260903"]}
    found = {"20260902|IMAX관|영화|1200": "y"}
    assert [found[k] for k in found if k not in entry["shows"]] == ["y"]
    entry["shows"].update(found)
    opened = {k.split("|")[0] for k in found}
    entry["empty"] = [y for y in entry["empty"] if y not in opened]
    assert entry["empty"] == ["20260903"], "열린 날짜는 빠른 감시 대상에서 빠져야 함"

    assert window(3)[0] == date.today().strftime("%Y%m%d")
    assert len(window(21)) == 21

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
        if any("shows" not in v for k, v in saved.items() if k != "__meta__"):
            saved = {}  # 옛 형식이면 기준선부터 다시 (알림 없음)
        full_every = config.get("poll_seconds", 300)
        fast_every = config.get("fast_seconds", 10)
        log(f"감시 시작: {[t['name'] for t in config['targets']]}")
        log(f"전체 {full_every}초 / 빠른 감시 {fast_every}초")
        last_full = 0.0
        while True:
            if time.monotonic() - last_full >= full_every:
                sweep(config, saved)
                heartbeat(config, saved)
                last_full = time.monotonic()
                if arg == "--once":
                    break
            else:
                fast_check(config, saved)
            time.sleep(fast_every)
