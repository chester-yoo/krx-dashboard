"""
OpenDART 연도별 핵심 재무정보(매출액/영업이익/당기순이익/자산총계/부채총계/자본총계) 수집
=============================================================
KRX API에는 재무정보가 없어서 OpenDART(전자공시시스템) 공식 API를 사용한다.
data/corp_codes.json: 종목코드(6자리) -> DART corp_code(8자리) 매핑 캐시
data/financials.json: { 종목코드: { 사업연도: {revenue, operating_profit, net_income,
                                              assets, liabilities, equity, fs_div} } }

사용법
  python financials.py update [--years N]   history.json의 종목 중 최근 N개 연도(기본 3) 재무정보를
                                             캐시에 없는 것만 새로 조회
"""
import json
import os
import re
import sys
import time
import zipfile
import io
from datetime import datetime
from pathlib import Path

import requests

BASE_DIR = Path(__file__).resolve().parent
HISTORY_PATH = BASE_DIR / "data" / "history.json"
CORP_CODE_PATH = BASE_DIR / "data" / "corp_codes.json"
FINANCIALS_PATH = BASE_DIR / "data" / "financials.json"

API_BASE = "https://opendart.fss.or.kr/api"

REVENUE_NAMES = {"매출액", "수익(매출액)"}
OPERATING_PROFIT_NAMES = {"영업이익", "영업이익(손실)"}
NET_INCOME_NAMES = {"당기순이익(손실)", "당기순이익"}
ASSET_NAMES = {"자산총계"}
LIABILITY_NAMES = {"부채총계"}
EQUITY_NAMES = {"자본총계"}


def get_auth_key():
    return os.environ.get("OPENDART_API_KEY")


def to_number(v):
    if v is None or v == "":
        return None
    try:
        return int(str(v).replace(",", ""))
    except ValueError:
        return None


def load_json(path, default):
    if not path.exists():
        return default
    with open(path, "r", encoding="utf-8") as f:
        return json.load(f)


def save_json(path, data):
    path.parent.mkdir(parents=True, exist_ok=True)
    with open(path, "w", encoding="utf-8") as f:
        json.dump(data, f, ensure_ascii=False, indent=0, separators=(",", ":"))


def fetch_corp_codes(key):
    """DART 전체 기업코드 목록을 받아 상장 종목코드 -> corp_code 매핑을 만든다."""
    resp = requests.get(API_BASE + "/corpCode.xml", params={"crtfc_key": key}, timeout=60)
    resp.raise_for_status()
    z = zipfile.ZipFile(io.BytesIO(resp.content))
    xml_bytes = z.read(z.namelist()[0])
    text = xml_bytes.decode("utf-8")

    mapping = {}
    for m in re.finditer(
        r"<corp_code>(\d+)</corp_code>\s*<corp_name>.*?</corp_name>\s*"
        r"(?:<corp_eng_name>.*?</corp_eng_name>\s*)?<stock_code>\s*(\d*)\s*</stock_code>",
        text,
        re.S,
    ):
        corp_code, stock_code = m.group(1), m.group(2).strip()
        if stock_code:
            mapping[stock_code] = corp_code
    return mapping


def get_corp_code_map(key, refresh=False):
    if not refresh:
        cached = load_json(CORP_CODE_PATH, None)
        if cached:
            return cached
    mapping = fetch_corp_codes(key)
    save_json(CORP_CODE_PATH, mapping)
    return mapping


def fetch_financial_accounts(key, corp_code, year, reprt_code="11011"):
    resp = requests.get(
        API_BASE + "/fnlttSinglAcnt.json",
        params={"crtfc_key": key, "corp_code": corp_code, "bsns_year": year, "reprt_code": reprt_code},
        timeout=20,
    )
    data = resp.json()
    if data.get("status") != "000":
        return None

    result = {"CFS": {}, "OFS": {}}
    for row in data.get("list", []):
        name = row.get("account_nm", "")
        fs_div = row.get("fs_div", "")
        amount = to_number(row.get("thstrm_amount"))
        if fs_div not in result or amount is None:
            continue
        if name in REVENUE_NAMES and "revenue" not in result[fs_div]:
            result[fs_div]["revenue"] = amount
        elif name in OPERATING_PROFIT_NAMES and "operating_profit" not in result[fs_div]:
            result[fs_div]["operating_profit"] = amount
        elif name in NET_INCOME_NAMES and "net_income" not in result[fs_div]:
            result[fs_div]["net_income"] = amount
        elif name in ASSET_NAMES and "assets" not in result[fs_div]:
            result[fs_div]["assets"] = amount
        elif name in LIABILITY_NAMES and "liabilities" not in result[fs_div]:
            result[fs_div]["liabilities"] = amount
        elif name in EQUITY_NAMES and "equity" not in result[fs_div]:
            result[fs_div]["equity"] = amount

    # 연결(CFS) 우선, 없으면 개별(OFS)로 보완
    merged = dict(result["OFS"])
    merged.update(result["CFS"])
    if not merged:
        return None
    merged["fs_div"] = "CFS" if result["CFS"] else "OFS"
    return merged


def update(codes, years, key):
    corp_map = get_corp_code_map(key)
    financials = load_json(FINANCIALS_PATH, {})
    current_year = datetime.now().year
    target_years = [str(current_year - 1 - i) for i in range(years)]  # 최신 확정 사업보고서 기준

    checked = 0
    fetched = 0
    no_corp_code = 0
    for code in dict.fromkeys(codes):
        corp_code = corp_map.get(code)
        if not corp_code:
            no_corp_code += 1
            continue
        entry = financials.setdefault(code, {})
        for year in target_years:
            if year in entry:
                continue
            checked += 1
            try:
                data = fetch_financial_accounts(key, corp_code, year)
            except Exception as e:
                print(f"[financials] {code} {year} 오류: {e}")
                data = None
            if data:
                entry[year] = data
                fetched += 1
            time.sleep(0.15)
        if not entry:
            del financials[code]
        if checked and checked % 200 == 0:
            save_json(FINANCIALS_PATH, financials)
            print(f"[financials] 진행: {checked}건 조회, {fetched}건 확보")

    save_json(FINANCIALS_PATH, financials)
    print(f"[financials] 완료: 조회 {checked}건, 확보 {fetched}건, corp_code 없음 {no_corp_code}개 종목")


if __name__ == "__main__":
    key = get_auth_key()
    if not key:
        print("환경변수 OPENDART_API_KEY가 필요합니다.")
        sys.exit(1)
    if len(sys.argv) < 2 or sys.argv[1] != "update":
        print(__doc__)
        sys.exit(1)
    years = 3
    if "--years" in sys.argv:
        years = int(sys.argv[sys.argv.index("--years") + 1])
    if not HISTORY_PATH.exists():
        print("data/history.json이 없습니다. 먼저 collect.py를 실행하세요.")
        sys.exit(1)
    history = json.load(open(HISTORY_PATH, encoding="utf-8"))
    codes = sorted({r["code"] for r in history})
    update(codes, years, key)
