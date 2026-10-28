"""
상장사 대주주 주식담보대출(주담대) 수집·빌드
=============================================
OpenDART 「주식등의대량보유상황보고서(일반)」의 '보유주식등에 관한 계약'을 수집·파싱해서
주담대 스크리닝 화면용 data/pledge.json을 만든다.

data/pledge_docs.json  수집 장부. { collected_through, reports: { rcept_no: {목록 정보, status, report_date,
                       reporter_key, superseded_by, doc(현재 스냅샷 보고서만 파싱 결과 보관)} } }
data/pledge_extra.json 회사별 보조 데이터 캐시. { corp_code: {exec: 임원현황(대표이사 매칭용), audit: 감사의견} }
data/pledge.json       화면용. 회사별 계약 행·보고서·재무·주가 조인 결과와 기본 가정값 기준 지표
cache/pledge/          원문 XML 캐시(rcept_no.xml, 커밋하지 않음)

스냅샷 원칙: 최신 일반보고서에는 해지되지 않은 계약이 누적 기재되므로 (종목, 보고자)별로
가장 최근 일반보고서 1건만 현재 상태로 쓴다. [기재정정]은 같은 보고의무발생일의 원보고서를 대체한다.

사용법
  python pledge.py update [--months 36] [--max-docs N] [--konex]
                             마지막 수집일(없으면 최근 N개월) 이후 접수분 목록 조회 -> 원문 다운로드·파싱 -> build
  python pledge.py extra     스냅샷 회사의 임원현황·감사의견 갱신(30일 지난 것만)
  python pledge.py reparse   파서 변경 후 현재 스냅샷 보고서를 캐시(없으면 재다운로드)로 다시 파싱 -> build
  python pledge.py build     API 호출 없이 data/pledge.json만 다시 생성
"""
import io
import json
import os
import re
import sys
import time
import zipfile
from datetime import date, datetime, timedelta
from pathlib import Path

import requests

import financials
import pledge_metrics
import pledge_parse

BASE_DIR = Path(__file__).resolve().parent
DATA_DIR = BASE_DIR / "data"
DOCS_PATH = DATA_DIR / "pledge_docs.json"
EXTRA_PATH = DATA_DIR / "pledge_extra.json"
OUT_PATH = DATA_DIR / "pledge.json"
SUMMARY_PATH = DATA_DIR / "summary.json"
HISTORY_PATH = DATA_DIR / "history.json"
FINANCIALS_PATH = DATA_DIR / "financials.json"
EXT_PATH = DATA_DIR / "fin_ext.json"
STAGE_PATH = DATA_DIR / "designation_stage.json"
CACHE_DIR = BASE_DIR / "cache" / "pledge"

API_BASE = "https://opendart.fss.or.kr/api"
LIST_WINDOW_DAYS = 90      # corp_code 없이 list.json을 조회하면 검색기간이 3개월로 제한된다
LIST_OVERLAP_DAYS = 3      # 증분 수집 시 늦게 반영되는 접수분을 놓치지 않도록 겹쳐 조회
CALL_DELAY = 0.15          # 호출 간 지연(초). 다른 수집 스크립트와 같은 값
# OpenDART 개인 인증키 일일 호출 한도(공식 안내 기준 20,000건). 한도·정책은 바뀔 수 있으므로
# opendart.fss.or.kr 공지에서 확인 후 조정한다. 다른 수집 스크립트와 키를 공유하므로 이 스크립트는 일부만 쓴다.
DAILY_CALL_LIMIT = 20000
RUN_CALL_BUDGET = 6000
EXTRA_REFRESH_DAYS = 30
REPORT_NAME = "주식등의대량보유상황보고서(일반)"
MARKETS = {"Y": "KOSPI", "K": "KOSDAQ"}


class DartLimitError(RuntimeError):
    """호출 한도 초과·점검: 이번 실행을 멈추고 진행분만 저장한다."""


def log(msg):
    print(financials.redact(f"[pledge] {msg}"), flush=True)


def load_json(path, default):
    if not path.exists():
        return default
    with open(path, "r", encoding="utf-8") as f:
        return json.load(f)


def save_json(path, obj, compact=True):
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_suffix(path.suffix + ".tmp")
    with open(tmp, "w", encoding="utf-8") as f:
        if compact:
            json.dump(obj, f, ensure_ascii=False, separators=(",", ":"))
        else:
            json.dump(obj, f, ensure_ascii=False, indent=1)
    tmp.replace(path)


# ---------------------------------------------------------------- API

class Dart:
    def __init__(self, key, budget=RUN_CALL_BUDGET, session=None):
        self.key = key
        self.budget = min(budget, DAILY_CALL_LIMIT)
        self.calls = 0
        self.http = session or requests

    def _get(self, path, params, timeout):
        if self.calls >= self.budget:
            raise DartLimitError(f"이번 실행 호출 예산({self.budget}건) 소진")
        last = None
        for attempt in range(5):
            if attempt:
                time.sleep(2 ** attempt)  # 2, 4, 8, 16초 지수 백오프
            try:
                self.calls += 1
                resp = self.http.get(API_BASE + path, params=dict(params, crtfc_key=self.key), timeout=timeout)
                time.sleep(CALL_DELAY)
                if resp.status_code >= 500:
                    last = f"HTTP {resp.status_code}"
                    continue
                return resp
            except requests.RequestException as e:
                last = type(e).__name__
        raise RuntimeError(f"{path} 재시도 실패 ({last})")

    def json(self, path, params, timeout=30):
        data = self._get(path, params, timeout).json()
        status = data.get("status")
        if status == "013":  # 조회된 데이터 없음
            return []
        if status in ("020", "800", "900"):  # 사용한도 초과 / 시스템 점검 / 정의되지 않은 오류
            raise DartLimitError(f"DART status={status} {data.get('message')}")
        if status != "000":
            raise RuntimeError(f"DART status={status} {data.get('message')}")
        return data

    def list_reports(self, bgn, end, corp_cls=None):
        """대량보유상황보고(D001) 목록. 기간을 LIST_WINDOW_DAYS 단위로 나누고 페이지를 끝까지 읽는다."""
        out = []
        for b, e in split_windows(bgn, end):
            page = 1
            while True:
                params = {"bgn_de": b, "end_de": e, "pblntf_ty": "D", "pblntf_detail_ty": "D001",
                          "page_no": page, "page_count": 100, "sort": "date", "sort_mth": "asc"}
                if corp_cls:
                    params["corp_cls"] = corp_cls
                data = self.json("/list.json", params)
                if not data:
                    break
                out.extend(data.get("list", []))
                if page >= int(data.get("total_page") or 1):
                    break
                page += 1
        return out

    def document(self, rcept_no):
        resp = self._get("/document.xml", {"rcept_no": rcept_no}, timeout=120)
        if resp.content[:2] != b"PK":  # zip 대신 오류 응답(XML/JSON)
            m = re.search(rb"<status>(\d+)</status>.*?<message>(.*?)</message>", resp.content, re.S)
            status = m.group(1).decode() if m else ""
            msg = m.group(2).decode("utf-8", "replace") if m else resp.content[:80].decode("utf-8", "replace")
            if status in ("020", "800") or "점검" in msg:
                raise DartLimitError(f"원문 다운로드 중단 ({status} {msg.strip()})")
            raise RuntimeError(f"원문 zip 아님 ({status} {msg.strip()})")
        return unzip_document(resp.content, rcept_no)


def split_windows(bgn, end, days=LIST_WINDOW_DAYS):
    """YYYYMMDD 기간을 days일 이하 구간으로 쪼갠다."""
    b = datetime.strptime(bgn, "%Y%m%d").date()
    e = datetime.strptime(end, "%Y%m%d").date()
    out = []
    while b <= e:
        w = min(e, b + timedelta(days=days - 1))
        out.append((b.strftime("%Y%m%d"), w.strftime("%Y%m%d")))
        b = w + timedelta(days=1)
    return out


def decode(raw):
    for enc in ("utf-8", "cp949", "euc-kr"):
        try:
            return raw.decode(enc)
        except UnicodeDecodeError:
            continue
    return raw.decode("utf-8", "replace")


def unzip_document(content, rcept_no):
    """원문 zip -> 본문 XML 텍스트. rcept_no.xml이 있으면 그것, 없으면 가장 큰 XML."""
    z = zipfile.ZipFile(io.BytesIO(content))
    names = [n for n in z.namelist() if n.lower().endswith(".xml")]
    if not names:
        raise RuntimeError("zip 안에 XML 없음")
    main = next((n for n in names if Path(n).stem == rcept_no), None) or max(names, key=lambda n: z.getinfo(n).file_size)
    return decode(z.read(main))


def cached_document(dart, rcept_no):
    path = CACHE_DIR / f"{rcept_no}.xml"
    if path.exists():
        return path.read_text(encoding="utf-8")
    text = dart.document(rcept_no)
    CACHE_DIR.mkdir(parents=True, exist_ok=True)
    path.write_text(text, encoding="utf-8")
    return text


# ---------------------------------------------------------------- 목록·스냅샷

def is_target(item, include_konex=False):
    name = item.get("report_nm", "")
    if REPORT_NAME not in name.replace(" ", "") or "약식" in name:
        return False
    markets = {"Y", "K", "N"} if include_konex else {"Y", "K"}
    return item.get("corp_cls") in markets and bool(item.get("stock_code"))


def reporter_key(report):
    return pledge_parse.norm_name(report.get("reporter_name")) + "|" + (report.get("reporter_id") or "").strip()


def select_snapshot(reports):
    """(종목, 보고자)별 최신 보고서를 고르고 나머지는 superseded_by를 채운다. 반환: 현재 rcept_no 집합.
    정렬 키 = (보고의무발생일, 접수번호). 정정본은 원보고서와 보고의무발생일이 같고 접수번호가 커서 원보고서를 대체하고,
    더 뒤의 보고서가 있으면 그 보고서가 우선한다(오래된 보고서의 정정이 최신 상태를 덮지 않는다)."""
    groups = {}
    for no, r in reports.items():
        if r.get("status") != "parsed" or not r.get("reporter_key"):
            continue
        groups.setdefault((r["stock_code"], r["reporter_key"]), []).append(no)
    current = set()
    for nos in groups.values():
        nos.sort(key=lambda n: (reports[n].get("report_date") or reports[n]["rcept_dt"], n))
        head = nos[-1]
        current.add(head)
        reports[head]["superseded_by"] = None
        for n in nos[:-1]:
            reports[n]["superseded_by"] = head
    return current


def update(key, months=36, max_docs=None, include_konex=False, budget=RUN_CALL_BUDGET):
    store = load_json(DOCS_PATH, {"collected_through": None, "reports": {}})
    reports = store["reports"]
    dart = Dart(key, budget)
    today = date.today()
    if store.get("collected_through"):
        bgn = datetime.strptime(store["collected_through"], "%Y%m%d").date() - timedelta(days=LIST_OVERLAP_DAYS)
    else:
        bgn = today - timedelta(days=round(months * 30.44))
    end = today.strftime("%Y%m%d")

    try:
        items = dart.list_reports(bgn.strftime("%Y%m%d"), end)
        added = 0
        for it in items:
            if not is_target(it, include_konex) or it["rcept_no"] in reports:
                continue
            reports[it["rcept_no"]] = {
                "corp_code": it.get("corp_code"), "corp_name": it.get("corp_name"), "stock_code": it.get("stock_code"),
                "corp_cls": it.get("corp_cls"), "report_nm": it.get("report_nm"), "flr_nm": it.get("flr_nm"),
                "rcept_dt": it.get("rcept_dt"), "correction": "정정" in it.get("report_nm", ""), "status": "pending",
            }
            added += 1
        store["collected_through"] = end
        log(f"목록 {len(items)}건 조회, 신규 대상 {added}건 (기간 {bgn:%Y%m%d}~{end})")
    except DartLimitError as e:
        log(f"목록 조회 중단: {e}")

    pending = sorted((n for n, r in reports.items() if r["status"] in ("pending", "failed") and r.get("tries", 0) < 3),
                     key=lambda n: n, reverse=True)  # 최신 접수분부터
    if max_docs:
        pending = pending[:max_docs]
    done = skipped = 0
    idx = latest_parsed_index(reports)
    for no in pending:
        r = reports[no]
        if superseded_by_parsed(idx, r):
            r["status"] = "skipped"  # 같은 보고자의 더 최근 보고서가 이미 있음 -> 원문을 받지 않는다
            skipped += 1
            continue
        try:
            parse_into(r, cached_document(dart, no))
            done += 1
            if r["report_date"]:
                k = filer_key(r)
                idx[k] = max(idx.get(k, ""), r["report_date"])
        except DartLimitError as e:
            log(f"원문 수집 중단: {e}")
            break
        except Exception as e:
            r["status"], r["error"] = "failed", financials.redact(e)[:200]
            r["tries"] = r.get("tries", 0) + 1
            log(f"{no} {r.get('corp_name')} 파싱 실패: {r['error']}")
        if done and done % 50 == 0:
            finalize_store(store)
            save_json(DOCS_PATH, store)
            log(f"진행: {done}/{len(pending)}건 (API {dart.calls}회)")
    finalize_store(store)
    save_json(DOCS_PATH, store)
    log(f"원문 {done}건 파싱, {skipped}건 생략(최신 보고서 존재), API {dart.calls}회 호출")


def filer_key(r):
    return (r.get("stock_code"), pledge_parse.norm_name(r.get("flr_nm")))


def latest_parsed_index(reports):
    """(종목, 제출인)별 파싱된 보고서의 가장 늦은 보고의무발생일."""
    idx = {}
    for r in reports.values():
        if r.get("status") == "parsed" and r.get("report_date"):
            k = filer_key(r)
            idx[k] = max(idx.get(k, ""), r["report_date"])
    return idx


def superseded_by_parsed(idx, r):
    """같은 종목·같은 제출인(flr_nm)의 파싱된 보고서 중 보고의무발생일이 이 보고서 접수일보다 뒤인 것이 있으면
    이 보고서는 스냅샷이 될 수 없다(보고의무발생일 <= 접수일). 정정본도 원보고서 날짜를 따르므로 같은 규칙이 성립한다."""
    return idx.get(filer_key(r), "") > r["rcept_dt"]


def parse_into(r, xml):
    doc = pledge_parse.parse_document(xml)
    rep = doc["report"]
    r.update({
        "status": "parsed", "error": None, "parsed_at": date.today().isoformat(),
        "report_date": (rep["report_date"] or "").replace("-", "") or None,
        "reporter_key": reporter_key(rep), "has_contracts": rep["has_contracts"], "has_pledge": rep["has_pledge"],
        "doc": doc,
    })


def finalize_store(store):
    """스냅샷을 다시 고르고, 대체된 보고서는 파싱 결과를 지워 파일 크기를 줄인다."""
    current = select_snapshot(store["reports"])
    for no, r in store["reports"].items():
        if no not in current and r.get("doc") is not None and r.get("superseded_by"):
            r["doc"] = None
    return current


def reparse(key):
    store = load_json(DOCS_PATH, {"collected_through": None, "reports": {}})
    dart = Dart(key) if key else None
    current = select_snapshot(store["reports"])
    n = 0
    for no in sorted(current):
        path = CACHE_DIR / f"{no}.xml"
        if not path.exists() and not dart:
            continue
        try:
            parse_into(store["reports"][no], cached_document(dart, no))
            n += 1
        except DartLimitError as e:
            log(f"재파싱 중단: {e}")
            break
    finalize_store(store)
    save_json(DOCS_PATH, store)
    log(f"재파싱 {n}건")


# ---------------------------------------------------------------- 보조 데이터(대표이사·감사의견)

def birth_yymm(birth_ym):
    """'1966년 03월' / '196603' -> '6603'."""
    if not birth_ym:
        return None
    digits = re.sub(r"\D", "", birth_ym)
    return digits[2:6] if len(digits) >= 6 else None


def fetch_exec(dart, corp_code, today):
    """최신 정기보고서 임원현황에서 직위에 '대표'가 들어간 임원. 최근 보고서부터 차례로 시도."""
    y = today.year
    for year, code in ((y, "11014"), (y, "11012"), (y, "11013"), (y - 1, "11011"), (y - 1, "11014")):
        data = dart.json("/exctvSttus.json", {"corp_code": corp_code, "bsns_year": str(year), "reprt_code": code})
        if data and data.get("list"):
            ceos = [{"nm": (x.get("nm") or "").strip(), "birth": birth_yymm(x.get("birth_ym")), "ofcps": x.get("ofcps"),
                     "rgist": x.get("rgist_exctv_at")}
                    for x in data["list"] if "대표" in (x.get("ofcps") or "")]
            return {"src": f"{year}-{code}", "ceos": ceos}
    return {"src": None, "ceos": []}


def fetch_audit(dart, corp_code, today):
    for year in (today.year - 1, today.year - 2):
        data = dart.json("/accnutAdtorNmNdAdtOpinion.json", {"corp_code": corp_code, "bsns_year": str(year), "reprt_code": "11011"})
        rows = data.get("list", []) if data else []
        cur = [x for x in rows if "당기" in (x.get("bsns_year") or "")] or rows
        for x in cur:
            op = (x.get("adt_opinion") or "").strip()
            if op and op != "-":
                return {"year": year, "opinion": op, "auditor": (x.get("adtor") or "").strip()}
    return {"year": None, "opinion": None, "auditor": None}


def update_extra(key, budget=RUN_CALL_BUDGET):
    store = load_json(DOCS_PATH, {"reports": {}})
    extra = load_json(EXTRA_PATH, {})
    dart = Dart(key, budget)
    today = date.today()
    current = select_snapshot(store["reports"])
    corps = sorted({store["reports"][n]["corp_code"] for n in current if store["reports"][n].get("has_pledge")})
    n = 0
    try:
        for cc in corps:
            e = extra.setdefault(cc, {})
            for field, fn in (("exec", fetch_exec), ("audit", fetch_audit)):
                checked = (e.get(field) or {}).get("checked")
                if checked and (today - date.fromisoformat(checked)).days < EXTRA_REFRESH_DAYS:
                    continue
                try:
                    e[field] = dict(fn(dart, cc, today), checked=today.isoformat())
                    n += 1
                except DartLimitError:
                    raise
                except Exception as ex:
                    log(f"{cc} {field} 조회 실패: {ex}")
    except DartLimitError as ex:
        log(f"보조 데이터 조회 중단: {ex}")
    save_json(EXTRA_PATH, extra)
    log(f"보조 데이터 {n}건 갱신 (대상 {len(corps)}개사, API {dart.calls}회)")


def match_ceo(rows, reports, exec_info):
    """담보제공자별 대표이사 매칭. 성명 일치 AND 생년월(SPC_ID2 앞 4자리 YYMM) 일치."""
    ceos = (exec_info or {}).get("ceos") or []
    have_exec = exec_info is not None and exec_info.get("src") is not None

    def is_ceo(name, pid):
        if not have_exec or not name or not pid or not re.fullmatch(r"\d{6}", pid):
            return None  # 판단 불가
        same = [c for c in ceos if c["nm"] == name]
        if not same:
            return False
        if any(c["birth"] is None for c in same) or len(same) > 1 and len({c["birth"] for c in same}) == 1:
            return None  # 생년월 누락·동명이인 구분 불가
        return any(c["birth"] == pid[:4] for c in same)

    reporter_ceo = any(is_ceo(r["reporter_name"], r["reporter_id"]) for r in reports)
    out = {}
    for row in rows:
        p = row["provider"]
        if p in out:
            continue
        if row["provider_type"] != "INDIVIDUAL":
            out[p] = "CORPORATE"
            continue
        v = is_ceo(p, row["provider_id"])
        if v:
            out[p] = "CEO"
        elif v is None:
            out[p] = "UNMATCHED"
        elif reporter_ceo and row["rel"] == "S":
            out[p] = "CEO_RELATED"
        else:
            out[p] = "NON_CEO_INDIVIDUAL"
    return out


CEO_RANK = ["CEO", "CEO_RELATED", "NON_CEO_INDIVIDUAL", "CORPORATE", "UNMATCHED"]


# ---------------------------------------------------------------- 빌드

def avg_trading_value(history_rows, n=20):
    vals = [r["trd_val_eok"] for r in history_rows[-n:] if r.get("trd_val_eok") is not None]
    return round(sum(vals) / len(vals), 2) if vals else None


def fin_summary(fin, ext):
    """재무 요약(억원). 최근 사업연도 기준, 영업이익 3개년 흑자 여부 포함."""
    if not fin:
        return None
    years = sorted(fin, reverse=True)[:3]
    y0 = fin[years[0]]
    eok = lambda v: None if v is None else round(v / 1e8, 1)
    ops = [fin[y].get("operating_profit") for y in years]
    out = {
        "y": int(years[0]), "rev": eok(y0.get("revenue")), "op": eok(y0.get("operating_profit")),
        "ni": eok(y0.get("net_income")), "eq": eok(y0.get("equity")),
        "op3": all(o is not None and o > 0 for o in ops) if len(ops) == 3 else None,
        "debt_ratio": round(y0["liabilities"] / y0["equity"] * 100, 1) if y0.get("liabilities") is not None and y0.get("equity") and y0["equity"] > 0 else None,
    }
    if ext:
        ebitda = (ext.get("op") or 0) + (ext.get("da") or 0) if ext.get("op") is not None and ext.get("da") is not None else None
        out.update({
            "nd": ext.get("nd"), "ocf": ext.get("ocf"), "ebitda": round(ebitda, 1) if ebitda is not None else None,
            "nd_ebitda": round(ext["nd"] / ebitda, 2) if ebitda and ebitda > 0 and ext.get("nd") is not None else None,
            "ebitda_margin": round(ebitda / out["rev"] * 100, 1) if ebitda is not None and out["rev"] else None,
        })
    return out


def compact_row(c, rcept_no):
    return {
        "rc": rcept_no, "seq": c["seq"], "p": c["provider"], "pid": c["provider_id"], "rel": c["rel"], "pt": c["provider_type"],
        "sh": abs(c["shares"] or 0), "ln": c["lender"], "lt": c["lender_type"], "ls": c["lender_score"],
        "ct": c["contract_type"], "ck": c["contract_kind"], "cd": c["contract_date"], "sd": c["start_date"], "mt": c["maturity"],
        "pr": c["period_raw"], "l": c["loan"], "r": c["rate"], "k": c["maint_ratio"], "d": c["debtor"], "etc": c["sec_etc"],
        "rmk": c["remark"], "sk": c["stk_kind"], "dup": c["dup"], "f": c["flags"],
    }


def build(today=None):
    today = today or date.today()
    rules = pledge_parse.load_rules()
    store = load_json(DOCS_PATH, {"collected_through": None, "reports": {}})
    reports = store["reports"]
    current = select_snapshot(reports)
    summary = load_json(SUMMARY_PATH, {"asof": None, "stocks": []})
    stocks = {s["c"]: s for s in summary["stocks"]}
    fins = load_json(FINANCIALS_PATH, {})
    exts = load_json(EXT_PATH, {})
    stages = load_json(STAGE_PATH, {})
    extra = load_json(EXTRA_PATH, {})
    hist_by_code = {}
    if HISTORY_PATH.exists():
        for r in load_json(HISTORY_PATH, []):
            hist_by_code.setdefault(r["code"], []).append(r)
    asof = datetime.strptime(summary["asof"], "%Y%m%d").date() if summary.get("asof") else today

    by_code = {}
    for no in current:
        r = reports[no]
        if r.get("has_pledge") and r.get("doc"):
            by_code.setdefault(r["stock_code"], []).append(no)

    companies, review = [], []
    for code, nos in sorted(by_code.items()):
        docs = [reports[n]["doc"] for n in nos]
        s = stocks.get(code)
        price = s["px"] if s else None
        m, facs = pledge_metrics.company_metrics(docs, price, asof, rules)
        rows, excluded = [], {}
        for n in nos:
            for c in reports[n]["doc"]["contracts"]:
                if c["included"] and c["active"] and c["dup"] != "exact":
                    rows.append(compact_row(c, n))
                elif c["active"] and c["contract_type"] in rules["encumbrance_types"]:
                    e = excluded.setdefault(c["contract_type"], {"label": c["contract_label"], "n": 0, "sh": 0})
                    e["n"] += 1
                    e["sh"] += abs(c["shares"] or 0)
        rep_list = [dict(rcept_no=n, report_nm=reports[n]["report_nm"], rcept_dt=reports[n]["rcept_dt"],
                         **{k: reports[n]["doc"]["report"][k] for k in (
                             "report_date", "reporter_name", "reporter_id", "reporter_type", "reporter_job", "holding_cnt",
                             "holding_rt", "voting_shares", "sum_trd_rt", "sum_stk_cnt")},
                         flags=reports[n]["doc"]["flags"], holders=reports[n]["doc"]["holders"]) for n in sorted(nos)]
        corp_code = reports[nos[0]]["corp_code"]
        ex = extra.get(corp_code, {})
        ceo = match_ceo([c for n in nos for c in reports[n]["doc"]["contracts"] if c["included"] and c["active"]],
                        [reports[n]["doc"]["report"] for n in nos], ex.get("exec"))
        ceo_best = min(ceo.values(), key=CEO_RANK.index) if ceo else None
        stage = (stages.get(code) or {}).get("stage")
        hist = sorted(hist_by_code.get(code, []), key=lambda r: r["date"])
        name = (s or {}).get("n") or reports[nos[0]]["corp_name"]
        companies.append({
            "code": code, "corp_code": corp_code, "name": name,
            "market": (s or {}).get("m") or MARKETS.get(reports[nos[0]]["corp_cls"]),
            "ind": (s or {}).get("i"), "cap": (s or {}).get("cap", [None])[-1] if s else None, "px": price,
            "halted": bool(s and s.get("h", "0")[-1] == "1"),
            "tv": avg_trading_value(hist), "fin": fin_summary(fins.get(code), exts.get(code)),
            "audit": (ex.get("audit") or {}).get("opinion"), "stage": stage["label"] if stage else None,
            "ceo": ceo, "ceo_best": ceo_best, "reports": rep_list, "rows": rows, "excluded": excluded,
            "m": {k: m[k] for k in ("total_loan", "pledged_shares", "pledge_ratio", "pledge_to_holding", "wavg_rate",
                                    "nearest_maturity", "days_to_maturity", "min_margin", "max_forced_price", "ltv",
                                    "score", "flags", "report_age_days", "latest_report_date")},
        })
        reasons = sorted({f for f in m["flags"] if f in ("DUP_SUSPECT", "REVIEW_ROWS", "SUM_MISMATCH", "SEC_UNMATCHED",
                                                         "RATIO_MISMATCH", "LEDGER_FORMAT")}
                         | ({"CEO_UNMATCHED"} if "UNMATCHED" in ceo.values() and ex.get("exec") else set()))
        if reasons:
            review.append({"code": code, "name": name, "rcept_nos": sorted(nos), "reasons": reasons})

    for no, r in reports.items():
        if r.get("status") == "failed":
            review.append({"code": r.get("stock_code"), "name": r.get("corp_name"), "rcept_nos": [no],
                           "reasons": ["PARSE_FAILED"], "error": r.get("error")})

    statuses = {}
    for r in reports.values():
        statuses[r["status"]] = statuses.get(r["status"], 0) + 1
    out = {
        "asof": summary.get("asof"), "built_at": datetime.now().isoformat(timespec="seconds"),
        "collected_through": store.get("collected_through"),
        "assumptions": rules["assumptions"], "stress_score": rules["stress_score"],
        "lender_scores": {x["type"]: x["score"] for x in rules["lender_types"]} | {rules["lender_default"]["type"]: rules["lender_default"]["score"]},
        "status": {"reports": len(reports), "by_status": statuses, "current": len(current),
                   "current_with_contracts": sum(1 for n in current if reports[n].get("has_contracts")),
                   "companies": len(companies)},
        "companies": companies, "review": review,
    }
    save_json(OUT_PATH, out)
    log(f"build: 주담대 {len(companies)}개사, 검토 큐 {len(review)}건 -> {OUT_PATH.name}")
    return out


def main(argv):
    if len(argv) < 2 or argv[1] not in ("update", "extra", "reparse", "build"):
        print(__doc__)
        return 1
    cmd = argv[1]
    if cmd == "build":
        build()
        return 0
    key = financials.get_auth_key()
    if not key:
        print("환경변수 OPENDART_API_KEY가 필요합니다.")
        return 1
    args = argv[2:]
    opt = lambda name, default: int(args[args.index(name) + 1]) if name in args else default
    if cmd == "update":
        update(key, months=opt("--months", 36), max_docs=opt("--max-docs", None), include_konex="--konex" in args)
    elif cmd == "extra":
        update_extra(key)
    elif cmd == "reparse":
        reparse(key)
    build()
    return 0


if __name__ == "__main__":
    sys.exit(main(sys.argv))
