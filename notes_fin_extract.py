"""
순차입금 보정값: 사업보고서 원문의 기타금융자산·기타금융부채 주석 표에서 구성 항목을 읽는다 (API 호출 없음)
==============================================================================
입력: out/notes_fin.json.gz   (full_financials.py notes_fin 결과: 종목별 '기타금융자산·부채'가 들어간 주석 표 원본)
      data/full_accounts.json (재무상태표 계정·연결/별도 기준)
      out/xbrl_nd.json.gz     (full_financials.py xbrl_nd 결과: 같은 사업보고서 XBRL의 기말 현금·차입·재무활동부채 사실, 없으면 주석만 쓴다)
출력: data/notes_fin.json { 종목코드: {y, cash_excl, debt_add, debt_min} }  금액은 보고 통화 원 단위, 보정이 있는 종목만

재무상태표의 '기타(유동)금융자산'·'기타금융부채' 금액과 같은 값을 주석 표의 합계·소계 셀에서 찾고,
그 위 같은 열 구성 행들의 합이 금액과 1% 이내로 맞을 때만 쓴다(금융상품 범주표처럼 한 항목으로만 나오는 표는 제외).
  cash_excl  유동 기타금융자산 중 비현금성 항목(대여금·미수금·보증금·대손충당금·파생상품·리스채권 등) → 현금에서 뺀다
  debt_add   기타금융부채 중 차입성 항목(차입금·사채·리스부채·유동화채무·우선주부채) → 차입금에 더한다
  debt_min   XBRL '재무활동에서 생기는 부채' 기말 합계(배당·보증금·파생 등 비차입 구성 제외) → 차입금이 이보다 작으면 이 값으로 올린다
             (기타채무·기타부채 안에 섞인 차입금·리스부채를 잡는다. 합계 태그가 없으면 차입성 구성 항목 합)
  XBRL에 '현금성자산이 아닌 단기예금' 태그가 있으면 기타유동금융자산 중 그 예금만 현금으로 남긴다(cash_excl을 대신함)
FnGuide 순부채와 대조해 규칙을 정했다(주석 보정 56.9% → 71.8%, XBRL 추가 74.0% → 77.5%, ±2% 또는 1억원 기준).

사용법
  python notes_fin_extract.py   out/notes_fin.json.gz → data/notes_fin.json
"""
import gzip
import json
import re
from pathlib import Path

import build_ext_metrics as ext

BASE_DIR = Path(__file__).resolve().parent
RAW_PATH = BASE_DIR / "out" / "notes_fin.json.gz"
XBRL_PATH = BASE_DIR / "out" / "xbrl_nd.json.gz"
ACCOUNTS_PATH = BASE_DIR / "data" / "full_accounts.json"
OUT_PATH = BASE_DIR / "data" / "notes_fin.json"

OTHER_FA = re.compile(r"기타(?!.*비금융).*금융자산")
OTHER_FL = re.compile(r"기타.*금융부채")
NONCASH = re.compile(r"대여금|미수|보증금|대손|충당금|파생|리스채권|리스순투자|선급|매출채권|기타채권|미청구|차감|손상")
DEBTLIKE = re.compile(r"차입|사채|리스부채|유동화|금융리스|우선주부채")
TOTAL = re.compile(r"합\s*계|소\s*계|총\s*계|^계$")
SUBTOTAL = re.compile(r"\s*(유\s*동|비\s*유\s*동)\s*(합계|소계)?\s*")


def val(s):
    s = (s or "").strip().replace(",", "").replace(" ", "")
    if s in ("", "-", "—", "–"):
        return 0.0 if s else None
    neg = s.startswith("(") and s.endswith(")")
    s = s.strip("()")
    if s.startswith("-") and len(s) > 1:
        neg, s = True, s[1:]
    try:
        v = float(s)
    except ValueError:
        return None
    return -v if neg else v


def components(rows, i, j, sc):
    """합계 셀 위로 같은 열의 구성 행들(숫자 있는 행)을 모은다. 다른 합계·소계 행이나 숫자 없는 머리행에서 멈춘다."""
    out = []
    for k in range(i - 1, -1, -1):
        r = rows[k]
        if not r or TOTAL.search(r[0]) or len(r) <= j:
            break
        v = val(r[j])
        if v is None:
            if out:
                break
            continue
        out.append((r[0], v * sc))
    return out


def locate(entry, target, scope):
    """target(원)과 같은 합계·소계 셀을 찾아, 구성 행 합이 target과 1% 이내로 맞으면 구성 항목을 돌려준다. 연결/별도가 맞는 표를 먼저."""
    if not entry or not target or abs(target) < 1e8:
        return None
    for t in sorted(entry.get("tables", []), key=lambda t: scope not in t["sec"]):
        rows = t["rows"]
        for i, r in enumerate(rows):
            if not r or not (TOTAL.search(r[0]) or SUBTOTAL.fullmatch(r[0])):
                continue
            for j, c in enumerate(r[1:], 1):
                v = val(c)
                if not v:
                    continue
                for sc in (1e3, 1, 1e6):
                    if abs(v * sc - target) <= max(abs(target) * 0.003, sc):
                        comp = components(rows, i, j, sc)
                        if comp and abs(sum(x for _, x in comp) - target) <= abs(target) * 0.01:
                            return comp
    return None


def adjust(entry, account):
    bs = [r for r in account["rows"] if r[0] == "BS" and r[3] is not None]
    scope = "연결" if account.get("fs") == "CFS" else "별도"
    cash_excl = debt_add = 0.0
    for r in ext.current_asset_rows(bs):
        if OTHER_FA.search(r[2].replace(" ", "")) and ext.LIQUID_NAME.search(r[2]):
            comp = locate(entry, r[3], scope)
            if comp:
                cash_excl += sum(x for lb, x in comp if NONCASH.search(lb.replace(" ", "")))
    for r in bs:
        if OTHER_FL.search(r[2].replace(" ", "")) and not ext.DEBT_NAME.search(r[2]):
            comp = locate(entry, r[3], scope)
            if comp:
                debt_add += sum(x for lb, x in comp if DEBTLIKE.search(lb.replace(" ", "")))
    return cash_excl, debt_add


LAFA = "ifrs-full:LiabilitiesArisingFromFinancingActivities"
SCOPES = ("Consolidated", "Separate")
NONDEBT = re.compile(r"Dividend|Deposit|Repurchase|Derivative|Guarantee|Interest|Payable|Hedg|Withholding|Accrued|Advance|Treasury|Noncontrolling|Grant|Subscription|Other", re.I)
DEBTMEM = re.compile(r"Borrow|Bond|Debenture|Lease|Loan|Debt|Note|Commercial|Paper|Securit|Convertible|Exchangeable|Redeemable|Preferred|Factoring|Discount", re.I)
SHORT_DEPOSIT = "ifrs-full:ShorttermDepositsNotClassifiedAsCashEquivalents"
DEPOSIT_LINE = re.compile(r"단기금융상품|단기예금|정기예금|예치금|단기금융자산|상각후원가")


def xbrl_facts(facts, scope):
    """연결/별도 member만 붙은 값은 합계(tot), 그 외 member 하나가 더 붙은 값은 구성(comp)으로 나눈다."""
    tot, comp = {}, {}
    for nm, v, mem in facts:
        other = [m for m in mem if m not in SCOPES]
        sc = [m for m in mem if m in SCOPES]
        if sc and sc[0] != scope:
            continue
        if not other:
            if nm not in tot or sc:
                tot[nm] = v
        elif len(other) == 1:
            comp.setdefault(nm, {})[other[0]] = v
    return tot, comp


def xbrl_debt(tot, comp, liab):
    """재무활동부채 합계 - 비차입 구성(없으면 차입성 구성 합). 부호는 절댓값, 부채총계를 넘으면(단위 오류) 버린다."""
    parts = comp.get(LAFA, {})
    if LAFA in tot:
        v = tot[LAFA] - sum(x for m, x in parts.items() if NONDEBT.search(m) and not DEBTMEM.search(m))
    else:
        debt = [x for m, x in parts.items() if DEBTMEM.search(m) and not re.search(r"Dividend|Deposit|Derivative", m, re.I)]
        if not debt:
            return None
        v = sum(debt)
    v = abs(v)
    return None if liab and v > liab * 1.01 else v


def xbrl_adjust(entry, account, bs):
    """(debt_min, cash_excl 대체값 또는 None)"""
    scope = "Consolidated" if account.get("fs") == "CFS" else "Separate"
    tot, comp = xbrl_facts(entry.get("facts", []), scope)
    liab = next((r[3] for r in bs if r[1] == "ifrs-full_Liabilities" or r[2].replace(" ", "") == "부채총계"), None)
    debt_min = xbrl_debt(tot, comp, liab)
    cash_excl = None
    if SHORT_DEPOSIT in tot:
        cur = ext.current_asset_rows(bs)
        in_cash = lambda r: not ext.is_cash(r) and ext.LIQUID_NAME.search(r[2]) and not ext.LIQUID_EXCLUDE.search(r[2])
        other_fa = sum(r[3] for r in cur if in_cash(r) and OTHER_FA.search(r[2].replace(" ", "")))
        separate = sum(r[3] for r in cur if in_cash(r) and DEPOSIT_LINE.search(r[2].replace(" ", "")) and not OTHER_FA.search(r[2].replace(" ", "")))
        if other_fa > 0:
            cash_excl = other_fa - min(max(0.0, tot[SHORT_DEPOSIT] - separate), other_fa)
    return debt_min, cash_excl


def build():
    raw = json.load(gzip.open(RAW_PATH, "rt", encoding="utf-8"))
    xbrl = json.load(gzip.open(XBRL_PATH, "rt", encoding="utf-8")) if XBRL_PATH.exists() else {}
    accounts = json.load(open(ACCOUNTS_PATH, encoding="utf-8"))
    out = {}
    n_xd = n_xc = 0
    for code in sorted(set(raw) | set(xbrl)):
        acc = accounts.get(code) or {}
        if not acc.get("rows"):
            continue
        cash_excl, debt_add = adjust(raw[code], acc) if code in raw else (0.0, 0.0)
        rec = {}
        if code in xbrl:
            bs = [r for r in acc["rows"] if r[0] == "BS" and r[3] is not None]
            debt_min, x_excl = xbrl_adjust(xbrl[code], acc, bs)
            if debt_min:
                rec["debt_min"] = round(debt_min)
                n_xd += 1
            if x_excl is not None:
                cash_excl = x_excl
                n_xc += 1
        if cash_excl or debt_add or rec:
            out[code] = {"y": acc["y"], "cash_excl": round(cash_excl), "debt_add": round(debt_add), **rec}
    OUT_PATH.write_text(json.dumps(out, ensure_ascii=False, separators=(",", ":")), encoding="utf-8")
    print(f"[notes-fin] 주석 {len(raw)}개사·XBRL {len(xbrl)}개사 중 보정 {len(out)}개사 (현금 {sum(1 for v in out.values() if v['cash_excl'])}, "
          f"차입금 {sum(1 for v in out.values() if v['debt_add'])}, XBRL 재무활동부채 {n_xd}, XBRL 단기예금 기준 현금 {n_xc}) -> {OUT_PATH.name}")


if __name__ == "__main__":
    build()
