"""
순차입금 보정값: 사업보고서 원문의 기타금융자산·기타금융부채 주석 표에서 구성 항목을 읽는다 (API 호출 없음)
==============================================================================
입력: out/notes_fin.json.gz   (full_financials.py notes_fin 결과: 종목별 '기타금융자산·부채'가 들어간 주석 표 원본)
      data/full_accounts.json (재무상태표 계정·연결/별도 기준)
출력: data/notes_fin.json { 종목코드: {y, cash_excl, debt_add} }  금액은 보고 통화 원 단위, 보정이 있는 종목만

재무상태표의 '기타(유동)금융자산'·'기타금융부채' 금액과 같은 값을 주석 표의 합계·소계 셀에서 찾고,
그 위 같은 열 구성 행들의 합이 금액과 1% 이내로 맞을 때만 쓴다(금융상품 범주표처럼 한 항목으로만 나오는 표는 제외).
  cash_excl  유동 기타금융자산 중 비현금성 항목(대여금·미수금·보증금·대손충당금·파생상품·리스채권 등) → 현금에서 뺀다
  debt_add   기타금융부채 중 차입성 항목(차입금·사채·리스부채·유동화채무·우선주부채) → 차입금에 더한다
FnGuide 순부채와 대조해 규칙을 정했다(일치율 56.9% → 71.8%, ±2% 또는 1억원 기준).

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
ACCOUNTS_PATH = BASE_DIR / "data" / "full_accounts.json"
OUT_PATH = BASE_DIR / "data" / "notes_fin.json"

OTHER_FA = re.compile(r"기타.*금융자산")
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


def build():
    raw = json.load(gzip.open(RAW_PATH, "rt", encoding="utf-8"))
    accounts = json.load(open(ACCOUNTS_PATH, encoding="utf-8"))
    out = {}
    for code, entry in raw.items():
        acc = accounts.get(code) or {}
        if not acc.get("rows"):
            continue
        cash_excl, debt_add = adjust(entry, acc)
        if cash_excl or debt_add:
            out[code] = {"y": acc["y"], "cash_excl": round(cash_excl), "debt_add": round(debt_add)}
    OUT_PATH.write_text(json.dumps(out, ensure_ascii=False, separators=(",", ":")), encoding="utf-8")
    print(f"[notes-fin] 주석 {len(raw)}개사 중 보정 {len(out)}개사 (현금 {sum(1 for v in out.values() if v['cash_excl'])}, "
          f"차입금 {sum(1 for v in out.values() if v['debt_add'])}) -> {OUT_PATH.name}")


if __name__ == "__main__":
    build()
