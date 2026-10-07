"""
OpenDART 전체 재무제표(fnlttSinglAcntAll) 수집 — EV/EBITDA·순현금·영업현금흐름·이자보상배율용
==============================================================================
주요계정 API에는 차입금·현금성자산·감가상각비·영업현금흐름·이자비용이 없어서 전체 재무제표를 받는다.
이 API는 회사 하나씩만 조회되므로(연결 없으면 별도로 한 번 더) 매일 정기 작업과 분리해 수동 실행한다.

표본(10개사) 결과: 한 번 조회로 당기·전기·전전기 3개년이 오고, 회사당 평균 약 7초 걸린다.
계정 코드가 회사마다 달라(차입금·사채·리스부채 등) 산식은 나중에 정하고, 관련 계정을 넓게 원본으로 저장한다.

data/full_accounts.json: { 종목코드: { y: 보고서연도, fs: CFS|OFS, rows: [[구분, 계정ID, 계정명, 당기, 전기, 전전기], ...] } }

사용법
  python full_financials.py sample   표본 10개사의 응답 원본과 호출 시간을 data/full_sample.json에 저장
  python full_financials.py full     summary.json의 전 종목을 동시 3건씩 조회해 full_accounts.json에 누적 저장
                                     (이미 받은 종목은 건너뛰고, 시간 제한에 걸리면 저장 후 종료 → 다시 실행하면 이어서)
"""
import json
import os
import re
import sys
import threading
import time
from concurrent.futures import ThreadPoolExecutor, as_completed
from datetime import datetime
from pathlib import Path

import requests

import financials  # corp_code 매핑 재사용

BASE_DIR = Path(__file__).resolve().parent
SAMPLE_PATH = BASE_DIR / "data" / "full_sample.json"
ACCOUNTS_PATH = BASE_DIR / "data" / "full_accounts.json"
SUMMARY_PATH = BASE_DIR / "data" / "summary.json"
API_BASE = "https://opendart.fss.or.kr/api"
WORKERS = 3                 # 동시 조회 수 (DART 호출 한도 대비 충분히 낮은 속도)
TIME_BUDGET_SEC = 270 * 60  # 워크플로 제한(300분) 전에 저장하고 끝내기 위한 시간
SAVE_EVERY = 100

# 저장할 계정: 계정명 키워드 또는 표준 계정ID 키워드 중 하나라도 맞으면 보관
KEEP_NAME = re.compile(r"현금및현금성자산|단기금융상품|차입금|사채|리스부채|부채총계|자본총계|영업이익|영업손실|이자비용|금융비용|금융원가"
                       r"|감가상각|상각비|영업활동|유형자산의 ?취득|무형자산의 ?취득|이자의 ?지급|당기순이익|당기순손실")
KEEP_ID = re.compile(r"CashAndCashEquivalents$|ShortTermDeposits|Borrowings|LoansReceived|Bonds|LeaseLiabilities|FinanceCosts|InterestExpense"
                     r"|Depreciation|Amortisation|OperatingActivities|PurchaseOfPropertyPlant|PurchaseOfIntangible|InterestPaid")

# 대형 연결 / 금융사 / 별도만 있는 소형사 / 거래정지 / 시가총액 미달 / 사전검토 종목을 섞은 표본
SAMPLE_CODES = ["005930", "000660", "105560", "002360", "060480", "011080", "001570", "312610", "035620", "024070"]
ROW_KEYS = ("sj_div", "account_id", "account_nm", "account_detail", "thstrm_amount", "frmtrm_amount", "bfefrmtrm_amount")


def log(message):
    print(message, flush=True)


def get_auth_key():
    key = os.environ.get("OPENDART_API_KEY")
    return key.strip() if key else key


def fetch_full(key, corp_code, year, fs_div):
    resp = requests.get(
        API_BASE + "/fnlttSinglAcntAll.json",
        params={"crtfc_key": key, "corp_code": corp_code, "bsns_year": year, "reprt_code": "11011", "fs_div": fs_div},
        timeout=60,
    )
    data = resp.json()
    return data.get("status"), data.get("message"), data.get("list", [])


def sample(key):
    corp_map = financials.get_corp_code_map(key)
    year = str(datetime.now().year - 1)
    out = {"year": year, "fetched_at": datetime.now().isoformat(timespec="seconds"), "companies": {}}
    total_started = time.time()
    for code in SAMPLE_CODES:
        corp_code = corp_map.get(code)
        if not corp_code:
            log(f"[full] {code}: corp_code 없음")
            continue
        entry = {"calls": []}
        for fs_div in ("CFS", "OFS"):
            started = time.time()
            try:
                status, message, rows = fetch_full(key, corp_code, year, fs_div)
            except Exception as e:
                status, message, rows = "ERR", str(e), []
            elapsed = round(time.time() - started, 2)
            entry["calls"].append({"fs_div": fs_div, "status": status, "message": message, "rows": len(rows), "sec": elapsed})
            log(f"[full] {code} {fs_div}: status={status} rows={len(rows)} {elapsed}초")
            if status == "000" and rows:
                entry["fs_div"] = fs_div
                entry["rows"] = [{k: r.get(k) for k in ROW_KEYS} for r in rows]
                break
            time.sleep(0.2)
        out["companies"][code] = entry
        time.sleep(0.2)
    out["total_sec"] = round(time.time() - total_started, 1)
    SAMPLE_PATH.parent.mkdir(parents=True, exist_ok=True)
    with open(SAMPLE_PATH, "w", encoding="utf-8") as f:
        json.dump(out, f, ensure_ascii=False, separators=(",", ":"))
    log(f"[full] 표본 완료: {len(out['companies'])}개사, 총 {out['total_sec']}초 -> {SAMPLE_PATH.name}")


XBRL_SAMPLE_PATH = BASE_DIR / "data" / "xbrl_sample.json"
# 소형사 위주 표본 + 비교용 대형사(삼성전자)
XBRL_SAMPLE_CODES = ["060480", "011080", "312610", "035620", "024070", "002360", "001570", "208640", "199730", "005930"]
DEPR_FACT = re.compile(r"<([\w\-]+):(\w*(?:Depreciation|Amortisation|Amortization)\w*)\s([^>]*?)>([^<]*)</", re.S)
CONTEXT = re.compile(r"<(?:\w+:)?context\s+id=\"([^\"]+)\"[^>]*>(.*?)</(?:\w+:)?context>", re.S)
MEMBER = re.compile(r"<(?:\w+:)?explicitMember[^>]*dimension=\"([^\"]+)\"[^>]*>([^<]+)<", re.S)
PERIOD = re.compile(r"<(?:\w+:)?(startDate|endDate|instant)>([^<]+)<")


def find_annual_report(key, corp_code, year):
    """해당 사업연도 사업보고서의 접수번호(정정 포함 최신)."""
    resp = requests.get(API_BASE + "/list.json", params={
        "crtfc_key": key, "corp_code": corp_code, "bgn_de": f"{int(year) + 1}0101", "end_de": f"{int(year) + 1}1231",
        "pblntf_ty": "A", "pblntf_detail_ty": "A001", "page_count": 10}, timeout=30)
    data = resp.json()
    items = [r for r in data.get("list", []) if "사업보고서" in (r.get("report_nm") or "")]
    return items[0]["rcept_no"] if items else None


def xbrl_sample(key):
    """사업보고서 XBRL 원본에서 감가상각·상각 관련 사실(fact)을 모두 뽑아 저장한다(주석 태깅 범위 확인용)."""
    import io
    import zipfile
    corp_map = financials.get_corp_code_map(key)
    year = str(datetime.now().year - 1)
    out = {"year": year, "companies": {}}
    for code in XBRL_SAMPLE_CODES:
        entry = {}
        started = time.time()
        try:
            rcept_no = find_annual_report(key, corp_map[code], year)
            entry["rcept_no"] = rcept_no
            if not rcept_no:
                entry["error"] = "사업보고서 없음"
            else:
                resp = requests.get(API_BASE + "/fnlttXbrl.xml", params={"crtfc_key": key, "rcept_no": rcept_no, "reprt_code": "11011"}, timeout=120)
                entry["bytes"] = len(resp.content)
                z = zipfile.ZipFile(io.BytesIO(resp.content))
                entry["files"] = z.namelist()
                text = "".join(z.read(n).decode("utf-8", "ignore") for n in z.namelist() if n.lower().endswith(".xbrl"))
                contexts = {}
                for cid, body in CONTEXT.findall(text):
                    contexts[cid] = {"period": dict(PERIOD.findall(body)), "members": [m[1] for m in MEMBER.findall(body)]}
                facts = []
                for prefix, name, attrs, value in DEPR_FACT.findall(text):
                    m = re.search(r'contextRef="([^"]+)"', attrs)
                    ctx = contexts.get(m.group(1) if m else "", {})
                    facts.append({"el": f"{prefix}:{name}", "v": value.strip(), "period": ctx.get("period"), "members": ctx.get("members")})
                entry["facts"] = facts
        except Exception as e:
            entry["error"] = str(e)
        entry["sec"] = round(time.time() - started, 2)
        log(f"[xbrl] {code}: {entry.get('error') or str(len(entry.get('facts', []))) + '개 감가상각 관련 항목'} ({entry['sec']}초, {entry.get('bytes', 0)//1024}KB)")
        out["companies"][code] = entry
        time.sleep(0.3)
    with open(XBRL_SAMPLE_PATH, "w", encoding="utf-8") as f:
        json.dump(out, f, ensure_ascii=False, separators=(",", ":"))
    log(f"[xbrl] 표본 완료 -> {XBRL_SAMPLE_PATH.name}")


XBRL_DEPR_PATH = BASE_DIR / "data" / "xbrl_depr.json"
TOTAL_MEMBERS = {"ifrs-full:ConsolidatedMember": "CFS", "ifrs-full:SeparateMember": "OFS"}


def xbrl_depr_one(key, corp_code, year):
    """사업보고서 XBRL에서 감가상각·상각의 연결/별도 합계 사실만 추린다(세부 구분 차원이 붙은 값은 제외)."""
    import io
    import zipfile
    rcept_no = find_annual_report(key, corp_code, year)
    if not rcept_no:
        return {"rcept_no": None, "facts": []}
    resp = requests.get(API_BASE + "/fnlttXbrl.xml", params={"crtfc_key": key, "rcept_no": rcept_no, "reprt_code": "11011"}, timeout=120)
    z = zipfile.ZipFile(io.BytesIO(resp.content))
    text = "".join(z.read(n).decode("utf-8", "ignore") for n in z.namelist() if n.lower().endswith(".xbrl"))
    contexts = {cid: (dict(PERIOD.findall(body)), [m[1] for m in MEMBER.findall(body)]) for cid, body in CONTEXT.findall(text)}
    facts = []
    for prefix, name, attrs, value in DEPR_FACT.findall(text):
        m = re.search(r'contextRef="([^"]+)"', attrs)
        period, members = contexts.get(m.group(1) if m else "", ({}, []))
        if len(members) > 1 or (members and members[0] not in TOTAL_MEMBERS):
            continue
        end = period.get("endDate") or period.get("instant") or ""
        num = to_number(value.strip())
        if num is None or not end:
            continue
        facts.append([f"{prefix}:{name}", num, end[:4], TOTAL_MEMBERS.get(members[0], "") if members else ""])
    return {"rcept_no": rcept_no, "facts": facts}


def xbrl_full(key):
    corp_map = financials.get_corp_code_map(key)
    accounts = json.load(open(ACCOUNTS_PATH, encoding="utf-8"))
    store = json.load(open(XBRL_DEPR_PATH, encoding="utf-8")) if XBRL_DEPR_PATH.exists() else {}
    has_depr = lambda rows: any(re.search(r"감가상각|상각비", r[2] or "") or "Depreciation" in (r[1] or "") for r in rows)
    targets = [c for c, v in accounts.items() if v.get("y") and corp_map.get(c) and not has_depr(v["rows"]) and c not in store]
    log(f"[xbrl] 대상 {len(targets)}개 종목 (본문에 감가상각비 없는 종목, 이미 받은 {len(store)}개 제외), 동시 {WORKERS}건")
    started = time.time()
    lock = threading.Lock()
    done = found = errors = 0
    stop = threading.Event()

    def work(code):
        if stop.is_set():
            return code, None, "skipped"
        try:
            return code, xbrl_depr_one(key, corp_map[code], str(accounts[code]["y"])), None
        except Exception as e:
            return code, None, str(e)

    def save():
        with open(XBRL_DEPR_PATH, "w", encoding="utf-8") as f:
            json.dump(store, f, ensure_ascii=False, separators=(",", ":"))

    with ThreadPoolExecutor(max_workers=WORKERS) as pool:
        for fut in as_completed([pool.submit(work, c) for c in targets]):
            code, result, err = fut.result()
            with lock:
                if result is not None:
                    store[code] = result
                    done += 1
                    found += 1 if result["facts"] else 0
                elif err and err != "skipped":
                    errors += 1
                    if errors <= 20:
                        log(f"[xbrl] {code} 오류: {err}")
                if done and done % SAVE_EVERY == 0:
                    save()
                    el = time.time() - started
                    log(f"[xbrl] 진행 {done}/{len(targets)} · 감가상각 확보 {found} · {el/60:.1f}분 · 남은 예상 {(len(targets)-done)*el/done/60:.0f}분")
                if not stop.is_set() and time.time() - started > TIME_BUDGET_SEC:
                    stop.set()
                    log("[xbrl] 시간 제한에 도달해 남은 종목은 다음 실행에서 이어서 받습니다.")
    save()
    log(f"[xbrl] 완료: 이번 실행 {done}개 조회, 감가상각 확보 {found}개, 오류 {errors}건, 총 {(time.time()-started)/60:.1f}분")


NOTES_SAMPLE_PATH = BASE_DIR / "data" / "notes_sample.json"
# 감가상각비가 없는 소형사 10곳 + 정답을 아는 검증용 2곳(060480 본문 5.9억, 001570 XBRL 177.8억)
NOTES_SAMPLE_CODES = ["011080", "208640", "199730", "086040", "481070", "038680", "109740", "289220", "006920", "011700", "060480", "001570"]
DA_ROW = re.compile(r"^(유형자산)?감가상각비(용)?$|^감가상각비및(무형자산)?상각비$|^감가상각비와상각비$|^무형자산(상각비|상각)$"
                    r"|^사용권자산(감가)?상각비$|^투자부동산(감가)?상각비$|^상각비$")
TAG = re.compile(r"<[^>]+>")
UNIT = re.compile(r"단위\s*[:：]?\s*(천원|백만원|억원|원)")


def cell_text(html):
    return re.sub(r"\s+", " ", TAG.sub(" ", html)).replace("&nbsp;", " ").strip()


def parse_amount(t):
    t = t.replace(",", "").replace(" ", "")
    neg = t.startswith("(") and t.endswith(")") or t.startswith("-") and len(t) > 1
    t = t.strip("()-△▲")
    return (-1 if neg else 1) * float(t) if re.fullmatch(r"\d+(\.\d+)?", t) else None


def notes_depr_candidates(text):
    """원문 XML에서 감가상각비 계열 행을 모두 찾아 (구간 제목, 표 직전 문구, 단위, 행 이름, 숫자들)로 돌려준다."""
    out, section, last_unit = [], "", None
    for m in re.finditer(r"<TITLE[^>]*>(.*?)</TITLE>|<TABLE[^>]*>(.*?)</TABLE>|<P[^>]*>(.*?)</P>", text, re.S | re.I):
        title, table, para = m.groups()
        if title is not None:
            section = cell_text(title)
            continue
        if para is not None:
            u = UNIT.search(cell_text(para))
            if u:
                last_unit = u.group(1)
            continue
        unit_in = UNIT.search(cell_text(table))
        unit = unit_in.group(1) if unit_in else last_unit
        before = cell_text(text[max(0, m.start() - 400):m.start()])[-120:]
        for row in re.findall(r"<TR[^>]*>(.*?)</TR>", table, re.S | re.I):
            cells = [cell_text(c) for c in re.findall(r"<(?:TD|TE|TH|TU)[^>]*>(.*?)</(?:TD|TE|TH|TU)>", row, re.S | re.I)]
            if not cells:
                continue
            label = re.sub(r"\s|\(\*?\d*\)|\*\d*|주석\d+|[①-⑩]", "", cells[0])
            if DA_ROW.search(label):
                nums = [parse_amount(c) for c in cells[1:]]
                out.append({"section": section[:60], "before": before, "unit": unit, "label": cells[0][:30],
                            "nums": [n for n in nums if n is not None][:6]})
    return out


def notes_sample(key):
    import io
    import zipfile
    corp_map = financials.get_corp_code_map(key)
    xbrl = json.load(open(XBRL_DEPR_PATH, encoding="utf-8")) if XBRL_DEPR_PATH.exists() else {}
    year = str(datetime.now().year - 1)
    out = {"year": year, "companies": {}}
    for code in NOTES_SAMPLE_CODES:
        entry, started = {}, time.time()
        try:
            rcept_no = (xbrl.get(code) or {}).get("rcept_no") or find_annual_report(key, corp_map[code], year)
            entry["rcept_no"] = rcept_no
            resp = requests.get(API_BASE + "/document.xml", params={"crtfc_key": key, "rcept_no": rcept_no}, timeout=180)
            entry["bytes"] = len(resp.content)
            z = zipfile.ZipFile(io.BytesIO(resp.content))
            entry["files"] = z.namelist()
            text = ""
            for n in z.namelist():
                raw = z.read(n)
                for enc in ("utf-8", "cp949"):
                    try:
                        text += raw.decode(enc)
                        break
                    except UnicodeDecodeError:
                        continue
            entry["candidates"] = notes_depr_candidates(text)
        except Exception as e:
            entry["error"] = str(e)
        entry["sec"] = round(time.time() - started, 2)
        log(f"[notes] {code}: {entry.get('error') or str(len(entry.get('candidates', []))) + '개 후보 행'} ({entry['sec']}초, {entry.get('bytes', 0)//1024}KB)")
        out["companies"][code] = entry
        time.sleep(0.3)
    with open(NOTES_SAMPLE_PATH, "w", encoding="utf-8") as f:
        json.dump(out, f, ensure_ascii=False, separators=(",", ":"))
    log(f"[notes] 표본 완료 -> {NOTES_SAMPLE_PATH.name}")


def to_number(v):
    if v is None or v == "":
        return None
    try:
        return int(str(v).replace(",", ""))
    except ValueError:
        return None


def keep(row):
    return bool(KEEP_NAME.search(row.get("account_nm") or "") or KEEP_ID.search(row.get("account_id") or ""))


def collect_one(key, corp_code, latest):
    """최신 사업보고서(연결 우선, 없으면 별도)의 관련 계정만 추린다. 최신이 없으면 전년 보고서."""
    for year in (latest, latest - 1):
        for fs_div in ("CFS", "OFS"):
            status, message, rows = fetch_full(key, corp_code, str(year), fs_div)
            if status == "000" and rows:
                kept = [[r.get("sj_div"), r.get("account_id"), (r.get("account_nm") or "").strip(),
                         to_number(r.get("thstrm_amount")), to_number(r.get("frmtrm_amount")), to_number(r.get("bfefrmtrm_amount"))]
                        for r in rows if keep(r)]
                return {"y": year, "fs": fs_div, "rows": kept}
            if status not in ("000", "013"):
                raise RuntimeError(f"status={status} {message}")
            time.sleep(0.15)
    return {"y": None, "fs": None, "rows": []}


def full(key):
    corp_map = financials.get_corp_code_map(key)
    summary = json.load(open(SUMMARY_PATH, encoding="utf-8"))
    latest = datetime.now().year - 1
    store = json.load(open(ACCOUNTS_PATH, encoding="utf-8")) if ACCOUNTS_PATH.exists() else {}
    targets = [s["c"] for s in summary["stocks"] if corp_map.get(s["c"]) and store.get(s["c"], {}).get("y") != latest]
    log(f"[full] 대상 {len(targets)}개 종목 (이미 받은 종목 {len(store)}개 제외), 동시 {WORKERS}건")

    started = time.time()
    lock = threading.Lock()
    done = errors = 0
    stop = threading.Event()

    def work(code):
        if stop.is_set():
            return code, None, "skipped"
        try:
            return code, collect_one(key, corp_map[code], latest), None
        except Exception as e:
            return code, None, str(e)

    def save():
        with open(ACCOUNTS_PATH, "w", encoding="utf-8") as f:
            json.dump(store, f, ensure_ascii=False, separators=(",", ":"))

    with ThreadPoolExecutor(max_workers=WORKERS) as pool:
        futures = [pool.submit(work, c) for c in targets]
        for fut in as_completed(futures):
            code, result, err = fut.result()
            with lock:
                if result is not None:
                    store[code] = result
                    done += 1
                elif err and err != "skipped":
                    errors += 1
                    if errors <= 20:
                        log(f"[full] {code} 오류: {err}")
                if done and done % SAVE_EVERY == 0:
                    save()
                    el = time.time() - started
                    log(f"[full] 진행 {done}/{len(targets)} · {el/60:.1f}분 · 종목당 {el/done:.2f}초 · 남은 예상 {(len(targets)-done)*el/done/60:.0f}분")
                if not stop.is_set() and time.time() - started > TIME_BUDGET_SEC:
                    stop.set()
                    log("[full] 시간 제한에 도달해 남은 종목은 다음 실행에서 이어서 받습니다.")
    save()
    remaining = len([c for c in targets if store.get(c, {}).get("y") != latest])
    log(f"[full] 완료: 이번 실행 {done}개 확보, 오류 {errors}건, 남은 종목 {remaining}개, 총 {(time.time()-started)/60:.1f}분")


if __name__ == "__main__":
    key = get_auth_key()
    if not key:
        print("환경변수 OPENDART_API_KEY가 필요합니다.")
        sys.exit(1)
    cmd = sys.argv[1] if len(sys.argv) > 1 else ""
    if cmd == "sample":
        sample(key)
    elif cmd == "full":
        full(key)
    elif cmd == "xbrl_sample":
        xbrl_sample(key)
    elif cmd == "xbrl":
        xbrl_full(key)
    elif cmd == "notes_sample":
        notes_sample(key)
    else:
        print(__doc__)
        sys.exit(1)
