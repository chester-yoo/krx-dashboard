"""
OpenDART 주요사항보고(pblntf_ty=B) 최근 5건 수집
=================================================
data/disclosures.json: { 종목코드: [ {date, report_nm, rcept_no}, ... 최대 5건, 최신순 ] }

재무정보(financials.py)와 달리 매번 최신 상태로 덮어써야 하므로 캐시하지 않고
매 실행마다 전체 종목을 다시 조회한다.

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


def update(codes, key):
    corp_map = financials.get_corp_code_map(key)
    result = {}
    end_de = datetime.now().strftime("%Y%m%d")
    bgn_de = (datetime.now() - timedelta(days=LOOKBACK_DAYS)).strftime("%Y%m%d")

    checked = 0
    for code in dict.fromkeys(codes):
        corp_code = corp_map.get(code)
        if not corp_code:
            continue
        try:
            reports = fetch_recent_major_reports(key, corp_code, bgn_de, end_de)
        except Exception as e:
            print(f"[disclosures] {code} 오류: {e}")
            reports = []
        if reports:
            result[code] = reports
        checked += 1
        if checked % 200 == 0:
            with open(DISCLOSURES_PATH, "w", encoding="utf-8") as f:
                json.dump(result, f, ensure_ascii=False, indent=0, separators=(",", ":"))
            print(f"[disclosures] 진행: {checked}건 확인, {len(result)}개 종목에 공시 있음")
        time.sleep(0.15)

    with open(DISCLOSURES_PATH, "w", encoding="utf-8") as f:
        json.dump(result, f, ensure_ascii=False, indent=0, separators=(",", ":"))
    print(f"[disclosures] 완료: {checked}개 종목 확인, {len(result)}개 종목에 주요사항보고 있음")


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
