"""
외화 공시 상장사 원화 환산
==========================
중국(위안)·미국(달러)·일본(엔) 등 외국 기업과 일부 국내 기업(예: 두산밥캣)은 재무제표를 외화로 공시한다.
OpenDART 금액을 그대로 쓰면 원화 대비 1/10~1/1,400 수준으로 작아져 PBR·PER·EV/EBITDA가 왜곡되므로 원화로 환산한다.

data/currency.json   { 종목코드: "USD" | "CNY" | "JPY" ... }  원화가 아닌 종목만. 사업보고서 원문 재무제표 단위 표기에서 판정
                     (full_financials.py notes_raw 결과로 갱신)
data/fx_rates.json   { "2025": { "USD": {"avg": 1421.73, "end": 1444.2}, ... } }  1단위당 원화
                     출처: 유럽중앙은행(ECB) 기준환율(frankfurter.dev). avg = 연간 영업일 평균, end = 연말 마지막 영업일
환산 기준: 손익·현금흐름(매출·영업이익·순이익·감가상각비·영업CF·이자비용) = 연평균, 재무상태표(자산·부채·자본·차입금·현금) = 연말
           (FnGuide 환산값과 대조: 2025년 USD 1,422.22 / CNY 197.78 → 연평균과 0.1% 이내)

사용법
  python fx.py      currency.json에 있는 통화의 최근 3개 연도 환율을 받아 fx_rates.json 갱신
"""
import json
import statistics
from datetime import date
from pathlib import Path

import requests

BASE_DIR = Path(__file__).resolve().parent
CURRENCY_PATH = BASE_DIR / "data" / "currency.json"
FX_PATH = BASE_DIR / "data" / "fx_rates.json"
API = "https://api.frankfurter.dev/v1"
BS_FIELDS = {"assets", "liabilities", "equity", "capital", "cash", "debt", "nd"}


def _load(path):
    return json.load(open(path, encoding="utf-8")) if path.exists() else {}


CURRENCY = _load(CURRENCY_PATH)
RATES = _load(FX_PATH)


def factor(code, year, kind):
    """원화 환산 배수. 원화 종목이거나 환율이 없으면 1 (환율이 없으면 None을 돌려 값을 비우게 한다)."""
    cur = CURRENCY.get(code)
    if not cur or cur == "KRW":
        return 1
    r = RATES.get(str(year), {}).get(cur)
    return r[kind] if r else None


def to_krw(code, year, field, amount):
    if amount is None:
        return None
    f = factor(code, year, "end" if field in BS_FIELDS else "avg")
    return None if f is None else amount * f


def fetch_year(year, currencies):
    end = min(date(year, 12, 31), date.today())
    data = requests.get(f"{API}/{year}-01-01..{end.isoformat()}", params={"base": "KRW", "symbols": ",".join(currencies)}, timeout=30).json()
    days = sorted(data["rates"])
    out = {}
    for cur in currencies:
        vals = [1 / data["rates"][d][cur] for d in days if cur in data["rates"][d]]
        if vals:
            out[cur] = {"avg": round(statistics.mean(vals), 4), "end": round(vals[-1], 4)}
    return out


def update(years=3):
    currencies = sorted({c for c in CURRENCY.values() if c != "KRW"})
    if not currencies:
        return
    rates = _load(FX_PATH)
    this_year = date.today().year
    for y in range(this_year - years, this_year + 1):
        if str(y) in rates and y < this_year - 1 and set(currencies) <= set(rates[str(y)]):
            continue  # 지난 연도는 한 번 받으면 바뀌지 않는다
        rates[str(y)] = fetch_year(y, currencies)
    with open(FX_PATH, "w", encoding="utf-8") as f:
        json.dump(rates, f, ensure_ascii=False, indent=1)
    print(f"[fx] {', '.join(currencies)} · {min(rates)}~{max(rates)} -> {FX_PATH.name}")


if __name__ == "__main__":
    update()
