"""
EV/EBITDA·순차입금·영업현금흐름·이자보상배율 계산용 확장 재무 지표 생성 (API 호출 없음)
=====================================================================================
입력: data/full_accounts.json(전체 재무제표), data/notes_extract.json(원문 주석 감가상각비·이자비용, notes_extract.py),
      data/xbrl_depr.json(XBRL 주석 감가상각 합계), data/industry.json(금융업 제외), data/currency.json·fx_rates.json(외화 환산, fx.py)
출력: data/fin_ext.json { 종목코드: {y, fs, cash, debt, nd, op, da, da_src, da_chk, da_alt, ocf, icr, icr_basis, cur} }  (금액 단위 억원, 원화)

산식(최근 사업연도, 연결 우선). FnGuide(감가상각비(현금흐름표)·이자발생부채·순부채·이자비용)와 대사해 맞춘 기준
  현금      = 현금및현금성자산 + 유동 금융자산(단기금융상품·당기손익-공정가치·상각후원가·기타유동금융자산 등, 채권·대여금 제외)
  차입부채  = 차입금(단기·장기·유동성장기부채) + 사채(CB·BW·EB 포함) + 리스부채(유동·비유동) + 유동화채무 + 상환전환우선주부채
  순차입금  = 차입부채 − 현금   (주석으로만 구분되는 기타금융자산·기타금융부채 구성은 반영하지 못함)
  EBITDA    = 영업이익 + 감가상각비(유형·사용권·투자부동산·생물자산 감가상각 + 무형자산상각)
              감가상각비 출처: 현금흐름표 본문 → 원문 주석(현금흐름 주석·성격별 분류 중 큰 값 → 자산 변동표 합) → XBRL 주석
              주석 값이 XBRL과 10% 넘게 다르면 da_chk="불일치"로 표시
  이자보상배율 = 영업이익 ÷ 이자비용 (손익계산서 → 금융비용 주석 → 현금흐름 조정 순, 모두 없으면 금융비용으로 대신하고 icr_basis="금융비용")
  EV/EBITDA는 시가총액이 매일 바뀌므로 화면에서 (시가총액 + 순차입금) ÷ EBITDA로 계산한다.
금융업(은행·보험·증권 등)은 EV 계열 지표가 의미 없어 제외한다.

사용법
  python build_ext_metrics.py
"""
import json
import re
from pathlib import Path

import fx

BASE_DIR = Path(__file__).resolve().parent
ACCOUNTS_PATH = BASE_DIR / "data" / "full_accounts.json"
XBRL_PATH = BASE_DIR / "data" / "xbrl_depr.json"
NOTES_PATH = BASE_DIR / "data" / "notes_extract.json"  # notes_extract.py 결과(원문 주석 감가상각비·이자비용)
FINANCIALS_PATH = BASE_DIR / "data" / "financials.json"
INDUSTRY_PATH = BASE_DIR / "data" / "industry.json"
OUT_PATH = BASE_DIR / "data" / "fin_ext.json"

FINANCIAL_INDUSTRY = re.compile(r"은행|보험|증권|창업투자|기타금융|카드|캐피탈|금융")
CASH_NAME = re.compile(r"^현금및현금성자산")
STFIN_NAME = re.compile(r"단기금융상품|단기금융자산")
# 유동자산 중 현금성 금융자산(FnGuide 순부채의 현금 쪽 기준에 맞춤). 매출채권·미수금·대여금·보증금 등 영업·채권성 자산은 제외
LIQUID_NAME = re.compile(r"금융상품|금융자산|투자자산|예치금|공정가치|상각후원가|단기매매|매도가능|만기보유|예금")
LIQUID_EXCLUDE = re.compile(r"매출채권|미수|대여금|보증금|계약자산|파생|리스채권|선급|재고")
DEBT_NAME = re.compile(r"차입금|사채|리스부채|차입부채|리스(비)?유동부채|장기부채|장기채무|차입채무|유동화채무|우선주부채")
DEBT_ID = re.compile(r"Borrowings|BondsIssued|LeaseLiabilities|LoansReceived")  # 이름이 '단기금융부채' 등으로만 된 차입 계정 보완
DEBT_EXCLUDE = re.compile(r"할인|할증|조정|발행비|상환|이자|미지급|충당|파생|총계")
DA_NAME = re.compile(r"감가상각|(무형|유형|사용권|생물|투자부동산)\S*상각")
DA_EXCLUDE = re.compile(r"대손|상각후원가|할인|할증|차금|손상|누계")
OP_NAME = re.compile(r"^영업이익|^영업손실|^영업이익\(손실\)")
OCF_ID = "ifrs-full_CashFlowsFromUsedInOperatingActivities"
IE_NAME = re.compile(r"^이자비용$")
FC_NAME = re.compile(r"^금융비용$|^금융원가$")

XBRL_COMPONENTS = ["ifrs-full:DepreciationPropertyPlantAndEquipment", "ifrs-full:DepreciationRightofuseAssets",
                   "ifrs-full:DepreciationInvestmentProperty", "ifrs-full:AmortisationIntangibleAssetsOtherThanGoodwill"]


def load(path, default):
    return json.load(open(path, encoding="utf-8")) if path.exists() else default


def eok(v):
    return None if v is None else round(v / 1e8, 1)


def first(rows, pred):
    for r in rows:
        if pred(r) and r[3] is not None:
            return r[3]
    return None


def sum_distinct(rows, pred):
    """같은 계정이 중복 표기된 경우를 막으려고 (계정ID, 계정명) 기준으로 한 번씩만 더한다."""
    seen, total, hit = set(), 0, False
    for r in rows:
        if not pred(r) or r[3] is None:
            continue
        key = (r[1], r[2])
        if key in seen:
            continue
        seen.add(key)
        total += r[3]
        hit = True
    return total if hit else None


def is_cash(r):
    return r[1] == "ifrs-full_CashAndCashEquivalents" or bool(CASH_NAME.search(r[2].replace(" ", "")))


def current_asset_rows(bs):
    """재무상태표 행 순서에서 '유동자산' 다음부터 '비유동자산' 전까지를 유동자산 항목으로 본다."""
    out, inside = [], False
    for r in bs:
        nm = r[2].replace(" ", "")
        if nm == "유동자산" or r[1] == "ifrs-full_CurrentAssets":
            inside = True
            continue
        if nm in ("비유동자산", "자산총계") or r[1] in ("ifrs-full_NoncurrentAssets", "ifrs-full_Assets"):
            if inside:
                break
            continue
        if inside:
            out.append(r)
    return out


def cash_like(bs):
    """현금 = 현금및현금성자산 + 유동 금융자산(단기금융상품·당기손익-공정가치·상각후원가·기타유동금융자산 등).
    유동자산 구간을 못 찾으면 단기금융상품만 더한다."""
    cash = first(bs, is_cash)
    cur = current_asset_rows(bs)
    liquid = sum_distinct(cur, lambda r: not is_cash(r) and LIQUID_NAME.search(r[2]) and not LIQUID_EXCLUDE.search(r[2])) if cur \
        else sum_distinct(bs, lambda r: STFIN_NAME.search(r[2]))
    if cash is None and liquid is None:
        return None
    return (cash or 0) + (liquid or 0)


def borrowings(bs):
    """차입부채 = 차입금·사채·리스부채·유동화채무·상환전환우선주부채. 같은 이름이 액면·순액 두 줄로 나오면(차이 2% 이내) 한 줄만 쓴다."""
    picked = [r for r in bs if r[3] is not None and (DEBT_NAME.search(r[2]) or DEBT_ID.search(r[1] or "")) and not DEBT_EXCLUDE.search(r[2])]
    total = sum_distinct(picked, lambda r: True) or 0
    by_name = {}
    for r in picked:
        by_name.setdefault(r[2].replace(" ", ""), []).append(r[3])
    for vals in by_name.values():
        if len(vals) == 2 and vals[0] != vals[1] and abs(vals[0] - vals[1]) <= abs(max(vals, key=abs)) * 0.02:
            total -= max(vals)
    return total


def body_da(rows, sources=("CF",)):
    """본문 감가상각비: 현금흐름표 조정 항목의 감가상각·상각 계정을 모두 더한다(유형·사용권·투자부동산·생물자산·무형).
    괄호 안 자산 구분(예: 감가상각비(유형자산)/(투자부동산))은 서로 다른 항목이므로 지우지 않는다.
    손익계산서 값은 판관비 몫만 있는 경우가 많아(매출원가 몫 누락) 기본으로 쓰지 않는다."""
    picked = {}
    for r in rows:
        if r[0] not in sources or r[3] is None:
            continue
        name = r[2]
        if not DA_NAME.search(name) or DA_EXCLUDE.search(name):
            continue
        picked.setdefault(re.sub(r"\s|에 ?대한|조정", "", name), abs(r[3]))
    return sum(picked.values()) if picked else None


def xbrl_da(facts, year):
    """XBRL 주석 합계: 연결 우선. 합계 항목 → 구성요소 합 → 감가상각+무형상각 순으로 고른다."""
    cur = [f for f in facts if f[2] == str(year)]
    for fs in ("CFS", "OFS", ""):
        sel = {}
        for el, v, _, ffs in cur:
            if ffs == fs:
                sel.setdefault(el, abs(v))
        if not sel:
            continue
        if "ifrs-full:DepreciationAndAmortisationExpense" in sel:
            return sel["ifrs-full:DepreciationAndAmortisationExpense"]
        if "ifrs-full:DepreciationPropertyPlantAndEquipment" in sel:
            return sum(sel.get(e, 0) for e in XBRL_COMPONENTS)
        if "ifrs-full:DepreciationExpense" in sel:
            return sel["ifrs-full:DepreciationExpense"] + sel.get("ifrs-full:AmortisationExpense", 0)
        adj = sel.get("ifrs-full:AdjustmentsForDepreciationExpense")
        if adj is not None:
            return adj + sel.get("ifrs-full:AdjustmentsForAmortisationExpense", 0)
    return None


def notes_pick(entry, assets):
    """원문 주석 값(notes_extract.py 결과, 보고 통화 원 단위): 현금흐름 주석·성격별 분류 중 큰 값 → 변동표 합.
    (현금흐름 주석이 일부 행만 담거나 성격별 표가 판관비 몫만 담은 경우가 있어 둘 중 큰 값이 FnGuide와 더 잘 맞았다)
    자산총계 대비 0.02%~50% 밖이면 단위를 잘못 읽은 것으로 보고 버린다."""
    if not entry:
        return None, None
    cands = [(v, "원문 주석") for v in (entry.get("cf"), entry.get("nature")) if v]
    if cands:
        val, src = max(cands)
    elif entry.get("roll"):
        val, src = entry["roll"], "원문 주석(변동표)"
    else:
        return None, None
    if assets and not 0.0002 <= val / assets <= 0.5:
        return None, None
    return val, src


def build():
    accounts = load(ACCOUNTS_PATH, {})
    xbrl = load(XBRL_PATH, {})
    industry = load(INDUSTRY_PATH, {})
    notes = load(NOTES_PATH, {})
    out = {}
    stats = {"total": 0, "da_body": 0, "da_notes": 0, "da_xbrl": 0, "da_mismatch": 0, "nd": 0, "ocf": 0, "icr": 0, "icr_ie": 0, "fx": 0}
    for code, v in accounts.items():
        if not v.get("y") or FINANCIAL_INDUSTRY.search(industry.get(code, "")):
            continue
        rows, year = v["rows"], v["y"]
        bs = [r for r in rows if r[0] == "BS"]
        pl = [r for r in rows if r[0] in ("IS", "CIS")]
        cf_rows = [r for r in rows if r[0] == "CF"]
        cash_total = cash_like(bs)
        debt = borrowings(bs)
        op = first(pl, lambda r: OP_NAME.search(r[2]))
        ocf = first(cf_rows, lambda r: r[1] == OCF_ID)
        assets = first(bs, lambda r: r[1] == "ifrs-full_Assets" or r[2].replace(" ", "") == "자산총계")  # 보고 통화 기준
        note = notes.get(code) or {}
        xv = xbrl_da(xbrl.get(code, {}).get("facts", []), year)
        if xv is not None and assets and not 0.0002 <= xv / assets <= 0.5:
            xv = None
        da, da_src, da_chk, da_alt = body_da(rows), "본문", None, None
        if da is None:
            da, da_src = notes_pick(note, assets)
            if da is not None and xv and abs(da / xv - 1) > 0.1:
                da_chk, da_alt = "불일치", xv
            if da is None and xv is not None:
                da, da_src = xv, "XBRL 주석"
        ie = first(pl, lambda r: IE_NAME.search(r[2])) or note.get("ie") or first(cf_rows, lambda r: re.match(r"이자비용", r[2].replace(" ", "")))
        fc = first(pl, lambda r: FC_NAME.search(r[2]))
        denom, basis = (abs(ie), "이자비용") if ie else (fc, "금융비용")
        icr = round(op / denom, 2) if op is not None and denom else None
        nd = debt - cash_total if cash_total is not None else None
        # 외화 공시 종목 원화 환산(재무상태표 항목은 연말, 손익·현금흐름은 연평균 환율). 비율인 이자보상배율은 그대로
        k = lambda field, val: eok(fx.to_krw(code, year, field, val))
        out[code] = {"y": year, "fs": v.get("fs"), "cash": k("cash", cash_total), "debt": k("debt", debt), "nd": k("nd", nd),
                     "op": k("op", op), "da": k("da", da), "da_src": da_src if da is not None else None, "da_chk": da_chk,
                     "da_alt": k("da", da_alt), "ocf": k("ocf", ocf), "icr": icr, "icr_basis": basis if icr is not None else None,
                     "cur": fx.CURRENCY.get(code)}
        stats["total"] += 1
        stats["da_body"] += da is not None and da_src == "본문"
        stats["da_notes"] += da is not None and da_src.startswith("원문")
        stats["da_xbrl"] += da is not None and da_src == "XBRL 주석"
        stats["da_mismatch"] += da_chk == "불일치"
        stats["nd"] += nd is not None
        stats["ocf"] += ocf is not None
        stats["icr"] += icr is not None
        stats["icr_ie"] += icr is not None and basis == "이자비용"
        stats["fx"] += code in fx.CURRENCY
    with open(OUT_PATH, "w", encoding="utf-8") as f:
        json.dump(out, f, ensure_ascii=False, separators=(",", ":"))
    t = stats["total"] or 1
    print(f"[ext] 비금융 {stats['total']}개사 · 감가상각 본문 {stats['da_body']} + 원문 주석 {stats['da_notes']} + XBRL {stats['da_xbrl']} "
          f"= {(stats['da_body']+stats['da_notes']+stats['da_xbrl'])/t*100:.0f}% (주석·XBRL 10% 넘게 차이 {stats['da_mismatch']}) · 순차입금 {stats['nd']/t*100:.0f}% "
          f"· 영업CF {stats['ocf']/t*100:.0f}% · 이자보상배율 {stats['icr']/t*100:.0f}% (이자비용 기준 {stats['icr_ie']}) · 외화 환산 {stats['fx']}개사 -> {OUT_PATH.name}")


if __name__ == "__main__":
    build()
