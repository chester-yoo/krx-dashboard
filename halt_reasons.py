"""
거래정지 사유(공시 기반 추정) 수집
=================================
KRX가 정지 사유를 API로 제공하지 않아서, 거래정지 종목의 OpenDART 거래소공시(pblntf_ty=I)에서 추정한다.
  1순위: "주권매매거래정지 (사유)" 공시의 괄호 문구(마지막 정지해제 이후 최신 건)
  2순위: 공시 제목의 사유 키워드(우려·예고 공시는 제외)
근거가 없으면 사유를 비워 둔다(null). 정확한 사유는 KRX KIND에서 확인해야 한다.

data/halt_reasons.json: { 종목코드: { reason: {label, date, report_nm, rcept_no} | null, checked_at } }
  현재 거래정지 상태인 종목만 담는다(정지가 풀린 종목은 다음 실행에서 제거).

사용법
  python halt_reasons.py update
"""
import json
import os
import re
import sys
import time
from datetime import datetime, timedelta
from pathlib import Path

import requests

import financials  # corp_code 매핑 재사용

BASE_DIR = Path(__file__).resolve().parent
HISTORY_PATH = BASE_DIR / "data" / "history.json"
REASONS_PATH = BASE_DIR / "data" / "halt_reasons.json"
CACHE_VERSION = 3  # 분류 규칙이 바뀌면 올려서 당일 캐시를 무효화한다

API_BASE = "https://opendart.fss.or.kr/api"
LOOKBACK_DAYS = 365 * 2
MAX_PAGES = 3  # 종목당 최대 300건(최신순)까지만 확인

# (패턴, 표시 라벨). 위에서부터 먼저 일치한 규칙을 쓴다.
RULES = [
    (re.compile(r"횡령|배임"), "횡령·배임 혐의"),
    (re.compile(r"감사의견|의견거절|감사범위\s*제한"), "감사의견 관련"),
    (re.compile(r"상장적격성|기업심사위원회|개선기간|개선계획"), "상장적격성 실질심사"),
    (re.compile(r"상장폐지"), "상장폐지 절차"),
    (re.compile(r"관리종목"), "관리종목 지정 관련"),
    (re.compile(r"회생절차|파산|해산"), "회생·파산 절차"),
    (re.compile(r"제출\s*지연|미제출"), "보고서 제출 지연·미제출"),
]


def get_auth_key():
    key = os.environ.get("OPENDART_API_KEY")
    return key.strip() if key else key


HALT_NOTICE = re.compile(r"주권\s*매매\s*거래\s*정지")


def top_level_groups(text):
    """가장 바깥 괄호의 내용을 순서대로 반환한다(중첩 괄호는 안쪽까지 포함)."""
    groups, depth, start = [], 0, None
    for i, ch in enumerate(text):
        if ch == "(":
            if depth == 0:
                start = i + 1
            depth += 1
        elif ch == ")" and depth > 0:
            depth -= 1
            if depth == 0 and start is not None:
                groups.append(text[start:i])
    return groups
TECHNICAL_HALT = re.compile(r"전자등록|병합|분할|감자|자본감소")


def norm(text):
    return re.sub(r"\s+", " ", text or "").strip()


def classify(report_nm):
    name = norm(report_nm)
    for pattern, label in RULES:
        if pattern.search(name):
            return label
    return None


def classify_halt_detail(detail):
    """'주권매매거래정지 (…)' 공시의 괄호 문구를 사유 라벨로 바꾼다. 알 수 없으면 원문(최대 30자)을 쓴다."""
    detail = norm(detail)
    if not detail:
        return None
    if detail.replace(" ", "") == "투자자보호":
        return "투자자 보호"
    if "상장폐지" in detail and "사유" in detail:
        return "상장폐지 사유 발생"
    if TECHNICAL_HALT.search(detail):
        return "주식 병합·분할 등(기술적 정지)"
    for pattern, label in RULES:
        if pattern.search(detail):
            return label
    return detail[:30]


def make_reason(row, label):
    return {"label": label, "date": row.get("rcept_dt"), "report_nm": norm(row.get("report_nm")), "rcept_no": row.get("rcept_no")}


def halt_notice_reason(rows):
    """'주권매매거래정지' 공시 중 마지막 정지해제 이후의 것에서 괄호 사유가 있는 가장 최신 건을 쓴다."""
    release = None
    notices = []
    for row in rows:  # 최신순
        name = norm(row.get("report_nm"))
        if not HALT_NOTICE.search(name):
            continue
        key = (row.get("rcept_dt") or "", row.get("rcept_no") or "")
        if "해제" in name:
            if release is None:
                release = key
            continue
        notices.append((key, row, name))
    for key, row, name in notices:
        if release is not None and key <= release:
            continue
        groups = top_level_groups(name)
        if groups:
            label = classify_halt_detail(groups[-1])
            if label:
                return make_reason(row, label)
    return None


def find_reason(rows):
    """1순위: 주권매매거래정지 공시의 괄호 사유. 2순위: 최신 공시 제목의 키워드. 없으면 None(공란)."""
    reason = halt_notice_reason(rows)
    if reason:
        return reason
    for row in rows:
        if re.search(r"우려|예고|해제", norm(row.get("report_nm"))):
            continue  # 우려·예고는 사전 안내, 해제는 정지가 풀린 공시라 사유가 아니다
        label = classify(row.get("report_nm"))
        if label:
            return make_reason(row, label)
    return None


def fetch_exchange_reports(key, corp_code, bgn_de, end_de):
    rows = []
    for page in range(1, MAX_PAGES + 1):
        resp = requests.get(
            API_BASE + "/list.json",
            params={
                "crtfc_key": key, "corp_code": corp_code, "pblntf_ty": "I",
                "bgn_de": bgn_de, "end_de": end_de,
                "page_no": page, "page_count": 100, "sort": "date", "sort_mth": "desc",
            },
            timeout=20,
        )
        data = resp.json()
        if data.get("status") != "000":
            break
        rows.extend(data.get("list", []))
        if page >= int(data.get("total_page", 1)):
            break
        time.sleep(0.15)
    return rows


def load_cache():
    if not REASONS_PATH.exists():
        return {}
    with open(REASONS_PATH, "r", encoding="utf-8") as f:
        return json.load(f)


def save_cache(cache):
    with open(REASONS_PATH, "w", encoding="utf-8") as f:
        json.dump(cache, f, ensure_ascii=False, indent=0, separators=(",", ":"))


def halted_codes(history):
    last = max(r["date"] for r in history)
    return sorted({r["code"] for r in history if r["date"] == last and r.get("halted")})


def update(codes, key):
    corp_map = financials.get_corp_code_map(key)
    cache = load_cache()
    today = datetime.now().strftime("%Y%m%d")
    bgn_de = (datetime.now() - timedelta(days=LOOKBACK_DAYS)).strftime("%Y%m%d")

    result = {}
    found = 0
    for code in codes:
        entry = cache.get(code)
        if entry and entry.get("checked_at") == today and entry.get("v") == CACHE_VERSION:
            result[code] = entry
            found += 1 if entry.get("reason") else 0
            continue
        corp_code = corp_map.get(code)
        reason = None
        if corp_code:
            try:
                reason = find_reason(fetch_exchange_reports(key, corp_code, bgn_de, today))
            except Exception as e:
                print(f"[halt_reasons] {code} 오류: {financials.redact(e)}")
                reason = (entry or {}).get("reason")
            time.sleep(0.15)
        result[code] = {"reason": reason, "checked_at": today, "v": CACHE_VERSION}
        found += 1 if reason else 0

    save_cache(result)
    print(f"[halt_reasons] 완료: 거래정지 {len(codes)}개 종목 중 사유 확인 {found}개, 공란 {len(codes) - found}개")


if __name__ == "__main__":
    key = get_auth_key()
    if not key:
        print("환경변수 OPENDART_API_KEY가 필요합니다.")
        sys.exit(1)
    if len(sys.argv) < 2 or sys.argv[1] != "update":
        print(__doc__)
        sys.exit(1)
    if not HISTORY_PATH.exists():
        print("data/history.json이 없습니다. 먼저 collect.py를 실행하세요.")
        sys.exit(1)
    history = json.load(open(HISTORY_PATH, encoding="utf-8"))
    update(halted_codes(history), key)
