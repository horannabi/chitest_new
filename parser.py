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

            soldout = "매진" in seg
            sm = SEATS_RE.search(seg)
            seats = int(sm.group(1)) if sm else (0 if soldout else None)

            out.append({
                "movie": movie, "screen": screen, "capacity": capacity,
                "screenplanid": sid, "time": time_range,
                "seats": seats, "soldout": soldout or seats == 0,
                "attrs": attrs,
            })
    return out
