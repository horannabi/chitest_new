#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
지정한 날짜의 편성을 주기적으로 조회해서, 키워드와 일치하는 항목이
나타나면 텔레그램으로 알려준다.

chitest(watch.py)의 완전한 포크다. 다른 건 전부 그대로 두고,
"시간표를 가져오는 부분"만 바꿨다 - 메인 목록 페이지(/Theater/MovieTable2)
대신 예매 팝업이 쓰는 두 엔드포인트를 직접 부른다.

    POST /popup/ReserveMovieList    그날의 영화 목록(체크박스 코드)
    POST /popup/ReserveScreenPlan   영화 코드로 회차·잔여석을 받는다

2026-11-09 실측: 메인 목록은 결제/취소가 반영되는 데 수 분씩 걸렸는데,
팝업 엔드포인트는 즉시 반영됐다 (좌석 취소 직후 팝업 146 vs 목록 145,
좌석 예매 직후 팝업 138 vs 목록 140 - 둘 다 팝업이 먼저 맞았다).

바뀐 것 / 안 바뀐 것
    바뀜: Fetcher, parse(), contains_keyword(), check_seats()
          (팝업이 이미 정확한 숫자를 주므로 좌석 배치도를 따로 셀 필요가
           없어졌다 - seats.py 의 "매진/5석 이하만 따로 본다" 는 단계가
           통째로 사라졌다. 느려서가 아니라 더는 쓸모가 없어졌다.)
    안 바뀜: Config 의 나머지, State/Telegram/handle_command(bot.py 그대로),
          Health/Watchdog(health.py 그대로), slot_key/observe_slots/
          only_slots/format_items/raise_alert/resend_alert/should_watch/
          interval_for/snapshot/status_text/main() 의 전체 흐름.

안 되는 것 1가지: watch.py 는 직접 접속이 막히면 r.jina.ai 로 우회하는
안전망이 있다. 그 우회로는 GET 렌더링 전용이라 이 두 엔드포인트처럼
POST 로 데이터를 보내야 하는 호출은 못 넘긴다. 직접 접속만 쓴다.
"""

import faulthandler
import os
import re
import signal
import ssl
import sys
import time
from datetime import datetime, timedelta, timezone

import requests
import urllib3
from requests.adapters import HTTPAdapter
from urllib3.util.ssl_ import create_urllib3_context

from parser import parse_movie_list, parse_screen_plan, pick_movies, to_items
from bot import State, Telegram, handle_command, parse_date
from health import Health, Watchdog

urllib3.disable_warnings(urllib3.exceptions.InsecureRequestWarning)
faulthandler.enable()

KST = timezone(timedelta(hours=9))
WDAY = ("월", "화", "수", "목", "금", "토", "일")

UA = ("Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
      "(KHTML, like Gecko) Chrome/131.0.0.0 Safari/537.36")

OP_LEGACY_SERVER_CONNECT = 0x4


# ---------------------------------------------------------------- 유틸

def log(msg):
    print("[%s] %s" % (datetime.now(KST).strftime("%m-%d %H:%M:%S"), msg), flush=True)


def die(msg, code=2):
    log("오류: " + msg)
    sys.exit(code)


def env(name, default=None):
    v = os.environ.get(name)
    return v.strip() if v and v.strip() else default


def env_int(name, default):
    try:
        return int(env(name) or default)
    except ValueError:
        return default


def env_bool(name, default="0"):
    return (env(name, default) or "").lower() in ("1", "true", "yes", "on")


# ---------------------------------------------------------------- 설정

class Config:
    def __init__(self):
        self.theater = env("THEATER_CODE", "1001")
        self.site_origin = env("SITE_ORIGIN", "https://www.cineq.co.kr")
        self.site_url = env("SITE_URL", "")

        self.keywords = [k.strip() for k in (env("KEYWORDS", "") or "").split(",") if k.strip()]
        if not self.keywords:
            die("KEYWORDS 가 비어 있습니다.")

        raw_date = env("TARGET_DATE")
        self.target = None
        if raw_date:
            self.target = parse_date(raw_date)
            if self.target is None:
                die("TARGET_DATE 형식이 잘못되었습니다: %r (예: 20261115)" % raw_date)

        self.label = env("LABEL", "")
        self.mode = (env("MODE", "until") or "until").lower()

        self.poll_sec = env_int("POLL_SEC", 10)
        self.burst_sec = env_int("BURST_SEC", 2)
        self.burst_windows = parse_windows(env("BURST_WINDOWS", ""))
        self.max_run_min = env_int("MAX_RUN_MIN", 340)
        self.relay_after_min = env_int("RELAY_AFTER_MIN", max(1, self.max_run_min - 15))
        self.relay_workflow = env("RELAY_WORKFLOW", "watch_popup.yml")
        self.health_enabled = env_bool("HEALTH", "1")
        self.health_beat_sec = env_int("HEALTH_BEAT_SEC", 60)
        self.healthcheck_url = env("HEALTHCHECK_URL")
        self.gap_alert_sec = env_int("GAP_ALERT_SEC", 120)
        self.stall_sec = env_int("STALL_SEC", 180)
        self.stall_exit_sec = env_int("STALL_EXIT_SEC", 300)
        self.stall_min_uptime_sec = env_int("STALL_MIN_UPTIME_SEC", 600)
        self.gh_run_id = env("GITHUB_RUN_ID")
        self.gh_event = env("GITHUB_EVENT_NAME")
        self.idle_sec = env_int("IDLE_SEC", 5)
        self.repeat_sec = env_int("ALERT_REPEAT_SEC", 5)

        self.tg_token = env("TELEGRAM_BOT_TOKEN")
        self.tg_chat = env("TELEGRAM_CHAT_ID")
        self.gh_token = env("GITHUB_TOKEN")
        self.gh_repo = env("GITHUB_REPOSITORY")
        self.gh_branch = env("GITHUB_REF_NAME", "main")

        self.force = env_bool("FORCE")
        self.notify_start = env_bool("NOTIFY_START")
        self.verbose = env_bool("VERBOSE")


def parse_windows(spec):
    out = []
    for chunk in (spec or "").split(","):
        chunk = chunk.strip()
        if not chunk:
            continue
        m = re.match(r"^(\d{1,2}):(\d{2})\s*-\s*(\d{1,2}):(\d{2})$", chunk)
        if not m:
            log("BURST_WINDOWS 항목을 해석하지 못해 무시합니다: %r" % chunk)
            continue
        out.append((int(m.group(1)) * 60 + int(m.group(2)),
                    int(m.group(3)) * 60 + int(m.group(4))))
    return out


# ---------------------------------------------------------------- 조회

class LegacyTLSAdapter(HTTPAdapter):
    """구형 TLS 재협상을 허용하는 어댑터. watch.py 와 동일."""

    def init_poolmanager(self, *args, **kwargs):
        kwargs["ssl_context"] = self._ctx()
        return super().init_poolmanager(*args, **kwargs)

    def proxy_manager_for(self, *args, **kwargs):
        kwargs["ssl_context"] = self._ctx()
        return super().proxy_manager_for(*args, **kwargs)

    @staticmethod
    def _ctx():
        ctx = create_urllib3_context()
        ctx.options |= OP_LEGACY_SERVER_CONNECT
        try:
            ctx.set_ciphers("DEFAULT@SECLEVEL=1")
        except ssl.SSLError:
            pass
        ctx.check_hostname = False
        ctx.verify_mode = ssl.CERT_NONE
        return ctx


class Fetcher:
    """
    팝업 두 엔드포인트를 묶어서 watch.py 의 Fetcher 와 같은 계약으로 맞춘다:
    fetch(target) -> (원문 전체, items). 원문은 contains_keyword 안전망용,
    items 는 to_items() 로 watch.py 와 똑같은 모양을 낸 것.

    watch.py 와 달리 r.jina.ai 우회로가 없다 - POST 를 못 넘기기 때문이다.
    """

    def __init__(self, cfg):
        self.cfg = cfg
        self.sess = requests.Session()
        self.sess.mount("https://", LegacyTLSAdapter())
        self.sess.headers.update({
            "User-Agent": UA,
            "Accept-Language": "ko-KR,ko;q=0.9",
            "Cache-Control": "no-cache",
            "Pragma": "no-cache",
            "X-Requested-With": "XMLHttpRequest",
        })
        self.fail_count = 0

    def _post(self, path, data):
        try:
            r = self.sess.post(self.cfg.site_origin + path, data=data,
                               timeout=20, verify=False)
        except requests.RequestException as e:
            log("직접 접속 예외(%s): %s" % (path, str(e)[:160]))
            return None
        if r.status_code != 200:
            log("직접 접속 응답 %s %s" % (path, r.status_code))
            return None
        return r.text or ""

    def fetch(self, target):
        """watch.py 의 fetch() 와 같은 모양: (body, items). 실패하면 (None, None)."""
        ymd = target.strftime("%Y%m%d")

        movielist = self._post("/popup/ReserveMovieList",
                               {"PlayDate": ymd, "TheaterCode": self.cfg.theater,
                                "movieCode": "", "SortType": "1"})
        if movielist is None:
            self.fail_count += 1
            return None, None
        movies = parse_movie_list(movielist)

        if not movies:
            self.fail_count = 0
            return movielist, []                  # 그날 편성 자체가 없다

        # watch.py 는 그날 목록 전체를 한 번에 받아서 client 에서 걸러냈다(match_keywords).
        # 여기서도 같은 모양을 내려고 "전부" 요청한다 - 키워드만 요청하면 /nowall 이
        # 그날 전체를 못 보여준다.
        codes = [c for _t, c in movies]
        plan_body = self._post("/popup/ReserveScreenPlan",
                               {"theaterCode": self.cfg.theater, "playDate": ymd,
                                "movieCode": ",".join(codes)})
        if plan_body is None:
            self.fail_count += 1
            return None, None
        self.fail_count = 0

        rows = parse_screen_plan(plan_body)
        items = to_items(rows)
        return movielist + "\n" + plan_body, items


# ---------------------------------------------------------------- 알림

def format_items(items):
    lines = []
    for it in items:
        lines.append("▪ %s" % it["name"])
        for g in it["groups"]:
            lines.append("   %s" % g["name"])
            for s in g["slots"]:
                note = (" [%s]" % s["note"]) if s["note"] else ""
                extra = s.get("seat_note", "")
                lines.append("     %s%s%s" % (s["time"], note, extra))
    return lines


def slot_label(item, group, slot):
    return "%s|%s|%s" % (item["name"], group["name"], slot["time"])


def slot_key(item, group, slot):
    """
    watch.py 와 동일한 규칙: 사이트 ID(screenplanid) 가 있으면 그것으로 구분한다.
    팝업 소스는 항상 screenplanid 를 주므로(목록처럼 ID 가 빠지는 경우가 없다)
    표기로 떨어지는 경우가 사실상 없다.
    """
    sid = str((slot.get("attrs") or {}).get("screenplanid") or "")
    return ("id:" + sid) if sid else slot_label(item, group, slot)


def observe_slots(state, items):
    """watch.py 의 observe_slots() 와 완전히 동일한 로직."""
    seen = {}
    anomalies = []
    for it in items:
        for g in it["groups"]:
            for s in g["slots"]:
                k = slot_label(it, g, s)
                sid = str((s.get("attrs") or {}).get("screenplanid") or "")
                if k in seen and sid and seen[k] and seen[k] != sid:
                    anomalies.append("같은 회차에 ID 두 개: %s (%s, %s)" % (k, seen[k], sid))
                seen[k] = sid or seen.get(k, "")

    by_id = {}
    for k, sid in seen.items():
        if not sid:
            continue
        if sid in by_id and by_id[sid] != k:
            anomalies.append("한 ID(%s)에 회차 두 개: %s / %s" % (sid, by_id[sid], k))
        by_id[sid] = k

    old = state.slot_ids
    old_labels = {}
    for k, v in old.items():
        if v:
            old_labels.setdefault(v, []).append(k)

    changes = []
    merged = dict(old)
    for k, sid in seen.items():
        if not sid:
            continue
        if old.get(k) and old[k] != sid:
            anomalies.append("같은 회차의 ID 변경: %s  %s -> %s" % (k, old[k], sid))
        prev = [p for p in old_labels.get(sid, []) if p != k]
        if prev and k not in old_labels.get(sid, []):
            changes.append("%s -> %s" % (prev[0], k))
        for p in prev:
            if merged.get(p) == sid:
                del merged[p]

    baseline = not old
    new_keys = []
    if not baseline:
        for it in items:
            for g in it["groups"]:
                for s in g["slots"]:
                    k = slot_label(it, g, s)
                    sid = seen.get(k, "")
                    known = (k in old) or (sid and sid in old_labels)
                    if not known:
                        new_keys.append(slot_key(it, g, s))

    for k, sid in seen.items():
        if sid or k not in merged:
            merged[k] = sid
    changed = merged != old
    state.slot_ids = merged
    if state.id_since is None and any(seen.values()):
        state.id_since = datetime.now(KST).strftime("%Y-%m-%d %H:%M")
        changed = True
    return new_keys, anomalies, changes, changed


def only_slots(items, keys):
    keys = set(keys)
    out = []
    for it in items:
        groups = []
        for g in it["groups"]:
            slots = [s for s in g["slots"] if slot_key(it, g, s) in keys]
            if slots:
                groups.append(dict(g, slots=slots))
        if groups:
            out.append(dict(it, groups=groups))
    return out


def match_keywords(items, keywords):
    hits = []
    for it in items:
        low = it["name"].lower()
        if any(k.lower() in low for k in keywords):
            hits.append(it)
    return hits


def contains_keyword(body, keywords):
    low = body.lower()
    return any(k.lower() in low for k in keywords)


def check_seats(cfg, fetcher, items, pump=None):
    """
    watch.py 의 check_seats() 와 같은 자리(반환 계약: {회차키: 빈자리수})를
    지키지만, 좌석 배치도를 따로 세지는 않는다. 팝업(/popup/ReserveScreenPlan)
    이 이미 정확한 잔여석 숫자를 주기 때문이다 - "매진이거나 5석 이하일 때만
    배치도를 본다"던 watch.py 의 단계 자체가 이 소스에서는 할 일이 없어졌다.
    seats.py 를 이 저장소에 들여오지 않은 이유다.
    """
    result = {}
    for it in items:
        for g in it["groups"]:
            for s in g["slots"]:
                key = slot_key(it, g, s)
                note = s.get("note") or ""
                if "매진" in note:
                    result[key] = 0
                    continue
                m = re.match(r"^(\d+)\s*/\s*(\d+)$", note)
                if m:
                    result[key] = int(m.group(1))
    return result


def build_message(cfg, target, hits):
    head = "찾았습니다!"
    if cfg.label:
        head = "%s - 찾았습니다!" % cfg.label
    lines = [head,
             "날짜 %s (%s)" % (target.strftime("%Y%m%d"), WDAY[target.weekday()]),
             ""]
    if hits:
        lines.extend(format_items(hits))
        lines.append("")
    else:
        lines.append("※ 원문에는 키워드가 있는데 상세 파싱에 실패했습니다.")
        lines.append("   직접 확인하세요.")
        lines.append("")
    if cfg.site_url:
        lines.append(cfg.site_url)
    return "\n".join(lines)


# ---------------------------------------------------------------- 실행 게이트

def should_watch(cfg, target, now):
    if target is None:
        return False, "지정된 날짜가 없습니다. 텔레그램에서 /watch 20261107 로 지정하세요."
    if cfg.force:
        return True, "FORCE=1 이라 날짜 조건을 무시합니다."

    today = now.date()
    if today > target:
        return False, "지정일(%s)이 이미 지났습니다." % target.strftime("%Y%m%d")
    if cfg.mode == "onday" and today != target:
        return False, ("오늘은 지정일(%s)이 아닙니다. MODE=onday 라 대기합니다."
                       % target.strftime("%Y%m%d"))
    return True, "날짜 %s 감시 중 (D-%d)" % (target.strftime("%Y%m%d"), (target - today).days)


def interval_for(cfg, now):
    minute = now.hour * 60 + now.minute
    for a, b in cfg.burst_windows:
        if a <= minute <= b:
            return cfg.burst_sec
    return cfg.poll_sec


# ---------------------------------------------------------------- main

def snapshot(cfg, fetcher, target, show_all=False):
    if target is None:
        return "지정된 날짜가 없습니다. /watch 20261107 처럼 지정하세요."
    body, items = fetcher.fetch(target)
    if body is None:
        return "조회에 실패했습니다. 잠시 후 다시 시도하세요."

    ymd = target.strftime("%Y%m%d")

    if show_all:
        head = "날짜 %s / 전체 %d개" % (ymd, len(items))
        if not items:
            return head + "\n아직 목록이 올라오지 않았습니다."
        return "\n".join([head, ""] + format_items(items))

    hits = match_keywords(items, cfg.keywords)
    if not hits:
        return ("날짜 %s\n아직 없습니다. (그날 전체 %d개)\n전체를 보려면 /nowall"
                % (ymd, len(items)))
    return "\n".join(["날짜 %s / %d건" % (ymd, len(hits)), ""] + format_items(hits))


def raise_alert(cfg, state, tg, text):
    entry = "[%s] %s" % (datetime.now(KST).strftime("%H:%M"), text)
    if state.ack_date is not None and state.ack_text:
        state.ack_text = state.ack_text + "\n\n" + ("-" * 20) + "\n" + entry
    else:
        state.ack_text = entry

    state.ack_date = state.target or state.ack_date
    state.ack_count = 1
    state.ack_last = datetime.now(KST)
    state.save("alert")
    tg.send(state.ack_text + "\n\n확인하셨으면 /ok 를 보내주세요.")


def resend_alert(cfg, state, tg, fetcher):
    if state.ack_date is None:
        return
    now = datetime.now(KST)
    if state.ack_last and (now - state.ack_last).total_seconds() < cfg.repeat_sec:
        return

    if not state.ack_text:
        state.ack_text = snapshot(cfg, fetcher, state.ack_date)

    state.ack_count += 1
    state.ack_last = now
    tg.send("%s\n\n아직 확인하지 않으셨습니다. (%d번째)\n확인하셨으면 /ok 를 보내주세요."
            % (state.ack_text, state.ack_count))
    if state.ack_count % 12 == 0:
        state.save("ack %d" % state.ack_count, commit=False)


def main():
    cfg = Config()
    start = datetime.now(KST)

    tg = Telegram(cfg.tg_token, cfg.tg_chat)
    state = State(cfg.gh_token, cfg.gh_repo, cfg.gh_branch)

    if state.target is None and cfg.target is not None:
        state.target = cfg.target
        state.save("init from env")

    log("기동 완료 / 키워드 %d개 / 극장 %s" % (len(cfg.keywords), cfg.theater))
    log("간격 평상시 %d초, 집중 %d초, 집중구간 %s"
        % (cfg.poll_sec, cfg.burst_sec, cfg.burst_windows or "없음"))
    if not tg.enabled:
        log("경고: 텔레그램이 설정되지 않아 명령 수신이 불가합니다.")

    fetcher = Fetcher(cfg)
    deadline = start + timedelta(minutes=cfg.max_run_min)
    relay_at = start + timedelta(minutes=cfg.relay_after_min)
    relay_last = None

    health = Health(cfg.gh_token, cfg.gh_repo, cfg.gh_branch, cfg.gh_run_id, cfg.gh_event,
                    ping_url=cfg.healthcheck_url, beat_sec=cfg.health_beat_sec)
    health.active = cfg.health_enabled and not state.health_off
    gap = health.start()
    if gap is not None and gap > cfg.gap_alert_sec:
        tg.send("감시 공백이 있었습니다: %d초 (%d분)\n이전 세션 run=%s\n/health 로 자세히 볼 수 있습니다."
                % (gap, gap // 60, health.prev_run))
    log("다음 workflow run 요청 예정 %s / 강제 종료 %s"
        % (relay_at.strftime("%H:%M:%S"), deadline.strftime("%H:%M:%S")))

    wd = Watchdog(health, tg, cfg.stall_sec,
                  exit_sec=cfg.stall_exit_sec,
                  relay_fn=lambda: health.request_next(
                      cfg.relay_workflow, cfg.gh_branch,
                      {"force": "true"} if cfg.force else {}),
                  min_uptime_sec=cfg.stall_min_uptime_sec)
    wd.start()

    def on_signal(signum, _frame):
        wd.stop()
        health.stop("cancelled(signal %d)" % signum, checks)
        sys.exit(0)
    for sig in (signal.SIGINT, signal.SIGTERM):
        signal.signal(sig, on_signal)
    backoff = 0
    checks = 0
    last_count = None
    last_reason = None

    def ymd(d):
        return d.strftime("%Y%m%d") if d else None

    announced = state.target is not None and state.announced == ymd(state.target)
    migrated = {("id:" + state.slot_ids[k]) if state.slot_ids.get(k) else k
                for k in state.zero_slots}
    if migrated != state.zero_slots:
        state.zero_slots = migrated
        state.save("zero slots by id")
    free_before = {k: 0 for k in state.zero_slots} if announced else {}
    if announced:
        log("이전 세션에서 이미 알린 날짜입니다. 매진 회차 %d개를 이어서 지켜봅니다."
            % len(state.zero_slots))

    id_compares = 0

    def id_check_lines():
        if not state.id_since:
            return ["회차 ID 검증: 아직 시작 전 (ID 가 있는 조회가 없었음)"]
        return ["회차 ID 검증: %s 부터 / 이상 %d건 / 이번 세션 비교 %d회 / 기록된 회차 %d개"
                % (state.id_since, state.id_anomalies, id_compares, len(state.slot_ids))]

    def extra_lines():
        return [wd.summary()] + id_check_lines()
    health.extra_fn = extra_lines

    def remember_zero():
        z = {k for k, v in free_before.items() if v == 0}
        if z == state.zero_slots:
            return False
        state.zero_slots = z
        return True

    def status_text():
        _ok, why = should_watch(cfg, state.target, datetime.now(KST))
        out = "%s\n조회 %d회 / 이 세션 종료까지 %d분" % (
            why, checks, max(0, int((deadline - datetime.now(KST)).total_seconds() // 60)))
        if state.ack_date is not None:
            out += "\n미확인 알림 있음 (%d번 보냄). /ok 로 멈춥니다." % state.ack_count
        if state.seatwatch:
            out += "\n빈자리 지켜보기: 켜짐"
        if not cfg.health_enabled:
            out += "\nhealth 체크: 꺼짐 (저장소 변수 HEALTH=0)"
        else:
            out += "\nhealth 체크: %s%s" % (
                "켜짐" if health.active else "꺼짐 (/health on 으로 켜기)",
                " / 외부 ping 사용" if (health.active and cfg.healthcheck_url) else "")
        r = health.relay
        if r["requested_at"]:
            out += "\n다음 workflow run 요청: %s %s" % (r["requested_at"][11:],
                                                 "성공" if r["ok"] else "실패")
        else:
            left = max(0, int((relay_at - datetime.now(KST)).total_seconds() // 60))
            out += "\n다음 workflow run 요청 예정: %s (%d분 뒤)" % (relay_at.strftime("%H:%M"), left)
        return out + ("\n" + cfg.site_url if cfg.site_url else "")

    if cfg.notify_start:
        tg.send("감시 프로세스가 올라왔습니다.\n" + status_text())

    target_changed = [False]

    def pump():
        wd.mark("텔레그램 명령")
        for update_id, text in tg.poll(state.tg_offset):
            state.tg_offset = update_id
            if text is None:
                state.save("offset", commit=False)
                continue
            log("명령 수신")
            if handle_command(
                    text, state, tg, status_text,
                    lambda d, allitems=False: snapshot(cfg, fetcher, d, allitems),
                    health_fn=health.summary):
                target_changed[0] = True
            state.save("offset", commit=False)

    while datetime.now(KST) < deadline:
        wd.mark("루프 시작", checks)

        now = datetime.now(KST)
        if now >= relay_at:
            wait = 300 if health.relay["ok"] else 60
            if relay_last is None or (now - relay_last).total_seconds() >= wait:
                relay_last = now
                wd.mark("다음 workflow run 요청")
                health.request_next(cfg.relay_workflow, cfg.gh_branch,
                                    {"force": "true"} if cfg.force else {})

        wd.mark("텔레그램 명령")
        pump()
        health.active = cfg.health_enabled and not state.health_off
        if target_changed[0]:
            target_changed[0] = False
            last_count = None
            last_reason = None
            checks = 0
            announced = False
            free_before = {}
            if state.announced or state.zero_slots or state.slot_ids:
                state.announced = None
                state.zero_slots = set()
                state.slot_ids = {}
                state.save("reset progress")

        wd.mark("알림 재전송")
        resend_alert(cfg, state, tg, fetcher)

        target = state.target
        watching, reason = should_watch(cfg, target, datetime.now(KST))

        if not watching:
            if state.ack_date is not None:
                time.sleep(min(cfg.repeat_sec, cfg.idle_sec))
                continue
            if reason != last_reason:
                log(reason + " 대기 모드로 텔레그램 명령만 기다립니다.")
                last_reason = reason
            time.sleep(cfg.idle_sec)
            continue

        if reason != last_reason:
            log(reason)
            last_reason = reason

        checks += 1
        wd.mark("목록 조회", checks)
        body, items = fetcher.fetch(target)

        if body is None:
            backoff = min(120, (backoff * 2) if backoff else 30)
            log("조회 실패. %d초 후 재시도합니다." % backoff)
            health.error("조회 실패")
            wd.mark("조회 실패 후 대기 %d초" % backoff)
            time.sleep(backoff)
            continue
        backoff = 0

        wd.mark("목록 해석")

        new_keys, anomalies, slot_changes, slots_changed = observe_slots(state, items)
        if slot_changes:
            for c in slot_changes:
                health.event("[회차 변경] " + c)
            tg.send("[회차 정보 변경] %d건 (같은 회차 ID, 시간 등 변경)\n\n%s"
                    % (len(slot_changes), "\n".join(slot_changes[:30])))
        if any((s.get("attrs") or {}).get("screenplanid")
               for it in items for g in it["groups"] for s in g["slots"]):
            id_compares += 1
        if anomalies:
            state.id_anomalies += len(anomalies)
            for a in anomalies:
                health.event("[ID 검증] " + a)
            tg.send("[회차 ID 검증] 이상 %d건 (누적 %d건)\n\n%s"
                    % (len(anomalies), state.id_anomalies, "\n".join(anomalies[:20])))
            slots_changed = True
        if slots_changed:
            state.save("slots %d" % len(state.slot_ids))

        if contains_keyword(body, cfg.keywords):
            hits = match_keywords(items, cfg.keywords)

            if not announced:
                log("일치 항목 %d건 발견. 알림을 보냅니다." % len(hits))
                wd.mark("좌석 조회")
                free_before = check_seats(cfg, fetcher, hits, pump)
                if target_changed[0]:
                    continue
                text = build_message(cfg, target, hits)
                if cfg.verbose:
                    print(text, flush=True)
                announced = True
                state.announced = ymd(target)
                remember_zero()
                raise_alert(cfg, state, tg, text)
                if not state.seatwatch:
                    state.target = None
                    state.save("done")
                    continue
                tg.send("이어서 빈자리를 지켜봅니다. 끄려면 /seatwatch off")
            elif state.seatwatch:
                wd.mark("좌석 조회")
                free_now = check_seats(cfg, fetcher, hits, pump)
                if target_changed[0]:
                    continue
                opened = [k for k, v in free_now.items()
                          if v > 0 and free_before.get(k) == 0]
                free_before = dict(free_before, **free_now)
                zero_changed = remember_zero()

                hit_keys = {slot_key(it, g, s) for it in hits
                            for g in it["groups"] for s in g["slots"]}
                added = [k for k in new_keys if k in hit_keys]
                if added:
                    log("회차 추가 %d건" % len(added))
                    raise_alert(cfg, state, tg,
                                "회차가 추가됐습니다!\n날짜 %s (%s)\n\n%s\n\n%s"
                                % (target.strftime("%Y%m%d"), WDAY[target.weekday()],
                                   "\n".join(format_items(only_slots(hits, added))),
                                   cfg.site_url))

                if opened:
                    log("빈자리 발생 %d건" % len(opened))
                    raise_alert(cfg, state, tg,
                                "자리가 났습니다!\n날짜 %s (%s)\n\n%s\n\n%s"
                                % (target.strftime("%Y%m%d"), WDAY[target.weekday()],
                                   "\n".join(format_items(hits)), cfg.site_url))
                elif zero_changed and not added:
                    state.save("zero slots %d" % len(state.zero_slots))

        if state.trace_on():
            tg.send("[trace] %s · %d회 · 없음 (전체 %d개)"
                    % (datetime.now(KST).strftime("%H:%M:%S"), checks, len(items)))
        elif state.trace_until is not None:
            state.trace_until = None
            state.save("trace expired")
            tg.send("trace 시간이 끝나 종료했습니다. 감시는 계속됩니다.")

        if len(items) != last_count:
            if cfg.verbose:
                log("현재 %d개: %s" % (len(items), " | ".join(it["name"] for it in items)))
            else:
                log("현재 %d개" % len(items))
            last_count = len(items)
        elif checks % 30 == 0:
            log("감시 중... %d회 조회, 변동 없음 (%d개)" % (checks, len(items)))

        wd.mark("다음 조회 대기")
        time.sleep(interval_for(cfg, datetime.now(KST)))

    wd.stop()
    log("이 세션의 실행 시간(%d분)이 끝났습니다. 다음 세션이 이어서 감시합니다." % cfg.max_run_min)
    health.stop("ended(timeout)", checks)
    tg.send("다음 workflow run 없이 이 세션이 끝났습니다. 다음 예약 실행까지 감시가 비어 있을 수 있습니다.\n"
            "다음 workflow run 요청: %d회 시도, 마지막 %s"
            % (health.relay["tries"], "성공" if health.relay["ok"] else "실패"))
    return 0


if __name__ == "__main__":
    sys.exit(main())
