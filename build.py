"""
대시보드용 파생 데이터 생성
============================
원천(data/history/*.json, industry.json, financials.json, disclosures.json)을 읽어
브라우저가 바로 쓰는 가벼운 파일을 만든다.

  data/summary.json         종목별 최신 상태·판정·밸류에이션 + 일자별 시장 집계 (첫 화면용)
  data/series/{code}.json   종목별 시가총액 추이·재무·공시 (상세 클릭 시에만 로드)

판정 규칙 (단순화 모델 - 실제 지정·폐지 여부는 거래소 공시로 확인 필요)
  - 시가총액이 기준 미만인 날이 STREAK_LIMIT(30)거래일 연속이면 관리종목 지정 대상
  - 지정 후 WINDOW(90)거래일 안에 POST_STREAK_LIMIT(45)거래일 연속 미달이면 상장폐지 사유 발생
  - 90거래일 안에 위 요건에 걸리지 않으면 지정 상태를 풀고 연속미달 일수를 다시 센다
  - 수집 시작일(2026-07-01) 이전 이력은 없으므로 그 이전 미달 일수는 반영되지 않는다
  - 거래정지일도 미달 일수에 포함한다 (정지일 산입 방식은 규정 확인 필요)

기준 세트
  official : 2026-12-31까지 코스피 300억/코스닥 200억, 2027-01-01부터 500억/300억 (일자별 적용)
  y2027    : 상향 기준(500억/300억)을 전 기간에 미리 적용한 시뮬레이션

사용법
  python build.py
"""
import json
import shutil
from pathlib import Path

import store

BASE_DIR = Path(__file__).resolve().parent
DATA_DIR = BASE_DIR / "data"
SERIES_DIR = DATA_DIR / "series"
SUMMARY_PATH = DATA_DIR / "summary.json"

STREAK_LIMIT = 30
DANGER_DAYS = 20  # 지정 임박 표시 시점 (30일의 약 2/3)
WINDOW = 90
POST_STREAK_LIMIT = 45

THRESHOLD_CHANGE_DATE = "20270101"
THRESHOLDS_OLD = {"KOSPI": 300, "KOSDAQ": 200}
THRESHOLDS_NEW = {"KOSPI": 500, "KOSDAQ": 300}

RULESETS = {
    "official": lambda market, date: (THRESHOLDS_OLD if date < THRESHOLD_CHANGE_DATE else THRESHOLDS_NEW)[market],
    "y2027": lambda market, date: THRESHOLDS_NEW[market],
}

# status 순위: 0 기준충족, 1 미달주의, 2 지정임박, 3 관리종목 지정 대상, 4 상장폐지 사유 발생
STATUS_SAFE, STATUS_WATCH, STATUS_DANGER, STATUS_CRIT, STATUS_DELIST = range(5)


def load_json(path, default):
    if not path.exists():
        return default
    with open(path, "r", encoding="utf-8") as f:
        return json.load(f)


def write_json(path, data):
    path.parent.mkdir(parents=True, exist_ok=True)
    with open(path, "w", encoding="utf-8") as f:
        json.dump(data, f, ensure_ascii=False, separators=(",", ":"))


def run_rules(hist, threshold_fn):
    """종목 이력(날짜순)에 판정 규칙을 적용해 일자별 상태와 최종 상태를 돌려준다."""
    streak = 0
    designated_at = None  # 지정일 인덱스
    post_streak = 0
    delisted_at = None
    daily = []  # (below, status)
    for i, r in enumerate(hist):
        th = threshold_fn(r["market"], r["date"])
        below = r["market_cap_eok"] < th
        streak = streak + 1 if below else 0
        if designated_at is None and delisted_at is None:
            if streak >= STREAK_LIMIT:
                designated_at = i
                post_streak = 0
        elif delisted_at is None:
            post_streak = post_streak + 1 if below else 0
            elapsed = i - designated_at
            if post_streak >= POST_STREAK_LIMIT:
                delisted_at = i
            elif elapsed >= WINDOW:
                designated_at = None
                post_streak = 0
                streak = 0

        if delisted_at is not None:
            status = STATUS_DELIST
        elif designated_at is not None:
            status = STATUS_CRIT
        elif streak >= DANGER_DAYS:
            status = STATUS_DANGER
        elif streak >= 1:
            status = STATUS_WATCH
        else:
            status = STATUS_SAFE
        daily.append((below, status))

    last = hist[-1]
    th = threshold_fn(last["market"], last["date"])
    cap = last["market_cap_eok"]
    result = {
        "st": daily[-1][1],
        "k": streak,
        "th": th,
        "mg": round((cap / th - 1) * 100, 1) if th else None,  # 기준 대비 여유율(%)
        "b60": sum(1 for b, _ in daily[-60:] if b),  # 최근 60거래일 중 미달 일수
    }
    if designated_at is not None:
        result["dd"] = hist[designated_at]["date"]  # 지정일
        result["de"] = len(hist) - 1 - designated_at  # 지정 후 경과 거래일
        result["ps"] = post_streak  # 지정 후 연속 미달
    if delisted_at is not None:
        result["xd"] = hist[delisted_at]["date"]
    prev = daily[-2] if len(daily) >= 2 else (False, STATUS_SAFE)
    events = []
    if daily[-1][0] and not prev[0]:
        events.append("new_below")
    if not daily[-1][0] and prev[0]:
        events.append("recovered")
    if daily[-1][1] == STATUS_CRIT and prev[1] < STATUS_CRIT:
        events.append("designated")
    if daily[-1][1] == STATUS_DELIST and prev[1] < STATUS_DELIST:
        events.append("delist")
    if events:
        result["ev"] = events
    return result, daily


def pct_change(hist, n):
    if len(hist) <= n:
        return None
    base = hist[-1 - n]["market_cap_eok"]
    if not base:
        return None
    return round((hist[-1]["market_cap_eok"] / base - 1) * 100, 1)


def latest_financials(fin):
    """가장 최근 사업연도의 핵심 계정 (원 단위 그대로)."""
    if not fin:
        return None
    year = max(fin)
    d = fin[year]
    return {"y": year, "fs": d.get("fs_div"), **{k: d.get(k) for k in
            ("revenue", "operating_profit", "net_income", "assets", "liabilities", "equity")}}


def valuation(cap_eok, f):
    """PER/PBR/부채비율/영업이익률. 적자·자본잠식 등 의미 없는 값은 None."""
    if not f:
        return {}
    cap_won = cap_eok * 1e8
    ni, eq, li, rev, op = f["net_income"], f["equity"], f["liabilities"], f["revenue"], f["operating_profit"]
    out = {}
    if ni and ni > 0:
        out["per"] = round(cap_won / ni, 1)
    if eq and eq > 0:
        out["pbr"] = round(cap_won / eq, 2)
        if li is not None:
            out["dr"] = round(li / eq * 100, 1)
    if rev and op is not None:
        out["opm"] = round(op / rev * 100, 1)
    if ni is not None:
        out["ni"] = round(ni / 1e8)
    if eq is not None:
        out["eq"] = round(eq / 1e8)
    if rev is not None:
        out["rev"] = round(rev / 1e8)
    out["fy"] = f["y"]
    return out


def build():
    rows = store.load_all()
    if not rows:
        raise SystemExit("data/history/에 데이터가 없습니다.")
    industry = load_json(DATA_DIR / "industry.json", {})
    financials = load_json(DATA_DIR / "financials.json", {})
    disclosures = load_json(DATA_DIR / "disclosures.json", {})

    by_code = {}
    for r in rows:
        by_code.setdefault(r["code"], []).append(r)
    dates = sorted({r["date"] for r in rows})
    date_idx = {d: i for i, d in enumerate(dates)}
    latest_date = dates[-1]

    agg = {rs: {k: [0] * len(dates) for k in ("kospi_below", "kosdaq_below", "danger", "crit", "delist")}
           for rs in RULESETS}

    if SERIES_DIR.exists():
        shutil.rmtree(SERIES_DIR)
    SERIES_DIR.mkdir(parents=True)

    stocks = []
    for code, hist in by_code.items():
        hist.sort(key=lambda r: r["date"])
        last = hist[-1]
        rules = {}
        for rs, fn in RULESETS.items():
            res, daily = run_rules(hist, fn)
            rules[rs] = res
            for r, (below, status) in zip(hist, daily):
                i = date_idx[r["date"]]
                if below:
                    agg[rs]["kospi_below" if r["market"] == "KOSPI" else "kosdaq_below"][i] += 1
                if status == STATUS_DANGER:
                    agg[rs]["danger"][i] += 1
                elif status == STATUS_CRIT:
                    agg[rs]["crit"][i] += 1
                elif status == STATUS_DELIST:
                    agg[rs]["delist"][i] += 1

        fin = financials.get(code) or {}
        stock = {
            "c": code,
            "n": last["name"],
            "m": last["market"],
            "i": industry.get(code) or "",
            "cap": last["market_cap_eok"],
            "px": last["close"],
            "h": 1 if last.get("halted") else 0,
            "a": 1 if last["date"] == latest_date else 0,  # 0이면 최신일 데이터 없음(상장폐지·코드변경 추정)
            "ld": last["date"],
            "c5": pct_change(hist, 5),
            "c20": pct_change(hist, 20),
            "r": rules,
            "v": valuation(last["market_cap_eok"], latest_financials(fin)),
        }
        stocks.append(stock)

        write_json(SERIES_DIR / f"{code}.json", {
            "d": [r["date"] for r in hist],
            "cap": [r["market_cap_eok"] for r in hist],
            "px": [r["close"] for r in hist],
            "h": [i for i, r in enumerate(hist) if r.get("halted")],
            "fin": fin,
            "disc": (disclosures.get(code) or {}).get("reports", []),
        })

    summary = {
        "asof": latest_date,
        "days": len(dates),
        "rules": {
            "streak_limit": STREAK_LIMIT, "danger_days": DANGER_DAYS,
            "window": WINDOW, "post_streak_limit": POST_STREAK_LIMIT,
            "change_date": THRESHOLD_CHANGE_DATE,
            "old": THRESHOLDS_OLD, "new": THRESHOLDS_NEW,
        },
        "daily": {"dates": dates, **agg},
        "stocks": stocks,
    }
    write_json(SUMMARY_PATH, summary)
    print(f"[build] {latest_date} 기준 {len(stocks)}개 종목, {len(dates)}거래일 -> summary.json, series/")


if __name__ == "__main__":
    build()
