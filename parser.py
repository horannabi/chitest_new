#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
/popup/ReserveScreenPlan 응답 파서.

실측(11-09, probe_call.txt)으로 확인한 구조를 그대로 따른다. 같은 영화도
자막판/더빙판처럼 상영관(관)이 다른 블록으로 나뉘어 반복된다.

    <div class="title">...영화명...</div>
    <div class="theater-info">4관 <span class="all-seats">(140석)</span></div>
    <div class="time-box">
      <div class="time" data-screenplanid="1111366" data-playdate="20261110" ...>
        <span class="from">13:10<span class="to">~14:56</span></span>
        <span class="seats"> 138석 </span>
        [<span class="screen-attributes">...더빙 뱃지...</span>]
        [<span class="period-icon morning">...조조 아이콘 svg...</span>]
      </div>
      ...
    </div>

뱃지(조조/더빙 등)가 회차 ID 와 잔여석 사이에 끼어도 안전하도록, 글자 수를
세지 않고 "다음 회차 블록 직전까지"를 그 회차의 범위로 삼는다.
"""

import re

TITLE_RE = re.compile(r'<div class="title">(.*?)</div>', re.S)
THEATER_RE = re.compile(r'<div class="theater-info">\s*(.*?)\s*<span class="all-seats">\s*\((\d{1,4})\s*석\)\s*</span>', re.S)
ATTR_RE = re.compile(r'data-([a-zA-Z]+)="([^"]*)"')
TIME_OPEN_RE = re.compile(r'<div class="time[^"]*"((?:\s+data-[a-zA-Z]+="[^"]*")+)')
FROM_TO_RE = re.compile(r'<span class="from">(\d{1,2}:\d{2})<span class="to">~(\d{1,2}:\d{2})</span>')
SEATS_RE = re.compile(r'(\d{1,4})\s*석')
DUB_RE = re.compile(r'aria-label="더빙"')
TAG_RE = re.compile(r"<[^>]+>")


def strip_tags(s):
    return re.sub(r"\s+", " ", TAG_RE.sub(" ", s)).strip()


CHECKBOX_RE = re.compile(
    r'<input[^>]*class="p-movie-check"[^>]*id="([^"]+)"[^>]*value="([^"]+)"[^>]*/>'
    r'|<input[^>]*id="([^"]+)"[^>]*value="([^"]+)"[^>]*class="p-movie-check"[^>]*/>')
LABEL_RE = re.compile(r'<label for="([^"]+)">(.*?)</label>', re.S)


def parse_movie_list(body):
    """
    /popup/ReserveMovieList 응답 -> [(제목, movieCode), ...]

    체크박스(id, value)와 그 id 를 가리키는 <label for="id"> 의 제목을 짝짓는다.
    순서가 어긋나도 맞게 찾도록 id 로 연결한다(앞뒤로 붙어 있다고 가정하지 않는다).
    """
    boxes = {}                           # id -> value
    for m in CHECKBOX_RE.finditer(body):
        if m.group(1):
            boxes[m.group(1)] = m.group(2)
        else:
            boxes[m.group(3)] = m.group(4)

    out = []
    for cid, label_html in LABEL_RE.findall(body):
        if cid not in boxes:
            continue
        # 등급 뱃지(<span class="rate-x">18</span> 등)가 제목 앞에 붙어 나온다.
        # 숫자가 제목에 섞이지 않도록 먼저 떼어낸다.
        clean = re.sub(r'<span class="rate[^"]*">.*?</span>', " ", label_html, flags=re.S)
        title = strip_tags(clean)
        out.append((title, boxes[cid]))
    return out


def pick_movies(movies, keywords):
    """
    감시 중인 키워드와 맞는 (제목, movieCode) 를 전부 돌려준다.
    더빙판/자막판처럼 같은 제목이 다른 코드로 여러 번 나올 수 있어 전부 모은다.
    """
    return [(t, c) for t, c in movies if any(kw and kw in t for kw in keywords)]


def parse_screen_plan(body):
    """
    응답 전체 -> [{"movie": 영화명, "screen": "4관", "capacity": 140,
                  "screenplanid": "1111366", "time": "13:10~14:56",
                  "seats": 138 또는 None, "soldout": bool,
                  "attrs": {screenplanid, playdate, playnumber, ...}}, ...]

    잔여석 숫자를 못 찾으면 seats=None (매진 문구가 있으면 soldout=True, 0 으로 본다).
    """
    out = []
    # 영화명 블록 경계로 자른다. 블록 안에 상영관이 여러 번 나올 수 있다.
    starts = [m.start() for m in TITLE_RE.finditer(body)]
    if not starts:
        return out
    starts.append(len(body))

    for i in range(len(starts) - 1):
        block = body[starts[i]:starts[i + 1]]
        tm = TITLE_RE.search(block)
        if tm:
            clean = re.sub(r'<span class="rate[^"]*">.*?</span>', " ", tm.group(1), flags=re.S)
            movie = strip_tags(clean)
        else:
            movie = "?"

        th = THEATER_RE.search(block)
        if not th:
            continue
        screen = strip_tags(th.group(1))
        capacity = int(th.group(2))

        # 이 상영관 블록 안의 회차들. 다음 "<div class=\"time\"" 직전까지가
        # 한 회차의 범위다 - 뱃지가 몇 개 끼든 안전하다.
        opens = list(TIME_OPEN_RE.finditer(block, th.end()))
        for j, om in enumerate(opens):
            seg_end = opens[j + 1].start() if j + 1 < len(opens) else len(block)
            seg = block[om.start():seg_end]
            attrs = dict(ATTR_RE.findall(om.group(1)))
            sid = attrs.get("screenplanid") or ""
            if not sid:
                continue

            ft = FROM_TO_RE.search(seg)
            time_range = "%s~%s" % ft.groups() if ft else "?"

            # 정확한 숫자가 있으면 그것을 믿는다 - 글자("매진")와 숫자가 둘 다
            # 있는데 서로 어긋나는 경우, 좌석이 실제로 풀렸는데도 "매진" 글자
            # 때문에 묻혀서 알림을 놓치면 안 된다. 숫자가 아예 없을 때만
            # "매진" 글자에 기댄다.
            sm = SEATS_RE.search(seg)
            if sm:
                seats = int(sm.group(1))
                soldout = seats == 0
            else:
                soldout = "매진" in seg
                seats = 0 if soldout else None

            # 더빙판은 영화 코드가 자막판과 같아서 한 응답 안에 섞여 나오고,
            # <span class="screen-attribute dub" aria-label="더빙">더</span>
            # 뱃지로만 구분된다. 이 뱃지를 안 읽으면 더빙 회차가 일반판과
            # 구분 없이 섞여 나온다 - 실제로 그래서 못 알아봤다.
            dub = bool(DUB_RE.search(seg))

            out.append({
                "movie": movie, "screen": screen, "capacity": capacity,
                "screenplanid": sid, "time": time_range,
                "seats": seats, "soldout": soldout, "dub": dub,
                "attrs": attrs,
            })
    return out


def to_items(plan_rows):
    """
    parse_screen_plan() 의 평평한 회차 목록 ->
    watch.py 의 parse_html() 과 똑같은 모양 [{name, groups:[{name, slots:[...]}]}].

    이렇게 모양을 맞춰 두면 watch.py 에서 그대로 가져온 match_keywords,
    slot_key, observe_slots, format_items, raise_alert 등을 한 글자도
    고치지 않고 그대로 쓸 수 있다. "시간표를 가져오는 코드"만 바뀐 것이지,
    그 뒤의 판단/알림 로직은 바뀐 게 없다는 뜻이다.

    note 는 "138/140" 또는 "매진" - seats.py 의 list_count()/needs_closer_look()
    가 읽던 바로 그 형식이다(다만 이 소스는 애초에 정확한 숫자를 주므로
    좌석 배치도를 따로 셀 필요가 없다. watch_popup.py 쪽 설명 참고).
    """
    order = []                       # (movie, screen_label) 순서 보존
    by_group = {}
    for row in plan_rows:
        # watch.py 가 쓰던 원래 목록 페이지는 더빙판을 "영화명 (더빙)" 이라는
        # 별도 항목으로 줬다. 팝업은 한 영화 코드 안에 뱃지로만 섞어 주므로,
        # 여기서 이름을 갈라 그 표시 관례를 그대로 맞춘다.
        movie = row["movie"] + " (더빙)" if row["dub"] else row["movie"]
        label = "%s (%d석)" % (row["screen"], row["capacity"])
        gkey = (movie, label)
        if gkey not in by_group:
            by_group[gkey] = []
            order.append(gkey)
        note = "매진" if row["soldout"] else "%d/%d" % (row["seats"], row["capacity"])
        by_group[gkey].append({
            "time": row["time"], "note": note, "attrs": row["attrs"],
        })

    items_by_movie = {}
    movie_order = []
    for movie, label in order:
        if movie not in items_by_movie:
            items_by_movie[movie] = []
            movie_order.append(movie)
        items_by_movie[movie].append({"name": label, "slots": by_group[(movie, label)]})

    return [{"name": m, "groups": items_by_movie[m]} for m in movie_order]
