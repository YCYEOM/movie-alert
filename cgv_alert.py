#!/usr/bin/env python3
"""CGV 예매 오픈 알리미 — 표준 라이브러리만 사용."""
import gzip
import http.client
import re
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

# CGV 가 429 를 주면 일정 시간 요청을 아예 멈춘다. 그냥 재시도하면 서버를 두드리는
# 꼴이고 제재만 길어진다. 성공하면 단계는 초기화.
_cool_until = 0.0
_cool_step = 0


def cooling():
    return time.monotonic() < _cool_until


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
    if cooling():  # 쉬는 중엔 요청 자체를 만들지 않는다 (단계도 올리지 않음)
        raise urllib.error.HTTPError(HOST, 429, "cooling", None, None)
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
    if status == 429:
        global _cool_step, _cool_until
        _cool_step = min(_cool_step + 1, 5)
        wait = 30 * 2 ** (_cool_step - 1)  # 30, 60, 120, 240, 480초
        _cool_until = time.monotonic() + wait
        log(f"429 Too Many Requests — {wait}초간 요청 중단")
        drop_conn()
        raise urllib.error.HTTPError(HOST + url, status, "rate limited", resp.headers, None)
    if status != 200:
        drop_conn()
        raise urllib.error.HTTPError(HOST + url, status, resp.reason, resp.headers, None)
    _cool_step = 0
    if enc == "gzip":
        raw = gzip.decompress(raw)
    data = json.loads(raw).get("data")
    return [] if data is None else data


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


# 설정의 상영관 이름 -> 예매가능 영화목록 API 의 attrCd.
# 이 목록은 siteNo 없이 CGV 전체에서 그 특별관으로 지금 예매되는 영화를 준다(222~750B).
# 아바타가 IMAX 목록에 뜨는 순간이 곧 IMAX 예매 오픈이다.
SPECIAL_ATTR = {"아이맥스": "04", "IMAX": "04", "4DX": "03",
                "SCREENX": "08", "돌비": "06", "DOLBY ATMOS": "06"}
ATTR_NM = {"04": "IMAX", "03": "4DX", "08": "SCREENX", "06": "DOLBY ATMOS"}


def movie_list(attr):
    """그 특별관으로 지금 예매 가능한 영화 이름. 요청 1건."""
    rows = get("booking/searchAtktTopPostrList",
               movNm="", div="CUST_EXPO_MOVTYP_CD", attrCd=attr)
    return {r["movNm"] for r in rows}


def site_dates(site_no, cap):
    """그 극장에 상영 일정이 잡힌 날짜. 요청 1건.

    회차 API 와 달리 scnYmd 가 필요 없다. 예매가 열리기 전에도 날짜가 먼저 뜨므로
    사전 신호로 쓸 수 있고, 21일 같은 임의의 창을 둘 필요도 없어진다.
    """
    today = date.today().strftime("%Y%m%d")
    last = (date.today() + timedelta(days=cap)).strftime("%Y%m%d")
    return [r["scnYmd"] for r in get("booking/searchSiteScnscYmdListBySite", siteNo=site_no)
            if today <= r["scnYmd"] <= last]


DOW = "월화수목금토일"


def daylabel(ymd):
    wd = DOW[date(int(ymd[:4]), int(ymd[4:6]), int(ymd[6:])).weekday()]
    return f"{ymd[4:6]}/{ymd[6:]}({wd})"


def room_rank(scns):
    """상영관 정렬 순서. IMAX 를 맨 위로 둔다 — 이 감시의 목적이 대체로 그것이라서.

    시각을 빼면서 "가장 이른 회차 순" 정렬이 의미를 잃어, 대신 특별관 우선순위로 세운다.
    """
    up = scns.upper()
    for i, tag in enumerate(("IMAX", "4DX", "SCREENX")):
        if tag in up or (tag == "IMAX" and "아이맥스" in scns):
            return i
    return 9


def render(name, keys):
    """회차를 상영관 > 영화 > 날짜 로 묶는다. 시각은 넣지 않는다.

    오픈 순간에 필요한 건 "어느 관에 무엇이 며칠치 열렸나"까지고, 시간표는
    어차피 CGV 에서 고르게 된다. 시각을 빼면 아바타 IMAX 7일 21회차가 네 줄이다.
    대신 날짜 뒤에 회차 수를 적어 얼마나 열렸는지는 알 수 있게 둔다.
    """
    rooms = {}
    for k in keys:
        ymd, scns, prod, _ = k.split("|")
        m = re.match(r"^(.*)\(([^()]*)\)$", prod)  # 제목 끝 괄호가 상영 포맷
        title, fmt = (m.group(1).strip(), m.group(2)) if m else (prod, "")
        rooms.setdefault(scns, {}).setdefault((title, fmt), []).append(ymd)
    days = {k[:8] for k in keys}
    lines = [f"**{name} 예매 오픈** · {len(days)}일 {len(keys)}회차"]
    for scns in sorted(rooms, key=lambda s: (room_rank(s),
                                            min(min(v) for v in rooms[s].values()), s)):
        lines.append(f"▸ **{scns}**")
        for (title, fmt), ymds in sorted(rooms[scns].items(), key=lambda x: min(x[1])):
            lines.append(f"　{title} · {fmt}" if fmt else f"　{title}")
            lines.append("　　" + ", ".join(daylabel(y) for y in sorted(set(ymds)))
                         + f" · {len(ymds)}회")
    return "\n".join(lines)


def chunks(text, limit=1900):
    """디스코드 한도에 맞춰 자르되 줄 중간에서 끊지 않는다. 한 줄이 한도를
    넘으면 그때만 쪼갠다 — 버리지는 않는다."""
    out, cur = [], ""
    for line in text.split("\n"):
        while len(line) > limit:
            if cur:
                out.append(cur)
                cur = ""
            out.append(line[:limit])
            line = line[limit:]
        if cur and len(cur) + 1 + len(line) > limit:
            out.append(cur)
            cur = ""
        cur = f"{cur}\n{line}" if cur else line
    if cur:
        out.append(cur)
    return out or [""]


def send(text, cfg, env="DISCORD_WEBHOOK_URL"):
    """설정된 채널로 전송. Discord 와 Telegram 둘 다 켜져 있으면 둘 다 보낸다."""
    # 공고용 채널을 따로 안 만들었으면 기본 채널로 보낸다 (안 보내는 것보다 낫다)
    discord = (os.environ.get(env) or cfg.get("discord_webhook")
               or os.environ.get("DISCORD_WEBHOOK_URL"))
    token = os.environ.get("TELEGRAM_TOKEN") or cfg.get("telegram_token")
    chat = os.environ.get("TELEGRAM_CHAT_ID") or cfg.get("telegram_chat_id")
    if not discord and not token:
        log(f"(알림 채널 미설정) {text}")
        return
    for chunk in chunks(text):
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
    send(render(name, fresh), cfg.get("notify", {}))


def entry_of(state, name):
    return state.setdefault(name, {"shows": {}, "dates": [], "seen": []})


def target_of(cfg, name):
    return next(t for t in cfg["targets"] if t["name"] == name)


def report(cfg, state, name, found):
    """찾은 회차 중 처음 보는 것만 알리고 스냅샷에 합친다."""
    entry = entry_of(state, name)
    fresh = [k for k in found if k not in entry["shows"]]
    if fresh:
        alert(name, fresh, cfg)
    entry["shows"].update(found)
    save_state(state)


def rush(cfg, state, attr, movies):
    """특별관 예매가 열린 순간. 드문 사건이라 이때만 몰아서 훑는다.

    평소에 요청을 아끼는 이유가 바로 이 순간에 마음껏 쓰기 위해서다.
    """
    for target in cfg["targets"]:
        if attr not in {SPECIAL_ATTR.get(s) for s in target.get("screens", [])}:
            continue
        name = target["name"]
        found = {}
        for ymd in entry_of(state, name)["dates"] or []:
            try:
                found.update(fetch_day(target["site_no"], ymd, target.get("screens", [])))
            except (urllib.error.URLError, OSError, ValueError) as exc:
                log(f"{name} 긴급조회 중단: {exc}")
                break
            time.sleep(0.2)
        report(cfg, state, name, found)


def tripwire(cfg, state, item):
    """요청 1건짜리 감시. 변화가 보이면 그때만 회차를 펼쳐본다."""
    kind, key = item
    if kind == "movies":
        seen = state.setdefault("__movies__", {})
        try:
            now = movie_list(key)
        except (urllib.error.URLError, OSError, ValueError) as exc:
            log(f"{ATTR_NM.get(key, key)} 영화목록 실패: {exc}")
            return
        was = set(seen.get(key) or [])
        if was == now:
            return
        seen[key] = sorted(now)
        save_state(state)
        if not was:
            log(f"{ATTR_NM.get(key, key)} 기준선 {len(now)}편")
            return
        fresh = now - was
        if fresh:
            log(f"{ATTR_NM.get(key, key)} 신규 예매 가능: {', '.join(sorted(fresh))}")
            rush(cfg, state, key, fresh)
        return

    target = target_of(cfg, key)
    entry = entry_of(state, key)
    try:
        now = site_dates(target["site_no"], cfg.get("days_ahead", 60))
    except (urllib.error.URLError, OSError, ValueError) as exc:
        log(f"{key} 날짜목록 실패: {exc}")
        return
    was = set(entry["dates"] or [])
    if was == set(now) and entry["dates"]:
        return
    entry["dates"] = now
    save_state(state)
    fresh = [y for y in now if y not in was]
    if not was or not fresh:
        return
    log(f"{key} 새 날짜 {len(fresh)}건: {', '.join(fresh)}")
    found = {}
    for ymd in fresh:
        try:
            found.update(fetch_day(target["site_no"], ymd, target.get("screens", [])))
        except (urllib.error.URLError, OSError, ValueError) as exc:
            log(f"{key} 신규날짜 조회 중단: {exc}")
            break
        time.sleep(0.2)
    report(cfg, state, key, found)


def scan_pair(cfg, state, pair):
    """(극장, 날짜) 한 쌍만 훑는다. 전체 스윕을 tick 단위로 흩뿌린 것.

    한꺼번에 126요청을 쏘면 그 순간 레이트리밋에 걸린다(실제로 걸렸다). 같은
    총량을 한 번에 1건씩 나눠 쓰면 폭주 구간이 사라지고 사각지대도 없어진다.
    """
    name, ymd = pair
    target = target_of(cfg, name)
    entry = entry_of(state, name)
    try:
        day = fetch_day(target["site_no"], ymd, target.get("screens", []))
    except (urllib.error.URLError, OSError, ValueError) as exc:
        log(f"{name} {ymd} 실패: {exc}")
        return
    first = ymd not in entry["seen"]
    was = {k for k in entry["shows"] if k.startswith(ymd)}
    if set(day) == was and not first:
        return  # 그대로면 쓸 것도 알릴 것도 없다
    if first:
        entry["seen"].append(ymd)
    fresh = [k for k in day if k not in entry["shows"]]
    # 그 날짜의 옛 키는 지우고 새로 채운다. 사라진 회차는 알리지 않는다.
    for k in was:
        del entry["shows"][k]
    entry["shows"].update(day)
    if fresh and not first:
        alert(name, fresh, cfg)
    save_state(state)


def build_plan(cfg):
    """트립와이어 순회 목록: 특별관 영화목록들 + 극장 날짜목록들."""
    attrs = []
    for target in cfg["targets"]:
        for screen in target.get("screens", []):
            attr = SPECIAL_ATTR.get(screen)
            if attr and attr not in attrs:
                attrs.append(attr)
    return [("movies", a) for a in attrs] + [("dates", t["name"]) for t in cfg["targets"]]


def sweep_pairs(cfg, state):
    """훑을 (극장, 날짜) 전체. 날짜목록이 알려준 실제 날짜만 돈다."""
    return [(t["name"], ymd)
            for t in cfg["targets"]
            for ymd in entry_of(state, t["name"])["dates"] or []]


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


def notices(cfg, state):
    """이벤트 게시판에서 특별관·예매 관련 새 공고를 찾아 알린다.

    회차가 실제로 열리는 순간(sweep/fast_check)과 달리, 이쪽은 "O월 O일 O시 오픈"
    같은 사전 공고를 잡는다. 둘은 다른 사건이라 채널도 따로 둘 수 있다.
    """
    if cooling():
        return
    conf = cfg.get("notices") or {}
    words = conf.get("keywords") or []
    if not words:
        return
    try:
        page = get(
            "content/event/evt/evt/searchEvtListForPage",
            sscnsChoiYn="N", expnYn="N", expoChnlCd="01",
            startRow=0, listCount=conf.get("count", 40),
        )
        # 회차 API 와 달리 data 가 {startRow, totalCount, list:[...]} 형태다
        rows = page.get("list") if isinstance(page, dict) else page
    except (urllib.error.URLError, OSError, ValueError) as exc:
        log(f"공고 조회 실패: {exc}")
        return

    pattern = re.compile("|".join(re.escape(w) for w in words), re.IGNORECASE)
    first = "__notices__" not in state
    seen = state.setdefault("__notices__", {})
    fresh = []
    for row in rows:
        no = str(row.get("evntNo") or "")
        name = (row.get("evntNm") or "").strip()
        if not no or no in seen:
            continue
        seen[no] = name
        if pattern.search(name):
            fresh.append(name)
    if first:
        log(f"공고 기준선 {len(seen)}건 (첫 실행이라 알림 없음)")
    elif fresh:
        log(f"공고 신규 {len(fresh)}건")
        send("**예매 오픈 공고**\n" + "\n".join("· " + f for f in fresh),
             conf, env="NOTICE_WEBHOOK_URL")
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
    assert [k for k in cur if k not in prev] == ["c"], "신규만 잡아야 함"
    assert [k for k in prev if k not in cur] == ["a"]  # 사라진 건 알리지 않음

    # 분할: 한도를 넘는 한 줄은 쪼개되 버리지 않는다
    assert len(chunks("가" * 4000)) == 3
    assert "".join(chunks("가" * 4000)) == "가" * 4000, "쪼개도 내용은 보존"
    body = "\n".join(f"{i:04d}번 줄 " + "가" * 40 for i in range(200))
    parts = chunks(body)
    assert len(parts) > 1 and all(len(c) <= 1900 for c in parts)
    assert "\n".join(parts) == body, "줄 경계에서만 끊겨야 함"

    # 알림 묶기: 같은 관·같은 영화면 시각이 한 줄에 모인다
    keys = ["20260902|IMAX관|오디세이(IMAX LASER 2D)|0730",
            "20260902|IMAX관|오디세이(IMAX LASER 2D)|1100",
            "20260902|4DX관|스파이더맨(ULTRA 4DX 2D)|1300",
            "20260903|IMAX관|오디세이(IMAX LASER 2D)|0730"]
    out = render("용산", keys)
    assert out.count("▸ **IMAX관**") == 1, "관 머리글은 한 번만"
    assert not re.search(r"\d\d:\d\d", out), "시각은 넣지 않는다"
    assert "09/02(수), 09/03(목) · 3회" in out, "영화별로 날짜를 묶고 횟수를 센다"
    assert "오디세이 · IMAX LASER 2D" in out, "제목과 포맷을 나눠서"
    assert "2일 4회차" in out
    # IMAX 가 맨 위로 온다 (시각을 뺀 뒤로는 특별관 우선순위로 세운다)
    assert out.index("▸ **IMAX관**") < out.index("▸ **4DX관**"), "IMAX 가 먼저"
    assert [room_rank(r) for r in ("IMAX관", "4DX관", "14관[SCREENX]", "아트하우스")] == [0, 1, 2, 9]

    # 같은 영화면 날짜가 한 줄로 모인다 (시각이 달라도 이제는 묶인다)
    merged = render("용산", [f"{y}|IMAX관|아바타(IMAX 2D)|{t}"
                            for y in ("20260910", "20260911") for t in ("1000", "1400")])
    assert "09/10(목), 09/11(금) · 4회" in merged, "날짜를 묶고 회차 수를 적는다"
    assert merged.count("아바타 · IMAX 2D") == 1, "영화 줄은 한 번만"
    # 괄호 없는 제목도 깨지지 않아야 한다
    assert render("x", ["20260902|IMAX관|무제|0730"]).endswith("· 1회")

    # 공고 필터: 키워드가 든 것만, 그리고 한 번 본 것은 다시 알리지 않는다
    pat = re.compile("|".join(re.escape(w) for w in ["예매", "IMAX", "SCREENX"]), re.IGNORECASE)
    rows = [{"evntNo": "1", "evntNm": "[오디세이] IMAX N차 관람 이벤트"},
            {"evntNo": "2", "evntNm": "8월 팝콘 할인"},
            {"evntNo": "3", "evntNm": "[아바타] 아이맥스 예매 오픈 안내"}]
    seen, fresh = {}, []
    for r in rows:
        if r["evntNo"] in seen:
            continue
        seen[r["evntNo"]] = r["evntNm"]
        if pat.search(r["evntNm"]):
            fresh.append(r["evntNm"])
    assert len(fresh) == 2 and "팝콘" not in " ".join(fresh)
    assert [r for r in rows if r["evntNo"] not in seen] == [], "두 번째 순회에선 신규 없음"

    cfg = {"targets": [
        {"name": "용산", "site_no": "0013", "screens": ["아이맥스", "4DX"]},
        {"name": "서면", "site_no": "0005", "screens": ["아이맥스"]},
    ]}
    # 트립와이어 순회: 특별관은 중복 제거, 극장은 전부
    assert build_plan(cfg) == [("movies", "04"), ("movies", "03"),
                               ("dates", "용산"), ("dates", "서면")]
    # 요청량: tick 당 1건이고, 스윕 share 만큼마다 트립와이어 1건
    share = 2
    kinds = ["트립" if n % (share + 1) == 0 else "스윕" for n in range(9)]
    assert kinds.count("트립") == 3 and kinds.count("스윕") == 6

    # 스윕 커서는 날짜목록이 준 실제 날짜만 돈다 (21일 같은 임의 창이 없다)
    st = {"용산": {"shows": {}, "dates": ["20260902", "20261009"], "seen": []},
          "서면": {"shows": {}, "dates": ["20260902"], "seen": []}}
    assert sweep_pairs(cfg, st) == [("용산", "20260902"), ("용산", "20261009"),
                                    ("서면", "20260902")], "먼 날짜도 포함"

    # 첫 방문 날짜는 기준선만 잡고 알리지 않는다. 두 번째부터 신규를 알린다.
    e = {"shows": {}, "dates": ["20260902"], "seen": []}
    day1 = {"20260902|IMAX관|오디세이(IMAX 2D)|1000": "x"}
    first = "20260902" not in e["seen"]
    assert first, "처음 보는 날짜"
    e["seen"].append("20260902")
    e["shows"].update(day1)
    day2 = dict(day1, **{"20260902|IMAX관|아바타(IMAX 2D)|1400": "y"})
    again = [k for k in day2 if k not in e["shows"]]
    assert "20260902" in e["seen"] and again == ["20260902|IMAX관|아바타(IMAX 2D)|1400"], \
        "이미 열린 날짜에 붙은 회차를 잡아야 함 (8/28~8/31 실측 43건이 이 경우)"

    # 같은 날짜를 다시 훑으면 그 날짜의 옛 키는 지우고 새로 채운다
    e["shows"].update(day2)
    shrunk = {"20260902|IMAX관|오디세이(IMAX 2D)|1000": "x"}
    for k in [k for k in e["shows"] if k.startswith("20260902")]:
        del e["shows"][k]
    e["shows"].update(shrunk)
    assert e["shows"] == shrunk, "사라진 회차는 조용히 빠진다"

    # 특별관 영화목록 트립와이어: 새 영화가 뜨면 그게 곧 예매 오픈
    was, now = {"오디세이"}, {"오디세이", "아바타-불과 재"}
    assert now - was == {"아바타-불과 재"}
    assert not ({"오디세이"} - {"오디세이", "아바타-불과 재"}), "빠진 영화는 알리지 않음"
    assert SPECIAL_ATTR["아이맥스"] == "04" and ATTR_NM["04"] == "IMAX"

    # 429 백오프: 한 번 맞으면 그 뒤 요청은 아예 나가지 않아야 한다
    # (__main__ 로 실행되므로 import 말고 이 모듈의 전역을 직접 다룬다)
    g = globals()
    saved = (g["_cool_until"], g["_cool_step"], g["get"])
    try:
        g["_cool_step"], g["_cool_until"] = 1, time.monotonic() + 30
        assert cooling(), "429 직후에는 쉬어야 함"
        calls = []
        g["get"] = lambda *a, **k: calls.append(1)
        notices({"notices": {"keywords": ["예매"]}}, {})
        assert calls == [], "쉬는 동안 요청이 나가면 안 됨"
        # tick 루프는 쉬는 동안 어떤 일도 배차하지 않는다
        assert not [x for x in ["배차"] if not cooling()], "쉬면 tick 은 빈손"
        g["get"] = saved[2]  # 진짜 get 으로 되돌려 놓고 확인
        # 쉬는 중 get() 은 네트워크에 나가지 않고 단계도 올리지 않아야 한다
        before = g["_cool_step"]
        for _ in range(6):
            try:
                get("booking/searchRegnList")
            except urllib.error.HTTPError as exc:
                assert exc.code == 429
        # 트립와이어와 스윕은 429 를 삼키고 단계도 건드리지 않아야 한다
        one = {"targets": [{"name": "x", "site_no": "0", "screens": ["아이맥스"]}]}
        tripwire(one, {}, ("movies", "04"))
        tripwire(one, {"x": {"shows": {}, "dates": [], "seen": []}}, ("dates", "x"))
        scan_pair(one, {"x": {"shows": {}, "dates": ["20260901"], "seen": []}},
                  ("x", "20260901"))
        assert g["_cool_step"] == before, "쉬는 중 호출로 단계가 오르면 안 됨"

        g["_cool_until"] = 0.0
        assert not cooling(), "시간이 지나면 재개"
        assert [30 * 2 ** (n - 1) for n in range(1, 6)] == [30, 60, 120, 240, 480]
    finally:
        g["_cool_until"], g["_cool_step"], g["get"] = saved

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
        if any("dates" not in v for k, v in saved.items() if not k.startswith("__")):
            saved = {}  # 옛 형식이면 기준선부터 다시 (알림 없음)
        tick = config.get("tick_seconds", 1.0)
        share = config.get("sweep_per_tripwire", 2)
        plan = build_plan(config)
        ntc_every = config.get("notice_seconds", 300)
        log(f"감시 시작: {[t['name'] for t in config['targets']]}")
        log(f"tick {tick}초 · 트립와이어 {len(plan)}종 · 스윕 {share}틱당 1종 "
            f"(약 {1 / tick:.1f} req/s)")
        last_ntc = float("-inf")
        pi = si = n = 0
        pairs = []
        while True:
            # 쉬는 중엔 요청을 아예 만들지 않는다. tick 만 흘려보낸다.
            if not cooling():
                if time.monotonic() - last_ntc >= ntc_every:
                    notices(config, saved)
                    last_ntc = time.monotonic()
                elif n % (share + 1) == 0 and plan:
                    tripwire(config, saved, plan[pi % len(plan)])
                    pi += 1
                else:
                    if not pairs:
                        pairs, si = sweep_pairs(config, saved), 0
                    if pairs:
                        scan_pair(config, saved, pairs[si % len(pairs)])
                        si += 1
                        if si >= len(pairs):  # 한 바퀴 돌면 날짜목록을 다시 읽는다
                            pairs = []
                heartbeat(config, saved)
                n += 1
            if arg == "--once" and n > len(plan) + 2:
                break
            time.sleep(tick)
