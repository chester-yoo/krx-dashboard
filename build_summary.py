"""
대시보드 로딩용 경량 요약 파일(data/summary.json) 생성
=====================================================
data/history.json(약 30MB)을 종목별 시계열로 압축해서 브라우저가 한 번에 가볍게 받도록 한다.
최신 일자에 존재하는 종목만 포함한다(상장폐지 종목 제외).

data/summary.json:
  { "asof": "YYYYMMDD", "dates": [...],
    "stocks": [ {c: 종목코드, n: 종목명, m: 시장, i: 업종, px: 최신 종가, sh: 최신 상장주식수,
                 cap: [일자별 시가총액(억원), 결측은 null], h: "일자별 거래정지 여부 0/1 문자열"} ] }

사용법
  python build_summary.py
"""
import json
from pathlib import Path

BASE_DIR = Path(__file__).resolve().parent
HISTORY_PATH = BASE_DIR / "data" / "history.json"
INDUSTRY_PATH = BASE_DIR / "data" / "industry.json"
SUMMARY_PATH = BASE_DIR / "data" / "summary.json"


def load_json(path, default):
    if not path.exists():
        return default
    with open(path, "r", encoding="utf-8") as f:
        return json.load(f)


def build():
    history = load_json(HISTORY_PATH, [])
    industry = load_json(INDUSTRY_PATH, {})
    if not history:
        raise SystemExit("data/history.json이 비어 있습니다.")

    dates = sorted({r["date"] for r in history})
    date_idx = {d: i for i, d in enumerate(dates)}
    by_code = {}
    for r in history:
        by_code.setdefault(r["code"], []).append(r)

    stocks = []
    for code, rows in by_code.items():
        rows.sort(key=lambda r: r["date"])
        latest = rows[-1]
        if latest["date"] != dates[-1]:
            continue
        cap = [None] * len(dates)
        halt = ["0"] * len(dates)
        for r in rows:
            i = date_idx[r["date"]]
            cap[i] = r["market_cap_eok"]
            halt[i] = "1" if r.get("halted") else "0"
        stocks.append({
            "c": code,
            "n": latest["name"],
            "m": latest["market"],
            "i": industry.get(code, ""),
            "px": latest.get("close"),
            "sh": latest.get("list_shares"),
            "cap": cap,
            "h": "".join(halt),
        })
    stocks.sort(key=lambda s: s["c"])

    summary = {"asof": dates[-1], "dates": dates, "stocks": stocks}
    with open(SUMMARY_PATH, "w", encoding="utf-8") as f:
        json.dump(summary, f, ensure_ascii=False, separators=(",", ":"))
    print(f"[summary] {len(stocks)}개 종목, {len(dates)}거래일 -> {SUMMARY_PATH.name} ({SUMMARY_PATH.stat().st_size/1e6:.1f}MB)")


if __name__ == "__main__":
    build()
