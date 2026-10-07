"""
거래정지 사유(공시 기반 추정) 수집
=================================
KRX가 정지 사유를 API로 제공하지 않아서, 거래정지 종목의 OpenDART 거래소공시(pblntf_ty=I) 제목에서
사유 관련 키워드를 찾아 "추정 사유"로 표시한다. 최신 공시부터 확인해 처음 일치한 항목을 쓰고,
일치하는 공시가 없으면 사유를 비워 둔다(null). 정확한 사유는 KRX KIND에서 확인해야 한다.

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
    (re.compile(r"매매거래정지"), "매매거래정지 공시"),
    (re.compile(r"제출\s*지연|미제출"), "보고서 제출 지연·미제출"),
]


def get_auth_key():
    key = os.environ.get("OPENDART_API_KEY")
    return key.strip() if key else key


def classify(report_nm):
    name = re.sub(r"\s+", " ", report_nm or "")
    for pattern, label in RULES:
        if pattern.search(name):
            return label
    return None


def find_reason(rows):
    """최신순 공시 목록에서 처음 일치하는 사유를 반환한다."""
    for row in rows:
        label = classify(row.get("report_nm"))
        if label:
            return {
                "label": label,
                "date": row.get("rcept_dt"),
                "report_nm": re.sub(r"\s+", " ", row.get("report_nm", "")).strip(),
                "rcept_no": row.get("rcept_no"),
            }
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
        if entry and entry.get("checked_at") == today:
            result[code] = entry
            found += 1 if entry.get("reason") else 0
            continue
        corp_code = corp_map.get(code)
        reason = None
        if corp_code:
            try:
                reason = find_reason(fetch_exchange_reports(key, corp_code, bgn_de, today))
            except Exception as e:
                print(f"[halt_reasons] {code} 오류: {e}")
                reason = (entry or {}).get("reason")
            time.sleep(0.15)
        result[code] = {"reason": reason, "checked_at": today}
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
