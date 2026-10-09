"""
사업보고서 원문 주석 표에서 감가상각비·이자비용·보고 통화를 뽑는다 (API 호출 없음)
==============================================================================
입력: out/notes_raw.json.gz  (full_financials.py notes_raw 결과: 종목별 '상각'·'이자비용'이 들어간 주석 표 원본)
      data/full_accounts.json (연결/별도 기준 판단용)
출력: data/notes_extract.json { 종목코드: {da, da_src, da_parts, ie, cur, unit_ok} }  금액은 보고 통화 원 단위(예: 원, USD)

감가상각비 후보 (연결 기준 종목은 연결재무제표 주석, 별도 기준 종목은 별도 주석만 사용)
  cf      현금흐름 주석(영업활동 조정)의 감가상각·상각 행 합계
  nature  비용의 성격별 분류 주석의 감가상각·상각 행 합계 (매출원가·판관비·합계 열이 있으면 합계 열)
  roll    유형자산·무형자산·사용권자산·투자부동산 변동표의 감가상각(상각)비 열 '합계' 행을 더한 값
  FnGuide 감가상각비(현금흐름표 기준)와 대조해 선택 규칙을 정했다(choose_da 참고).
이자비용: 금융비용(금융원가) 주석의 '이자비용' 행 → 없으면 현금흐름 주석의 '이자비용' 행

사용법
  python notes_extract.py        out/notes_raw.json.gz → data/notes_extract.json
"""
import gzip
import json
import re
from pathlib import Path

BASE_DIR = Path(__file__).resolve().parent
RAW_PATH = BASE_DIR / "out" / "notes_raw.json.gz"
ACCOUNTS_PATH = BASE_DIR / "data" / "full_accounts.json"
OUT_PATH = BASE_DIR / "data" / "notes_extract.json"

DA_ROW = re.compile(r"감가상각비|(무형|유형|사용권|생물|투자부동산)\S*상각|^상각비$|^무형자산상각$")
DA_ROW_EXCLUDE = re.compile(r"누계|대손|상각후원가|손상|환입|처분|이연법인세|차금|할인|할증")
DA_COMBINED = re.compile(r"^감가상각비(,|및|와)")
ROLL_COL = re.compile(r"감가상각|상각")
ROLL_COL_EXCLUDE = re.compile(r"누계")
TOTAL_ROW = re.compile(r"^(합계|계|총계|합\s*계|소계)$")
ASSET_KIND = (("사용권자산", "rou"), ("투자부동산", "inv"), ("무형자산", "intang"), ("유형자산", "ppe"), ("생물자산", "bio"))
CF_CTX = re.compile(r"영업활동|창출|영업으로부터|현금흐름표.{0,6}조정|조정.{0,10}현금")
NATURE_CTX = re.compile(r"성격별|비용의 ?분류|영업비용의 ?구성")
FINCOST_CTX = re.compile(r"금융비용|금융원가|금융손익|금융수익")
CURRENT = re.compile(r"당\s*기|당기말|^\s*제\s*\d+\s*\(당\)\s*기")
CURRENCY_CODE = {"원": "KRW", "USD": "USD", "US$": "USD", "달러": "USD", "위안": "CNY", "RMB": "CNY", "CNY": "CNY",
                 "엔": "JPY", "JPY": "JPY", "홍콩달러": "HKD", "HKD": "HKD", "싱가포르달러": "SGD", "SGD": "SGD", "유로": "EUR", "EUR": "EUR"}


def num(t):
    t = (t or "").replace(",", "").replace(" ", "")
    neg = (t.startswith("(") and t.endswith(")")) or (t.startswith("-") and len(t) > 1) or t.startswith("△")
    t = t.strip("()-△▲")
    return (-1 if neg else 1) * float(t) if re.fullmatch(r"\d+(\.\d+)?", t) else None


UNIT_TEXT = re.compile(r"단위\s*[:：]?\s*([^)\]\s,]*)")


def table_unit(table):
    """표 바로 앞 문구의 '(단위 : 천원)'을 가장 믿고, 없으면 표 첫 행, 마지막으로 표 안 아무 곳의 단위 표기."""
    m = UNIT_TEXT.findall(table["before"][-60:])
    if m and m[-1]:
        return m[-1]
    for r in table["rows"][:2]:
        for c in r:
            m = UNIT_TEXT.findall(c)
            if m and m[-1]:
                return m[-1]
    return table.get("unit") or ""


def unit_mult(text):
    t = text or ""
    if "백만" in t:
        return 1e6
    if "억" in t:
        return 1e8
    if "천" in t:
        return 1e3
    return 1.0


def split_table(rows):
    """(단위 행 제외) 머리글 행들과 숫자 행들로 나눈다. 숫자가 처음 나오는 행부터 본문."""
    rows = [r for r in rows if not (len(r) == 1 and "단위" in r[0])]
    for i, r in enumerate(rows):
        if any(num(c) is not None for c in r[1:]):
            return rows[:i], rows[i:]
    return rows, []


def value_col(header, row):
    """행 방향 표에서 당기(또는 합계) 값이 있는 열을 고른다. 머리글과 행 길이가 같을 때만 위치를 믿는다."""
    for h in reversed(header):
        if len(h) == len(row):
            for key in (CURRENT, re.compile(r"^(합\s*계|계)$")):
                for j, c in enumerate(h):
                    if j and key.search(c) and num(row[j]) is not None:
                        return num(row[j])
    vals = [num(c) for c in row[1:] if num(c) is not None]
    return vals[0] if vals else None


def row_da_total(table):
    header, body = split_table(table["rows"])
    picked = {}
    for r in body:
        label = re.sub(r"\s", "", r[0] if r else "")
        if not DA_ROW.search(label) or DA_ROW_EXCLUDE.search(label):
            continue
        v = value_col(header, r)
        if v is None:
            continue
        if DA_COMBINED.search(label):
            return abs(v)  # '감가상각비 및 무형자산상각비' 등 합쳐진 행은 그 값이 곧 합계
        picked.setdefault(label, abs(v))
    return sum(picked.values()) if picked else None


def roll_da(table):
    """변동표: 머리글에서 감가상각(상각) 열을 찾고, 당기 구간의 합계 행 값을 쓴다. 합계 행이 없으면 당기 구간 행을 더한다."""
    header, body = split_table(table["rows"])
    col = None
    for h in header:
        for j, c in enumerate(h):
            if j and ROLL_COL.search(c) and not ROLL_COL_EXCLUDE.search(c):
                col, width = j, len(h)
                break
        if col is not None:
            break
    if col is None:
        return roll_da_by_row(header, body)
    total, acc, seen_block = None, 0.0, False
    for r in body:
        label = re.sub(r"\s", "", r[0] if r else "")
        if re.search(r"전기", label) and seen_block:
            break
        if len(r) == 1:  # '<당기>' 같은 구간 표시
            continue
        idx = col if len(r) == width else col - (width - len(r))
        if not 0 < idx < len(r):
            continue
        v = num(r[idx])
        seen_block = True
        if TOTAL_ROW.search(label):
            total = v
            break
        if v is not None:
            acc += v
    val = total if total is not None else (acc if acc else None)
    return abs(val) if val is not None else None


def roll_da_by_row(header, body):
    """자산 종류가 열, 변동 항목이 행인 변동표: '감가상각비'·'상각' 행에서 합계 열 값(없으면 숫자 칸 합)을 쓴다."""
    total_col = None
    for h in header:
        for j, c in enumerate(h):
            if j and re.fullmatch(r"(합\s*계|계|총\s*계)", c.strip()):
                total_col, width = j, len(h)
    for r in body:
        label = re.sub(r"\s|\(.*?\)", "", r[0] if r else "")
        if re.search(r"전기", label):
            break
        if not re.fullmatch(r"(감가)?상각(비)?|감가상각비\S*|무형자산상각비?|상각비?", label):
            continue
        if total_col is not None and len(r) == width and num(r[total_col]) is not None:
            return abs(num(r[total_col]))
        vals = [num(c) for c in r[1:] if num(c) is not None]
        return abs(sum(vals)) if vals else None
    return None


def table_kind(table):
    before = table["before"]
    tail = before[-90:]
    if NATURE_CTX.search(tail):
        return "nature", None
    if FINCOST_CTX.search(tail):
        return "fincost", None
    if CF_CTX.search(tail) and "변동" not in tail[-40:]:
        return "cf", None
    pos, kind = -1, None
    for word, k in ASSET_KIND:
        p = tail.rfind(word)
        if p > pos:
            pos, kind = p, k
    if kind and re.search(r"변동|증감", tail[pos:]):
        return "roll", kind
    return None, None


def section_ok(table, fs):
    consolidated = "연결" in table["sec"]
    return consolidated if fs == "CFS" else not consolidated


def extract(entry, fs):
    out = {"cf": None, "nature": None, "roll_parts": {}, "ie": None, "ie_cf": None, "units": set()}
    for t in entry.get("tables", []):
        if not section_ok(t, fs):
            continue
        kind, sub = table_kind(t)
        mult = unit_mult(table_unit(t))
        if kind in ("cf", "nature"):
            v = row_da_total(t)
            if v is not None and out[kind] is None:
                out[kind] = v * mult
                out["units"].add(table_unit(t))
        elif kind == "roll":
            v = roll_da(t)
            if v is not None and sub not in out["roll_parts"]:
                out["roll_parts"][sub] = v * mult
        if kind in ("fincost", "cf"):
            header, body = split_table(t["rows"])
            for r in body:
                if re.sub(r"\s", "", r[0] if r else "") in ("이자비용", "이자비용(주1)"):
                    v = value_col(header, r)
                    key = "ie" if kind == "fincost" else "ie_cf"
                    if v is not None and out[key] is None:
                        out[key] = abs(v) * mult
                    break
    parts = out["roll_parts"]
    out["roll"] = sum(parts.values()) if parts else None
    return out


def currency_of(entry):
    counts = entry.get("cur") or {}
    best = max(counts.items(), key=lambda kv: kv[1])[0] if counts else "원"
    return CURRENCY_CODE.get(best, CURRENCY_CODE.get(best.upper(), "KRW"))


def choose_da(c):
    """FnGuide 대조로 정한 규칙: 현금흐름 주석·성격별 분류 중 큰 값 → 없으면 자산 변동표 합.
    (현금흐름 주석이 일부 행만 담거나 성격별 표가 판관비 몫만 담은 경우가 있어 둘 중 큰 값이 더 잘 맞았다)"""
    cands = [(c[k], k) for k in ("cf", "nature") if c.get(k)]
    if cands:
        return max(cands)
    if c.get("roll"):
        return c["roll"], "roll"
    return None, None


def build():
    raw = json.load(gzip.open(RAW_PATH, "rt", encoding="utf-8"))
    accounts = json.load(open(ACCOUNTS_PATH, encoding="utf-8"))
    out = {}
    for code, entry in raw.items():
        fs = (accounts.get(code) or {}).get("fs") or "CFS"
        c = extract(entry, fs)
        da, src = choose_da(c)
        cur = currency_of(entry)  # 참고용(주석 단위 표기 다수결). 실제 환산 통화는 full_financials.py currency 결과(data/currency.json)
        out[code] = {"da": da, "da_src": src, "cf": c["cf"], "nature": c["nature"], "roll": c["roll"], "roll_parts": c["roll_parts"],
                     "ie": c["ie"] or c["ie_cf"], "cur": cur}
    OUT_PATH.write_text(json.dumps(out, ensure_ascii=False, separators=(",", ":")), encoding="utf-8")
    n = len(out) or 1
    print(f"[notes] {len(out)}개사 · 감가상각 {sum(1 for v in out.values() if v['da'])/n:.0%} · 이자비용 {sum(1 for v in out.values() if v['ie'])/n:.0%} "
          f"-> {OUT_PATH.name}")


if __name__ == "__main__":
    build()
