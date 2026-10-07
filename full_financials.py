"""
OpenDART 전체 재무제표(fnlttSinglAcntAll) 수집 — EV/EBITDA·순현금·영업현금흐름·이자보상배율용
==============================================================================
주요계정 API에는 차입금·현금성자산·감가상각비·영업현금흐름·이자비용이 없어서 전체 재무제표를 받는다.
이 API는 회사 하나씩만 조회되므로(연결 없으면 별도로 한 번 더) 매일 정기 작업과 분리해 수동 실행한다.

사용법
  python full_financials.py sample   표본 10개사의 응답 원본과 호출 시간을 data/full_sample.json에 저장
                                     (계정 매칭 규칙과 전체 수집 소요 시간을 정하기 위한 시험)
"""
import json
import os
import sys
import time
from datetime import datetime
from pathlib import Path

import requests

import financials  # corp_code 매핑 재사용

BASE_DIR = Path(__file__).resolve().parent
SAMPLE_PATH = BASE_DIR / "data" / "full_sample.json"
API_BASE = "https://opendart.fss.or.kr/api"

# 대형 연결 / 금융사 / 별도만 있는 소형사 / 거래정지 / 시가총액 미달 / 사전검토 종목을 섞은 표본
SAMPLE_CODES = ["005930", "000660", "105560", "002360", "060480", "011080", "001570", "312610", "035620", "024070"]
ROW_KEYS = ("sj_div", "account_id", "account_nm", "account_detail", "thstrm_amount", "frmtrm_amount", "bfefrmtrm_amount")


def log(message):
    print(message, flush=True)


def get_auth_key():
    key = os.environ.get("OPENDART_API_KEY")
    return key.strip() if key else key


def fetch_full(key, corp_code, year, fs_div):
    resp = requests.get(
        API_BASE + "/fnlttSinglAcntAll.json",
        params={"crtfc_key": key, "corp_code": corp_code, "bsns_year": year, "reprt_code": "11011", "fs_div": fs_div},
        timeout=60,
    )
    data = resp.json()
    return data.get("status"), data.get("message"), data.get("list", [])


def sample(key):
    corp_map = financials.get_corp_code_map(key)
    year = str(datetime.now().year - 1)
    out = {"year": year, "fetched_at": datetime.now().isoformat(timespec="seconds"), "companies": {}}
    total_started = time.time()
    for code in SAMPLE_CODES:
        corp_code = corp_map.get(code)
        if not corp_code:
            log(f"[full] {code}: corp_code 없음")
            continue
        entry = {"calls": []}
        for fs_div in ("CFS", "OFS"):
            started = time.time()
            try:
                status, message, rows = fetch_full(key, corp_code, year, fs_div)
            except Exception as e:
                status, message, rows = "ERR", str(e), []
            elapsed = round(time.time() - started, 2)
            entry["calls"].append({"fs_div": fs_div, "status": status, "message": message, "rows": len(rows), "sec": elapsed})
            log(f"[full] {code} {fs_div}: status={status} rows={len(rows)} {elapsed}초")
            if status == "000" and rows:
                entry["fs_div"] = fs_div
                entry["rows"] = [{k: r.get(k) for k in ROW_KEYS} for r in rows]
                break
            time.sleep(0.2)
        out["companies"][code] = entry
        time.sleep(0.2)
    out["total_sec"] = round(time.time() - total_started, 1)
    SAMPLE_PATH.parent.mkdir(parents=True, exist_ok=True)
    with open(SAMPLE_PATH, "w", encoding="utf-8") as f:
        json.dump(out, f, ensure_ascii=False, separators=(",", ":"))
    log(f"[full] 표본 완료: {len(out['companies'])}개사, 총 {out['total_sec']}초 -> {SAMPLE_PATH.name}")


if __name__ == "__main__":
    key = get_auth_key()
    if not key:
        print("환경변수 OPENDART_API_KEY가 필요합니다.")
        sys.exit(1)
    cmd = sys.argv[1] if len(sys.argv) > 1 else ""
    if cmd == "sample":
        sample(key)
    else:
        print(__doc__)
        sys.exit(1)
