"""
시가총액 미달 종목의 거래소 공시 단계(공시 기반) 수집
===================================================
대시보드의 "관리종목 지정 대상"은 시총과 연속일수로 계산한 값이라 실제 지정과 다를 수 있다.
그래서 OpenDART 거래소공시(pblntf_ty=I) 제목에서 관리종목 관련 공시 단계를 찾아 대사용으로 보여준다.
최신 관련 공시 1건을 쓰고, 해당 공시가 없으면 단계를 비운다(null). 최종 확인은 KRX KIND에서 한다.

단계 라벨
  관리종목 지정 해제 / 관리종목 지정 우려(예고) / 관리종목 지정 사유 발생 / 관리종목 지정 / 상장폐지 우려 안내

data/designation_stage.json: { 종목코드: { stage: {label, detail, date, report_nm, rcept_no} | null, checked_at } }
  대상은 data/summary.json 기준 거래정지가 아니면서 현행 기준 미달(연속미달 1일 이상)인 종목이다.

사용법
  python designation_stage.py update
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
from halt_reasons import fetch_exchange_reports, norm

BASE_DIR = Path(__file__).resolve().parent
SUMMARY_PATH = BASE_DIR / "data" / "summary.json"
STAGE_PATH = BASE_DIR / "data" / "designation_stage.json"
CACHE_VERSION = 2  # 분류 규칙이 바뀌면 올려서 당일 캐시를 무효화한다

LOOKBACK_DAYS = 400
# index.html의 REG.current와 같은 값으로 유지한다(현행 시가총액 기준, 억원).
CURRENT_THRESHOLD = {"KOSPI": 300, "KOSDAQ": 200}

PAREN_GROUPS = re.compile(r"\(([^()]*)\)")


def get_auth_key():
    key = os.environ.get("OPENDART_API_KEY")
    return key.strip() if key else key


def classify_stage(report_nm):
    name = norm(report_nm)
    if "관리종목" in name:
        if "해제" in name:
            return "관리종목 지정 해제"
        if re.search(r"우려|예고", name):
            return "관리종목 지정 우려(예고)"
        if re.search(r"사유\s*발생", name):
            return "관리종목 지정 사유 발생"
        return "관리종목 지정"
    if "상장폐지" in name and "우려" in name:
        return "상장폐지 우려 안내"
    return None


def find_stage(rows):
    """최신순 공시 목록에서 처음 일치하는 단계를 반환한다."""
    for row in rows:
        label = classify_stage(row.get("report_nm"))
        if label:
            name = norm(row.get("report_nm"))
            groups = PAREN_GROUPS.findall(name)
            return {
                "label": label,
                "detail": groups[-1].strip() if groups else "",
                "date": row.get("rcept_dt"),
                "report_nm": name,
                "rcept_no": row.get("rcept_no"),
            }
    return None


def below_cap_codes(summary):
    """거래정지가 아니고 현행 기준 미달 연속일수가 1 이상인 종목코드."""
    codes = []
    for s in summary["stocks"]:
        if s["h"][-1] == "1":
            continue
        threshold = CURRENT_THRESHOLD.get(s["m"], CURRENT_THRESHOLD["KOSPI"])
        streak = 0
        for v in reversed(s["cap"]):
            if v is None:
                continue
            if v < threshold:
                streak += 1
            else:
                break
        if streak >= 1:
            codes.append(s["c"])
    return sorted(codes)


def load_cache():
    if not STAGE_PATH.exists():
        return {}
    with open(STAGE_PATH, "r", encoding="utf-8") as f:
        return json.load(f)


def save_cache(cache):
    with open(STAGE_PATH, "w", encoding="utf-8") as f:
        json.dump(cache, f, ensure_ascii=False, indent=0, separators=(",", ":"))


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
            found += 1 if entry.get("stage") else 0
            continue
        corp_code = corp_map.get(code)
        stage = None
        if corp_code:
            try:
                stage = find_stage(fetch_exchange_reports(key, corp_code, bgn_de, today))
            except Exception as e:
                print(f"[designation_stage] {code} 오류: {financials.redact(e)}")
                if entry:
                    result[code] = entry  # 기존 값 유지, 다음 실행에서 다시 조회
                    found += 1 if entry.get("stage") else 0
                    continue
            time.sleep(0.15)
        result[code] = {"stage": stage, "checked_at": today, "v": CACHE_VERSION}
        found += 1 if stage else 0

    save_cache(result)
    print(f"[designation_stage] 완료: 대상 {len(codes)}개 종목 중 단계 확인 {found}개, 공란 {len(codes) - found}개")


if __name__ == "__main__":
    key = get_auth_key()
    if not key:
        print("환경변수 OPENDART_API_KEY가 필요합니다.")
        sys.exit(1)
    if len(sys.argv) < 2 or sys.argv[1] != "update":
        print(__doc__)
        sys.exit(1)
    if not SUMMARY_PATH.exists():
        print("data/summary.json이 없습니다. 먼저 build_summary.py를 실행하세요.")
        sys.exit(1)
    summary = json.load(open(SUMMARY_PATH, encoding="utf-8"))
    update(below_cap_codes(summary), key)
