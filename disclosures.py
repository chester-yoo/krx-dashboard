"""
OpenDART 주요사항보고(pblntf_ty=B) 최근 5건 수집
=================================================
data/disclosures.json: { 종목코드: [ {date, report_nm, rcept_no}, ... 최대 5건, 최신순 ] }

공시는 매번 최신 상태가 바뀔 수 있으므로 오래된 항목은 --refresh-days 이상 지나면
다시 조회한다. 중간에 중단돼도 이어서 실행하면 이미 처리한 종목은 건너뛴다.

사용법
  python disclosures.py update
"""
import json
import os
import sys
import time
from datetime import datetime, timedelta
from pathlib import Path

import requests

import financials  # corp_code 매핑 재사용

BASE_DIR = Path(__file__).resolve().parent
HISTORY_PATH = BASE_DIR / "data" / "history.json"
DISCLOSURES_PATH = BASE_DIR / "data" / "disclosures.json"

API_BASE = "https://opendart.fss.or.kr/api"
LOOKBACK_DAYS = 365 * 3  # 최근 3년 내에서 최신 5건을 찾는다


def get_auth_key():
    return os.environ.get("OPENDART_API_KEY")


def fetch_recent_major_reports(key, corp_code, bgn_de, end_de):
    resp = requests.get(
        API_BASE + "/list.json",
        params={
            "crtfc_key": key, "corp_code": corp_code, "pblntf_ty": "B",
            "bgn_de": bgn_de, "end_de": end_de,
            "page_no": 1, "page_count": 5, "sort": "date", "sort_mth": "desc",
        },
        timeout=20,
    )
    data = resp.json()
    if data.get("status") != "000":
        return []
    return [
        {"date": row.get("rcept_dt"), "report_nm": row.get("report_nm"), "rcept_no": row.get("rcept_no")}
        for row in data.get("list", [])[:5]
    ]


def load_cache():
    if not DISCLOSURES_PATH.exists():
        return {}
    with open(DISCLOSURES_PATH, "r", encoding="utf-8") as f:
        return json.load(f)


def save_cache(cache):
    with open(DISCLOSURES_PATH, "w", encoding="utf-8") as f:
        json.dump(cache, f, ensure_ascii=False, indent=0, separators=(",", ":"))


def update(codes, key, refresh_days=1):
    corp_map = financials.get_corp_code_map(key)
    cache = load_cache()
    today = datetime.now().strftime("%Y%m%d")
    end_de = today
    bgn_de = (datetime.now() - timedelta(days=LOOKBACK_DAYS)).strftime("%Y%m%d")

    checked = 0
    fetched = 0
    for code in dict.fromkeys(codes):
        entry = cache.get(code)
        if entry and entry.get("checked_at") == today:
            continue  # 이미 오늘 처리함 (재시작 시 이어하기)
        corp_code = corp_map.get(code)
        if not corp_code:
            continue
        try:
            reports = fetch_recent_major_reports(key, corp_code, bgn_de, end_de)
        except Exception as e:
            print(f"[disclosures] {code} 오류: {e}")
            reports = (entry or {}).get("reports", [])
        cache[code] = {"reports": reports, "checked_at": today}
        checked += 1
        fetched += 1
        if fetched % 20 == 0:
            save_cache(cache)
            print(f"[disclosures] 진행: {fetched}건 처리 (전체 캐시 {len(cache)}개 종목)")
        time.sleep(0.15)

    save_cache(cache)
    print(f"[disclosures] 완료: 이번 실행 {fetched}건 처리, 전체 캐시 {len(cache)}개 종목")


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
    codes = sorted({r["code"] for r in history})
    update(codes, key)
