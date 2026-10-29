"""
주식등의대량보유상황보고서(일반) 원문 XML 파서 - 보유주식등에 관한 계약(주담대) 추출
=====================================================================================
DART 원문은 TABLE-GROUP의 ACLASS와 셀의 ACODE/AUNIT 속성으로 필드가 구조화되어 있으므로
표 위치·열 순서가 아니라 속성으로 읽는다. 원문 XML은 엄격한 XML이 아닌 경우가 있어
(이스케이프 안 된 &, 닫히지 않은 태그 등) XML 파서 대신 태그 단위 정규식으로 읽는다.

parse_document(xml_text) -> {
  report:    {corp_name, stock_code, voting_shares, reporter_name, reporter_id, reporter_job, reporter_tp,
              report_date, holding_cnt, holding_rt, reason, trd_rvq_sel, sum_stk_cnt, sum_trd_rt}
  holders:   [{name, id, role, cnt, rt}]                    보유주식등의 수(HLD_STK)
  contracts: [{seq, provider, rel, provider_id, provider_type, stk_kind, shares, lender, lender_type, lender_score,
               contract_type, included, high_risk, contract_date, start_date, maturity, period_raw, ratio, remark,
               loan, debtor, rate, maint_ratio, sec_etc, sec_join, active, dup, flags}]
  flags:     보고서 단위 플래그 목록
}

용어
  ledger(누적 변동표) 형식: 일부 보고서는 계약 표를 체결·해지 이력 장부처럼 쓰며 주식 수에 음수(-)를 쓴다.
  이 경우 해지 행이 같은 상대방·같은 주식 수의 이전 행을 상쇄하고, 남은 행만 현재 유효한 계약으로 본다.
"""
import html
import json
import re
from datetime import date, timedelta
from pathlib import Path

BASE_DIR = Path(__file__).resolve().parent
RULES_PATH = BASE_DIR / "pledge_rules.json"

CELL_RE = re.compile(r"<(TE|TU)\b([^>]*)>(.*?)</\1\s*>", re.S | re.I)
ATTR_RE = re.compile(r'(\w+)\s*=\s*"([^"]*)"')
TR_RE = re.compile(r"<TR\b[^>]*>(.*?)</TR\s*>", re.S | re.I)
DATE_RE = re.compile(r"(\d{4})\s*[.\-/년]\s*(\d{1,2})\s*[.\-/월]\s*(\d{1,2})\s*일?")
BIZ_NO_RE = re.compile(r"^\d{3}-\d{2}-\d{5}$")
BIRTH_RE = re.compile(r"^\d{6}$")
CORP_NAME_RE = re.compile(r"\(주\)|㈜|주식회사|유한회사|합자회사|\(유\)")

_rules_cache = None


def load_rules(path=RULES_PATH):
    global _rules_cache
    if path == RULES_PATH and _rules_cache is not None:
        return _rules_cache
    with open(path, "r", encoding="utf-8") as f:
        rules = json.load(f)
    if path == RULES_PATH:
        _rules_cache = rules
    return rules


# ---------------------------------------------------------------- 셀 값 정리

def clean_text(raw):
    """셀 안의 태그를 걷어내고 공백을 정리한다. '-'·빈 값은 None."""
    if raw is None:
        return None
    text = html.unescape(re.sub(r"<[^>]+>", " ", raw))
    text = re.sub(r"\s+", " ", text).strip()
    return None if text in ("", "-", "−", "–") else text


def to_int(text):
    if text is None:
        return None
    s = re.sub(r"[,\s주원]", "", text)
    if not re.fullmatch(r"[-−]?\d+", s):
        return None
    return int(s.replace("−", "-"))


def to_float(text):
    if text is None:
        return None
    s = re.sub(r"[,\s%]", "", text)
    if not re.fullmatch(r"[-−]?\d+(\.\d+)?", s):
        return None
    return float(s.replace("−", "-"))


def parse_ymd(text):
    """'20261022' 또는 '2026.10.22'·'2026년 10월 22일' 형식 하나를 date로. 실패하면 None."""
    if not text:
        return None
    if re.fullmatch(r"\d{8}", text):
        y, m, d = int(text[:4]), int(text[4:6]), int(text[6:])
    else:
        mm = DATE_RE.search(text)
        if not mm:
            return None
        y, m, d = (int(g) for g in mm.groups())
    try:
        return date(y, m, d)
    except ValueError:
        return None


def parse_period(text):
    """계약기간 문자열 -> (시작일, 만기일). 날짜가 둘 이상이면 첫 날짜가 시작, 끝 날짜가 만기.
    날짜가 하나뿐이면 만기로 본다('2027.01.22까지'). '공탁 해지시까지'처럼 날짜가 없으면 (None, None)."""
    if not text:
        return None, None
    dates = []
    for mm in DATE_RE.finditer(text):
        try:
            dates.append(date(*(int(g) for g in mm.groups())))
        except ValueError:
            continue
    if not dates:
        return None, None
    if len(dates) == 1:
        return None, dates[0]
    return dates[0], dates[-1]


YM_RE = re.compile(r"(\d{4})\s*[.\-/년]\s*(\d{1,2})\s*월?\s*$")
DURATION_RE = re.compile(r"(\d+)\s*(년|개월|달|일)")
AUTO_RENEW_RE = re.compile(r"자동\s*연장|갱신|연장\s*가능|단위로?\s*연장")


def add_months(d, n):
    y, m = divmod(d.month - 1 + n, 12)
    y += d.year
    m += 1
    last = [31, 29 if y % 4 == 0 and (y % 100 or y % 400 == 0) else 28, 31, 30, 31, 30, 31, 31, 30, 31, 30, 31][m - 1]
    return date(y, m, min(d.day, last))


def derive_maturity(text, base):
    """절대 날짜가 없는 계약기간 -> (만기일, 사유). '1년'·'6개월'·'91일'은 base(계약 시작일 또는 계약체결일)+기간,
    '~ 2027.07'처럼 연월만 있으면 그 달 말일. 자동연장·갱신 조건이면 만기를 정할 수 없으므로 (None, 'AUTO_RENEW')."""
    if not text:
        return None, None
    if AUTO_RENEW_RE.search(text):
        return None, "AUTO_RENEW"
    mm = YM_RE.search(text)
    if mm:
        y, m = int(mm.group(1)), int(mm.group(2))
        if 1 <= m <= 12:
            return add_months(date(y, m, 1), 1).replace(day=1) - timedelta(days=1), "MATURITY_DERIVED"
    parts = DURATION_RE.findall(text)
    if parts and base:
        months = days = 0
        for n, u in parts:
            n = int(n)
            if u == "년":
                months += 12 * n
            elif u in ("개월", "달"):
                months += n
            else:
                days += n
        return add_months(base, months) + timedelta(days=days), "MATURITY_DERIVED"
    return None, None


def iso(d):
    return d.isoformat() if d else None


# ---------------------------------------------------------------- 표 읽기

def row_cells(tr_html):
    """TR 하나 -> {ACODE 또는 AUNIT: (텍스트, AUNITVALUE)}. TE는 ACODE, TU는 AUNIT 속성이 키."""
    cells = {}
    for m in CELL_RE.finditer(tr_html):
        attrs = dict(ATTR_RE.findall(m.group(2)))
        key = attrs.get("ACODE") or attrs.get("AUNIT")
        if not key:
            continue
        cells[key] = (clean_text(m.group(3)), attrs.get("AUNITVALUE"))
    return cells


def table_group_rows(xml, aclass):
    """ACLASS가 일치하는 TABLE-GROUP(여러 개면 모두)의 행 목록. 셀이 없는 행(머리글·TD만 있는 행)은 뺀다."""
    rows = []
    pat = re.compile(r'<TABLE-GROUP\b[^>]*ACLASS="%s"[^>]*>(.*?)</TABLE-GROUP\s*>' % re.escape(aclass), re.S | re.I)
    for g in pat.finditer(xml):
        for tr in TR_RE.finditer(g.group(1)):
            cells = row_cells(tr.group(1))
            if cells:
                rows.append(cells)
    return rows


def first_value(xml, acode):
    """문서 어디든 처음 나오는 ACODE 셀의 텍스트."""
    m = re.search(r'<TE\b[^>]*ACODE="%s"[^>]*>(.*?)</TE\s*>' % re.escape(acode), xml, re.S | re.I)
    return clean_text(m.group(1)) if m else None


def first_unit(xml, aunit):
    """문서 어디든 처음 나오는 AUNIT 셀의 (텍스트, AUNITVALUE)."""
    m = re.search(r'<TU\b([^>]*AUNIT="%s"[^>]*)>(.*?)</TU\s*>' % re.escape(aunit), xml, re.S | re.I)
    if not m:
        return None, None
    attrs = dict(ATTR_RE.findall(m.group(1)))
    return clean_text(m.group(2)), attrs.get("AUNITVALUE")


def val(cells, key):
    return cells.get(key, (None, None))[0]


def unit(cells, key):
    v = cells.get(key, (None, None))[1]
    return None if v in (None, "", "-") else v


def is_blank_row(cells):
    return all(v[0] is None and (v[1] in (None, "", "-", "9")) for v in cells.values())


# ---------------------------------------------------------------- 분류

def classify_contract(kind, remark, rules=None, lender=None):
    """계약종류·비고 문자열로 분류. 해지 행은 항상 해지로 보고(누적 변동표 상쇄용), 그 외에는 계약 상대방이
    세무서·법원이면 문구와 관계없이 납세담보로 본다(lender_keywords). unless에 걸리는 복합 계약
    ('주주간 계약, 근질권설정 계약')은 그 규칙을 건너뛰어 담보 계약으로 분류되게 한다."""
    rules = rules or load_rules()
    text = " ".join(t for t in (kind, remark) if t)
    term = rules["contract_types"][0]
    if term["type"] == "TERMINATION" and any(k in text for k in term["keywords"]):
        return term
    for r in rules["contract_types"]:
        if lender and any(k in lender for k in r.get("lender_keywords", [])):
            return r
    for r in rules["contract_types"]:
        if any(k in text for k in r["keywords"]) and not any(k in text for k in r.get("unless", [])):
            return r
    return {"type": "OTHER", "label": "기타(검토)", "include": False}


def classify_provider(name, pid):
    """담보제공자(또는 보고자) 유형. 조합·SPV는 이름으로, 개인·법인은 생년월일/사업자번호 형식으로 판별."""
    name = name or ""
    pid = (pid or "").strip()
    if "조합" in name:
        return "PARTNERSHIP"
    if re.search(r"유한회사|제\s*\d+\s*차|SPC|LLC|L\.P\.|Acquisition|ACQUISITION", name):
        return "SPV"
    if BIRTH_RE.match(pid):
        return "INDIVIDUAL"
    if BIZ_NO_RE.match(pid) or CORP_NAME_RE.search(name):
        return "CORPORATE"
    return "UNKNOWN"


def classify_lender(name, rules=None):
    rules = rules or load_rules()
    if not name:
        return rules["lender_default"]["type"], rules["lender_default"]["score"]
    compact = name.replace(" ", "")
    for r in rules["lender_types"]:
        if any(k in name for k in r.get("keywords", [])) or any(re.search(p, compact) for p in r.get("patterns", [])):
            return r["type"], r["score"]
    return rules["lender_default"]["type"], rules["lender_default"]["score"]


def norm_name(name):
    """이름 비교용: 법인 표기·괄호 설명·공백 제거."""
    if not name:
        return ""
    s = re.sub(r"\(舊[^)]*\)|\(구[^)]*\)", "", name)
    s = re.sub(r"\(주\)|㈜|주식회사|\(유\)|\s", "", s)
    return s


# ---------------------------------------------------------------- 본문

def parse_report_info(xml):
    info = {
        "corp_name": first_value(xml, "CRP_NM"),
        "stock_code": first_value(xml, "CRP_CD"),
        "voting_shares": to_int(first_value(xml, "FLT_STK_CNT")),
        "reporter_name": first_value(xml, "IFR_NM"),
        "reporter_id": first_value(xml, "IFR_ID"),
        "reporter_job": first_value(xml, "IFR_JOB"),
        "reporter_tp": first_unit(xml, "CRP_TP")[1],
        "report_date": iso(parse_ymd(first_unit(xml, "THS_RPT_DT")[1])),
        "holding_cnt": to_int(first_value(xml, "SUM_TMT_CNT")),
        "holding_rt": to_float(first_value(xml, "SUM_TMT_RT")),
        "reason": first_value(xml, "SUM_CHN_RWN"),
        "trd_rvq_sel": first_unit(xml, "TRD_RVQ_SEL")[1],
    }
    info["reporter_type"] = classify_provider(info["reporter_name"], info["reporter_id"])
    m = re.search(r"<DOCUMENT-NAME[^>]*>(.*?)</DOCUMENT-NAME", xml, re.S)
    info["doc_name"] = clean_text(m.group(1)) if m else None
    return info


def parse_holders(xml):
    holders = []
    for c in table_group_rows(xml, "HLD_STK"):
        name = val(c, "SPC_NM")
        if not name:
            continue
        holders.append({
            "name": name,
            "id": val(c, "SPC_ID2"),
            "cnt": to_int(val(c, "HLD_TOT_CNT")),
            "rt": to_float(val(c, "HLD_TOT_RT")),
        })
    return holders


def parse_contract_rows(xml, rules):
    rows, sums = [], {"sum_stk_cnt": None, "sum_trd_rt": None}
    for c in table_group_rows(xml, "TRD_RVL"):
        if "SUM_STK_CNT" in c or "SUM_TRD_RT" in c:
            sums["sum_stk_cnt"] = to_int(val(c, "SUM_STK_CNT"))
            sums["sum_trd_rt"] = to_float(val(c, "SUM_TRD_RT"))
            continue
        if is_blank_row(c):
            continue
        kind, remark = val(c, "TRD_KND"), val(c, "TRD_RMK")
        lender = val(c, "TRD_RVL")
        ctype = classify_contract(kind, remark, rules, lender)
        provider, pid = val(c, "SPC_NM"), val(c, "SPC_ID2")
        start, maturity = parse_period(val(c, "TRD_PRD"))
        contract_date = parse_ymd(unit(c, "TRD_SIN")) or parse_ymd(val(c, "TRD_SIN"))
        mflag = None
        if maturity is None:
            maturity, mflag = derive_maturity(val(c, "TRD_PRD"), start or contract_date)
        rows.append({
            "seq": to_int(val(c, "SEQ_NO2")),
            "provider": provider,
            "rel": unit(c, "IFR_RLT2"),
            "provider_id": pid,
            "provider_type": classify_provider(provider, pid),
            "stk_kind": unit(c, "STK_KND"),
            "shares": to_int(val(c, "STK_CNT")),  # 원문 그대로(누적 변동표 형식은 음수 가능)
            "lender": lender,
            "contract_kind": kind,
            "contract_type": ctype["type"],
            "contract_label": ctype["label"],
            "included": bool(ctype.get("include")),
            "high_risk": bool(ctype.get("high_risk")),
            "contract_date": iso(contract_date),
            "start_date": iso(start),
            "maturity": iso(maturity),
            "period_raw": val(c, "TRD_PRD"),
            "ratio": to_float(val(c, "TRD_RT")),
            "remark": remark,
            "loan": None, "debtor": None, "rate": None, "maint_ratio": None, "sec_etc": None,
            "sec_join": None, "active": True, "dup": None, "flags": [mflag] if mflag else [],
        })
    return rows, sums


def parse_sec_rows(xml):
    out = []
    for c in table_group_rows(xml, "SEC_CONT"):
        if is_blank_row(c):
            continue
        out.append({
            "seq": to_int(val(c, "SEQ_NO2")),
            "shares": to_int(val(c, "SEC_CNT")),
            "loan": to_int(val(c, "SEC_MOY")),
            "debtor": val(c, "SEC_DEB"),
            "rate": to_float(val(c, "SEC_INT")),
            "maint_ratio": to_float(val(c, "SEC_KEP")),
            "etc": val(c, "SEC_ETC"),
        })
    return out


def apply_ledger(rows, rules):
    """누적 변동표 형식이면 해지 행으로 이전 행을 상쇄해 active 여부를 정한다. 반환: ledger 여부."""
    ledger = any((r["shares"] or 0) < 0 for r in rows)
    for r in rows:
        if r["contract_type"] == "TERMINATION":
            r["active"] = False
    if not ledger:
        return False
    for i, t in enumerate(rows):
        if t["contract_type"] != "TERMINATION" or t["shares"] is None:
            continue
        # 같은 상대방·같은 주식 수(절대값)의 가장 최근 이전 유효 행을 상쇄
        for r in reversed(rows[:i]):
            if (r["active"] and r["contract_type"] != "TERMINATION" and r["shares"] is not None
                    and abs(r["shares"]) == abs(t["shares"]) and norm_name(r["lender"]) == norm_name(t["lender"])):
                r["active"] = False
                r["flags"].append("TERMINATED")
                break
    return True


def join_sec(rows, secs, rules):
    """담보계약 추가기재(SEC_CONT)를 계약 행에 붙인다.
    1) SEQ_NO2가 같고 주식 수가 일치하면 그 행에 붙인다(정상 경로).
    2) 안 맞으면 아직 안 붙은 유효 담보 행 중 주식 수가 같은 행 하나에 붙인다.
    3) 그래도 없으면 같은 계약 상대방의 유효 행 묶음 합계가 일치하는지 보고, 대출금액을 주식 수 비율로 나눈다.
    4) 실패하면 보고서 플래그(SEC_UNMATCHED)로 남긴다."""
    enc = set(rules["encumbrance_types"])
    by_seq = {r["seq"]: r for r in rows if r["seq"] is not None}
    pending, unmatched = [], []

    def attach(r, s, mode, share=1.0):
        r["loan"] = round(s["loan"] * share) if s["loan"] is not None else None
        r["debtor"], r["rate"], r["maint_ratio"], r["sec_etc"] = s["debtor"], s["rate"], s["maint_ratio"], s["etc"]
        r["sec_join"] = mode
        if not r["lender"] and s["etc"]:
            r["lender"] = s["etc"]

    for s in secs:
        r = by_seq.get(s["seq"])
        if r and r["sec_join"] is None and (s["shares"] is None or r["shares"] is None or abs(r["shares"]) == s["shares"]):
            attach(r, s, "seq")
        else:
            pending.append(s)

    for s in pending:
        cands = [r for r in rows if r["active"] and r["sec_join"] is None and r["contract_type"] in enc
                 and r["shares"] is not None and abs(r["shares"]) == s["shares"]]
        if len(cands) == 1:
            attach(cands[0], s, "shares")
            cands[0]["flags"].append("SEC_JOIN_FALLBACK")
            continue
        groups = {}
        for r in rows:
            if r["active"] and r["sec_join"] is None and r["contract_type"] in enc and r["shares"]:
                groups.setdefault(norm_name(r["lender"]), []).append(r)
        hit = [g for g in groups.values() if s["shares"] and sum(abs(r["shares"]) for r in g) == s["shares"]]
        if len(hit) == 1:
            total = s["shares"]
            for r in hit[0]:
                attach(r, s, "group", abs(r["shares"]) / total)
                r["flags"].append("SEC_JOIN_FALLBACK")
            continue
        unmatched.append(s)
    return unmatched


def mark_duplicates(rows):
    """완전 중복은 뒤쪽 행을 제외(dup='exact'), 계약일만 하루 이내로 다른 반복 기재는 둘 다 검토 대상(dup='suspect')."""
    seen = {}
    for r in rows:
        if not r["active"]:
            continue
        key = (r["provider"], r["shares"], r["contract_date"], norm_name(r["lender"]), r["contract_kind"])
        if key in seen:
            r["dup"] = "exact"
            r["active"] = False
            r["flags"].append("DUPLICATE")
        else:
            seen[key] = r
    act = [r for r in rows if r["active"] and r["contract_date"]]
    for i, a in enumerate(act):
        for b in act[i + 1:]:
            if (a["provider"], a["shares"], norm_name(a["lender"]), a["contract_kind"]) != (b["provider"], b["shares"], norm_name(b["lender"]), b["contract_kind"]):
                continue
            gap = abs((date.fromisoformat(a["contract_date"]) - date.fromisoformat(b["contract_date"])).days)
            if gap <= 1:
                for r in (a, b):
                    r["dup"] = "suspect"
                    if "DUP_SUSPECT" not in r["flags"]:
                        r["flags"].append("DUP_SUSPECT")


def split_shared_loans(rows):
    """여러 담보제공자가 하나의 대출을 공동담보로 잡은 경우 각 행에 같은 대출금액이 반복 기재된다.
    채무자가 명시되어 있고 (대출기관, 채무자, 대출금액, 만기)가 같으며 담보제공자가 2명 이상이면 하나의 대출로 보고
    주식 수 비율로 나눈다. 채무자 미기재('-')는 각자 본인 대출이므로 금액이 같아도 별개 대출로 둔다."""
    groups = {}
    for r in rows:
        if r["active"] and r["included"] and r.get("dup") != "exact" and r["loan"] and r["shares"] and r["debtor"]:
            key = (norm_name(r["lender"]), norm_name(r["debtor"]), r["loan"], r["maturity"])
            groups.setdefault(key, []).append(r)
    for g in groups.values():
        if len(g) < 2 or len({norm_name(r["provider"]) for r in g}) < 2:
            continue
        loan, total = g[0]["loan"], sum(abs(r["shares"]) for r in g)
        for r in g:
            r["loan"] = round(loan * abs(r["shares"]) / total)
            r["flags"].append("SHARED_FACILITY")


def row_flags(r, rules):
    f = r["flags"]
    if r["high_risk"]:
        f.append("HIGH_RISK_CONTRACT")
    if r["provider_type"] == "PARTNERSHIP":
        f.append("PARTNERSHIP_PROVIDER")
    if r["included"]:
        if r["debtor"] and norm_name(r["debtor"]) != norm_name(r["provider"]):
            f.append("THIRD_PARTY_DEBT")
        if r["contract_type"] == "OTHER":
            f.append("REVIEW")
        if r["sec_join"] is None:
            f.append("NO_LOAN_DETAIL")
        if r["maturity"] is None and "AUTO_RENEW" not in f:
            f.append("NO_MATURITY")
        if r["stk_kind"] not in (None, "11"):
            f.append("NON_COMMON_STOCK")
    elif r["contract_type"] == "OTHER" and r["active"]:
        f.append("REVIEW")


def parse_document(xml, rules=None):
    rules = rules or load_rules()
    report = parse_report_info(xml)
    rows, sums = parse_contract_rows(xml, rules)
    report.update(sums)
    secs = parse_sec_rows(xml)
    flags = []

    ledger = apply_ledger(rows, rules)
    if ledger:
        flags.append("LEDGER_FORMAT")
    unmatched = join_sec(rows, secs, rules)
    if unmatched:
        flags.append("SEC_UNMATCHED")
    mark_duplicates(rows)
    split_shared_loans(rows)

    lender_rules = rules
    for r in rows:
        r["lender_type"], r["lender_score"] = classify_lender(r["lender"], lender_rules)
        row_flags(r, rules)

    # 합계 검증: 일반 형식은 전체 행 합, 누적 변동표 형식은 유효 담보성 행의 절대값 합
    enc = set(rules["encumbrance_types"])
    if ledger:
        computed = sum(abs(r["shares"]) for r in rows if r["active"] and r["contract_type"] in enc and r["shares"])
    else:
        computed = sum(r["shares"] for r in rows if r["shares"] is not None and r["dup"] != "exact")
    report["computed_stk_cnt"] = computed
    if rows and report["sum_stk_cnt"] is not None and computed != report["sum_stk_cnt"]:
        flags.append("SUM_MISMATCH")
    if any("DUP_SUSPECT" in r["flags"] for r in rows):
        flags.append("DUP_SUSPECT")
    if any("REVIEW" in r["flags"] for r in rows):
        flags.append("REVIEW_ROWS")
    if report["reporter_type"] == "PARTNERSHIP":
        flags.append("REPORTER_PARTNERSHIP")

    report["has_contracts"] = bool(rows)
    report["has_pledge"] = any(r["included"] and r["active"] for r in rows)
    return {"report": report, "holders": parse_holders(xml), "contracts": rows,
            "sec_unmatched": unmatched, "flags": flags}


if __name__ == "__main__":
    import sys
    for p in sys.argv[1:]:
        doc = parse_document(Path(p).read_text(encoding="utf-8"))
        print(json.dumps(doc, ensure_ascii=False, indent=1))
