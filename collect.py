"""
KRX 일자별 시가총액 수집 - 로컬 Python 버전
=============================================
Google Sheets/Apps Script 없이 로컬 JSON 파일(data/history.json)에 누적 저장한다.

사용법
  python collect.py daily              최근 영업일(어제) 데이터 수집
  python collect.py manual YYYYMMDD    특정 날짜 수집
  python collect.py backfill START END 기간(YYYYMMDD~YYYYMMDD) 평일 전체 백필

작업 스케줄러(schtasks)로 "daily"를 매일 오전 9시(KST)에 등록해두면
Code.gs의 setupDailyTrigger()와 동일한 효과를 낸다.
"""
import json
import os
import re
import sys
import time
from datetime import datetime, timedelta
from pathlib import Path
from zoneinfo import ZoneInfo

import requests

import industry

BASE_DIR = Path(__file__).resolve().parent
CONFIG_PATH = BASE_DIR / "config.json"
HISTORY_PATH = BASE_DIR / "data" / "history.json"
LOG_PATH = BASE_DIR / "data" / "collect.log"

API_BASE = "https://data-dbg.krx.co.kr/svc/apis/sto"
ENDPOINTS = {"KOSPI": "/stk_bydd_trd", "KOSDAQ": "/ksq_bydd_trd"}

# 우선주는 종목명이 "...우", "...우A", "...우B", "...2우B", "...2우(전환)" 같은 형태로 끝남
PREFERRED_STOCK_RE = re.compile(r"\d?우[A-Z]?(\(전환\))?$")

KST = ZoneInfo("Asia/Seoul")


def log(msg):
    line = f"[{datetime.now(KST).strftime('%Y-%m-%d %H:%M:%S')}] {msg}"
    print(line)
    LOG_PATH.parent.mkdir(parents=True, exist_ok=True)
    with open(LOG_PATH, "a", encoding="utf-8") as f:
        f.write(line + "\n")


def load_config():
    file_config = {}
    if CONFIG_PATH.exists():
        with open(CONFIG_PATH, "r", encoding="utf-8") as f:
            file_config = json.load(f)
    auth_key = os.environ.get("KRX_AUTH_KEY") or file_config.get("auth_key")
    return {
        "auth_key": auth_key.strip() if auth_key else auth_key,
        "thresholds_eok": file_config.get("thresholds_eok") or {"KOSPI": 300, "KOSDAQ": 200},
    }


def load_history():
    if not HISTORY_PATH.exists():
        return []
    with open(HISTORY_PATH, "r", encoding="utf-8") as f:
        return json.load(f)


def save_history(records):
    HISTORY_PATH.parent.mkdir(parents=True, exist_ok=True)
    with open(HISTORY_PATH, "w", encoding="utf-8") as f:
        json.dump(records, f, ensure_ascii=False, indent=0, separators=(",", ":"))


def is_excluded_name(name):
    if "스팩" in name:
        return True
    if PREFERRED_STOCK_RE.search(name):
        return True
    return False


def to_number(v):
    if v is None:
        return 0
    try:
        return float(str(v).replace(",", ""))
    except ValueError:
        return 0


def krx_fetch(endpoint, bas_dd, auth_key):
    resp = requests.post(
        API_BASE + endpoint,
        headers={"AUTH_KEY": auth_key, "Content-Type": "application/json"},
        json={"basDd": bas_dd},
        timeout=30,
    )
    if resp.status_code != 200:
        raise RuntimeError(f"KRX API 오류 {resp.status_code}: {resp.text[:300]}")
    data = resp.json()
    return data.get("OutBlock_1", [])


def collect_for_date(bas_dd, config, history):
    existing_keys = {f"{r['date']}|{r['code']}" for r in history}
    thresholds = config["thresholds_eok"]
    auth_key = config["auth_key"]

    new_rows = []
    for market, endpoint in ENDPOINTS.items():
        rows = krx_fetch(endpoint, bas_dd, auth_key)
        if not rows:
            log(f"{market} {bas_dd}: 데이터 없음 (휴장일이거나 미갱신)")
            continue
        threshold = thresholds[market]
        for r in rows:
            name = r.get("ISU_NM", "")
            if is_excluded_name(name):
                continue
            close = to_number(r.get("TDD_CLSPRC"))
            list_shares = to_number(r.get("LIST_SHRS"))
            mktcap_won = to_number(r.get("MKTCAP"))
            mktcap_eok = round((mktcap_won / 100000000) * 100) / 100
            date = r.get("BAS_DD") or bas_dd
            code = str(r.get("ISU_CD", "")).strip()
            if code.isdigit() and len(code) < 6:
                code = code.zfill(6)
            key = f"{date}|{code}"
            if key in existing_keys:
                continue
            # 거래량 0 + 시가/고가/저가/등락 전부 0 = 그날 매매가 아예 없었던 것으로,
            # 거래정지의 근사치로 사용한다 (KRX API에 별도 거래정지 필드는 없음).
            volume = to_number(r.get("ACC_TRDVOL"))
            halted = (
                volume == 0
                and to_number(r.get("TDD_OPNPRC")) == 0
                and to_number(r.get("TDD_HGPRC")) == 0
                and to_number(r.get("TDD_LWPRC")) == 0
            )
            new_rows.append({
                "date": date,
                "market": market,
                "code": code,
                "name": name,
                "close": close,
                "list_shares": list_shares,
                "market_cap_eok": mktcap_eok,
                "below_threshold": 1 if mktcap_eok < threshold else 0,
                "halted": halted,
            })
        log(f"{market} {bas_dd}: {len(rows)}개 종목 처리")

    if new_rows:
        history.extend(new_rows)
        save_history(history)
        log(f"{len(new_rows)}행 추가 완료")
        new_codes = {r["code"] for r in new_rows}
        try:
            industry.update_missing(new_codes)
        except Exception as e:
            log(f"업종 조회 오류(무시하고 계속): {e}")
    else:
        log("추가할 신규 데이터 없음")
    return new_rows


def collect_daily():
    config = load_config()
    history = load_history()
    yesterday = datetime.now(KST) - timedelta(days=1)
    bas_dd = yesterday.strftime("%Y%m%d")
    collect_for_date(bas_dd, config, history)


def collect_manual(bas_dd):
    config = load_config()
    history = load_history()
    collect_for_date(bas_dd, config, history)


def backfill(start_str, end_str):
    config = load_config()
    history = load_history()
    start = datetime.strptime(start_str, "%Y%m%d")
    end = datetime.strptime(end_str, "%Y%m%d")
    cur = start
    count = 0
    while cur <= end:
        if cur.weekday() < 5:  # 0=월 ... 4=금, 주말(5,6) 제외
            bas_dd = cur.strftime("%Y%m%d")
            log(f"[{bas_dd}] 수집 시작...")
            try:
                collect_for_date(bas_dd, config, history)
            except Exception as e:
                log(f"  오류: {e}")
            count += 1
            time.sleep(0.4)
        cur += timedelta(days=1)
    log(f"백필 완료: {count}개 평일 처리")


if __name__ == "__main__":
    if len(sys.argv) < 2:
        print(__doc__)
        sys.exit(1)

    cmd = sys.argv[1]
    if cmd == "daily":
        collect_daily()
    elif cmd == "manual":
        if len(sys.argv) < 3:
            print("사용법: python collect.py manual YYYYMMDD")
            sys.exit(1)
        collect_manual(sys.argv[2])
    elif cmd == "backfill":
        if len(sys.argv) < 4:
            print("사용법: python collect.py backfill START END")
            sys.exit(1)
        backfill(sys.argv[2], sys.argv[3])
    else:
        print(__doc__)
        sys.exit(1)
