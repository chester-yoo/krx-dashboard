"""
OpenDART 연도별 핵심 재무정보(매출액/영업이익/당기순이익/자산총계/부채총계/자본총계/자본금) 수집
=============================================================
KRX API에는 재무정보가 없어서 OpenDART(전자공시시스템) 공식 API를 사용한다.

수집 방식
  다중회사 주요계정 API(fnlttMultiAcnt)로 회사 100개를 한 번에 조회한다.
  사업보고서 1건의 응답에 당기/전기/전전기 3개 연도가 들어 있으므로,
  최신 사업보고서(작년 기준) 하나로 최근 3개 연도를 모두 얻는다. (전체 약 26회 호출)
  다중 조회가 실패한 묶음만 단일회사 API(fnlttSinglAcnt)로 한 곳씩 다시 조회한다.

data/corp_codes.json:   종목코드(6자리) -> DART corp_code(8자리) 매핑 캐시
data/dart_accounts.json: 응답 원본(계정명 단위). { 종목코드: { 보고서연도: { CFS|OFS: { 계정명: [당기, 전기, 전전기] } } } }
                         새 지표가 필요하면 API를 다시 호출하지 않고 이 파일에서 꺼낸다(rebuild).
data/financials.json:   화면용 요약. { 종목코드: { 사업연도: {revenue, operating_profit, net_income,
                                                         assets, liabilities, equity, capital, fs_div} } }

사용법
  python financials.py update [--years N] [--allow-single]
                                            dart_accounts.json에 없는 종목만 다중 조회한 뒤 financials.json 갱신.
                                            먼저 다중 조회 가능 여부를 점검하고, 불가하면 건너뛴다(--allow-single이면 단일 조회로 진행)
  python financials.py rebuild [--years N]  API 호출 없이 dart_accounts.json으로 financials.json만 다시 생성
"""
import io
import json
import os
import re
import sys
import time
import zipfile
from datetime import datetime
from pathlib import Path

import requests

import fx

BASE_DIR = Path(__file__).resolve().parent
HISTORY_PATH = BASE_DIR / "data" / "history.json"
CORP_CODE_PATH = BASE_DIR / "data" / "corp_codes.json"
SUMMARY_PATH = BASE_DIR / "data" / "summary.json"
CORP_CODE_META_PATH = BASE_DIR / "data" / "corp_codes_meta.json"
FINANCIALS_PATH = BASE_DIR / "data" / "financials.json"
ACCOUNTS_PATH = BASE_DIR / "data" / "dart_accounts.json"

API_BASE = "https://opendart.fss.or.kr/api"
BATCH_SIZE = 100
MIN_SPLIT = 10      # 다중 조회가 실패하면 이 크기까지 절반씩 나눠 재시도
MAX_FALLBACK = 200  # 단일 조회 대체는 실행당 최대 건수(초과하면 건너뛰고 경고)
AMOUNT_KEYS = ("thstrm_amount", "frmtrm_amount", "bfefrmtrm_amount")  # 당기, 전기, 전전기
MAX_YEARS = len(AMOUNT_KEYS)

REVENUE_NAMES = {"매출액", "수익(매출액)"}
OPERATING_PROFIT_NAMES = {"영업이익", "영업이익(손실)"}
NET_INCOME_NAMES = {"당기순이익(손실)", "당기순이익"}
ASSET_NAMES = {"자산총계"}
LIABILITY_NAMES = {"부채총계"}
EQUITY_NAMES = {"자본총계"}
CAPITAL_NAMES = {"자본금"}

FIELD_BY_NAME = {}
for _names, _field in (
    (REVENUE_NAMES, "revenue"),
    (OPERATING_PROFIT_NAMES, "operating_profit"),
    (NET_INCOME_NAMES, "net_income"),
    (ASSET_NAMES, "assets"),
    (LIABILITY_NAMES, "liabilities"),
    (EQUITY_NAMES, "equity"),
    (CAPITAL_NAMES, "capital"),
):
    for _n in _names:
        FIELD_BY_NAME[_n] = _field


def get_auth_key():
    key = os.environ.get("OPENDART_API_KEY")
    return key.strip() if key else key


def to_number(v):
    if v is None or v == "":
        return None
    try:
        return int(str(v).replace(",", ""))
    except ValueError:
        return None


def load_json(path, default):
    if not path.exists():
        return default
    with open(path, "r", encoding="utf-8") as f:
        return json.load(f)


def save_json(path, data):
    path.parent.mkdir(parents=True, exist_ok=True)
    with open(path, "w", encoding="utf-8") as f:
        json.dump(data, f, ensure_ascii=False, indent=0, separators=(",", ":"))


def _norm_name(name):
    return re.sub(r"\s|주식회사|\(주\)|㈜|\(Reg\.S\)|\(유\)", "", name or "").upper()


def fetch_corp_codes(key):
    """DART 전체 기업코드 목록을 받아 상장 종목코드 -> corp_code 매핑을 만든다.
    한 종목코드에 기업이 여럿 걸려 있는 경우(분할 신설법인 등)가 있어, KRX 종목명과 회사명이 같은 기업을 고르고
    없으면 최근 수정된 기업을 고른다. 신규 상장 종목의 영숫자 종목코드(예: 0001A0)도 받는다."""
    resp = requests.get(API_BASE + "/corpCode.xml", params={"crtfc_key": key}, timeout=60)
    resp.raise_for_status()
    z = zipfile.ZipFile(io.BytesIO(resp.content))
    xml_bytes = z.read(z.namelist()[0])
    text = xml_bytes.decode("utf-8")
    krx_names = {}
    if SUMMARY_PATH.exists():
        krx_names = {r["c"]: _norm_name(r.get("n")) for r in load_json(SUMMARY_PATH, {}).get("stocks", [])}

    candidates = {}
    for m in re.finditer(r"<list>(.*?)</list>", text, re.S):
        item = dict(re.findall(r"<(\w+)>\s*(.*?)\s*</\1>", m.group(1), re.S))
        stock_code = item.get("stock_code", "").strip()
        if stock_code:
            candidates.setdefault(stock_code, []).append(item)
    mapping, duplicated = {}, 0
    for stock_code, items in candidates.items():
        if len(items) > 1:
            duplicated += 1
        name = krx_names.get(stock_code)
        same = [i for i in items if name and _norm_name(i.get("corp_name")) == name]
        pick = max(same or items, key=lambda i: i.get("modify_date", ""))
        mapping[stock_code] = pick["corp_code"]
    log(f"[financials] 기업코드 {len(mapping)}개 (종목코드 중복 {duplicated}건은 KRX 종목명 일치 기준으로 선택)")
    return mapping


def get_corp_code_map(key, refresh=False):
    """받은 지 7일이 지났으면 새로 받는다(신규 상장·기업코드 변경 반영). 받은 날짜는 corp_codes_meta.json에 남긴다
    (워크플로 체크아웃은 파일 수정시각이 매번 바뀌어 날짜 판단에 쓸 수 없다)."""
    meta = load_json(CORP_CODE_META_PATH, {})
    fresh = meta.get("fetched") and (datetime.now() - datetime.fromisoformat(meta["fetched"])).days < 7
    if not refresh and fresh:
        cached = load_json(CORP_CODE_PATH, None)
        if cached:
            return cached
    mapping = fetch_corp_codes(key)
    save_json(CORP_CODE_PATH, mapping)
    save_json(CORP_CODE_META_PATH, {"fetched": datetime.now().isoformat(timespec="seconds")})
    return mapping


def fetch_multi_rows(key, corp_codes, year):
    """회사 여러 곳(최대 100)의 주요계정 행 목록. 데이터가 없으면 빈 목록."""
    resp = requests.get(
        API_BASE + "/fnlttMultiAcnt.json",
        params={"crtfc_key": key, "corp_code": ",".join(corp_codes), "bsns_year": year, "reprt_code": "11011"},
        timeout=120,
    )
    data = resp.json()
    status = data.get("status")
    if status == "013":
        return []
    if status != "000":
        raise RuntimeError(f"fnlttMultiAcnt status={status} {data.get('message')}")
    return data.get("list", [])


def fetch_single_rows(key, corp_code, year):
    """회사 한 곳의 주요계정 행 목록(다중 조회 실패 시 대체 경로)."""
    resp = requests.get(
        API_BASE + "/fnlttSinglAcnt.json",
        params={"crtfc_key": key, "corp_code": corp_code, "bsns_year": year, "reprt_code": "11011"},
        timeout=20,
    )
    data = resp.json()
    if data.get("status") != "000":
        return []
    return data.get("list", [])


def add_rows(raw, rows, corp_to_stock, year, force_code=None):
    """응답 행을 종목별 원본 저장소에 넣는다. 같은 계정명이 여러 번 나오면 처음 값을 쓴다."""
    added = set()
    for row in rows:
        code = force_code or corp_to_stock.get(row.get("corp_code")) or (row.get("stock_code") or "").strip() or None
        fs_div = row.get("fs_div")
        name = row.get("account_nm", "")
        if not code or fs_div not in ("CFS", "OFS") or not name:
            continue
        amounts = [to_number(row.get(k)) for k in AMOUNT_KEYS]
        if all(a is None for a in amounts):
            continue
        accounts = raw.setdefault(code, {}).setdefault(str(year), {}).setdefault(fs_div, {})
        accounts.setdefault(name, amounts)
        added.add(code)
    return added


def derive_entry(raw_code, year, latest):
    """원본 저장소에서 사업연도 year의 요약 항목을 만든다. 최신 보고서를 우선 쓴다."""
    for report_year in (latest, latest - 1):
        report = raw_code.get(str(report_year))
        idx = report_year - year
        if not report or not 0 <= idx < MAX_YEARS:
            continue
        result = {"CFS": {}, "OFS": {}}
        for fs_div in ("CFS", "OFS"):
            for name, amounts in report.get(fs_div, {}).items():
                field = FIELD_BY_NAME.get(name)
                amount = amounts[idx] if idx < len(amounts) else None
                if field and amount is not None and field not in result[fs_div]:
                    result[fs_div][field] = amount
        # 연결(CFS) 우선, 없는 항목은 개별(OFS)로 보완
        merged = dict(result["OFS"])
        merged.update(result["CFS"])
        if merged:
            merged["fs_div"] = "CFS" if result["CFS"] else "OFS"
            return merged
    return None


def rebuild_financials(raw, years, latest):
    """기존 financials.json을 바탕으로, 원본 저장소에서 만든 연도 항목으로 덮어쓴다."""
    financials = load_json(FINANCIALS_PATH, {})
    target_years = [latest - i for i in range(years)]
    updated = 0
    for code, raw_code in raw.items():
        for y in target_years:
            entry = derive_entry(raw_code, y, latest)
            if entry:
                # 외화 공시 종목은 원화로 환산 (fx.py 참고)
                entry = {k: (v if k == "fs_div" else fx.to_krw(code, y, k, v)) for k, v in entry.items()}
                financials.setdefault(code, {})[str(y)] = entry
                updated += 1
    save_json(FINANCIALS_PATH, financials)
    return financials, updated


def log(message):
    print(message, flush=True)


def collect_batch(key, corp_codes, year, stats):
    """다중 조회. 실패하면 절반으로 나눠 다시 시도하고, MIN_SPLIT개 이하에서도 실패한 corp_code는 돌려준다."""
    started = time.time()
    try:
        rows = fetch_multi_rows(key, corp_codes, year)
        stats["calls"] += 1
        log(f"[financials] 다중 조회 {len(corp_codes)}개: {len(rows)}행, {time.time() - started:.1f}초")
        return rows, []
    except Exception as e:
        log(f"[financials] 다중 조회 실패 {len(corp_codes)}개 ({time.time() - started:.1f}초): {e}")
        if len(corp_codes) <= MIN_SPLIT:
            return [], list(corp_codes)
        mid = len(corp_codes) // 2
        rows1, failed1 = collect_batch(key, corp_codes[:mid], year, stats)
        rows2, failed2 = collect_batch(key, corp_codes[mid:], year, stats)
        return rows1 + rows2, failed1 + failed2


def check_multi_supported(key, pending, corp_map, corp_to_stock, year):
    """다중 조회가 되는지 소량(최대 10개)으로 먼저 확인한다. 응답 형식도 같이 확인한다."""
    sample = pending[:10]
    started = time.time()
    try:
        rows = fetch_multi_rows(key, [corp_map[c] for c in sample], year)
    except Exception as e:
        log(f"[financials] 사전 점검 실패: 다중 조회 호출 오류 ({time.time() - started:.1f}초): {e}")
        return False
    if not rows:
        log(f"[financials] 사전 점검 실패: 표본 {len(sample)}개에서 응답 행이 없습니다. ({time.time() - started:.1f}초)")
        return False
    need = {"account_nm", "fs_div", "thstrm_amount", "frmtrm_amount", "bfefrmtrm_amount"}
    missing = need - set(rows[0].keys())
    if missing:
        log(f"[financials] 사전 점검 실패: 응답에 필드가 없습니다 {sorted(missing)} (있는 필드: {sorted(rows[0].keys())})")
        return False
    matched = add_rows({}, rows, corp_to_stock, year)
    if not matched:
        log(f"[financials] 사전 점검 실패: 종목코드 매칭 불가 (corp_code/stock_code 확인 필요, 있는 필드: {sorted(rows[0].keys())})")
        return False
    log(f"[financials] 사전 점검 통과: 표본 {len(sample)}개 중 {len(matched)}개 매칭, {len(rows)}행, {time.time() - started:.1f}초")
    return True


def update(codes, years, key, allow_single=False):
    years = min(years, MAX_YEARS)
    corp_map = get_corp_code_map(key)
    corp_to_stock = {v: k for k, v in corp_map.items()}
    raw = load_json(ACCOUNTS_PATH, {})
    latest = datetime.now().year - 1  # 최신 확정 사업보고서 기준
    listed = [c for c in dict.fromkeys(codes) if corp_map.get(c)]
    no_corp_code = len(set(codes)) - len(listed)

    stats = {"calls": 0, "fallback": 0, "skipped": 0}
    multi_checked = False
    use_multi = True
    max_fallback = 10 ** 9 if allow_single else MAX_FALLBACK
    for report_year in (latest, latest - 1):
        if report_year == latest:
            pending = [c for c in listed if str(latest) not in raw.get(c, {})]
        else:
            pending = [c for c in listed if str(latest) not in raw.get(c, {}) and str(report_year) not in raw.get(c, {})]
        if not pending:
            continue
        if not multi_checked:
            multi_checked = True
            use_multi = check_multi_supported(key, pending, corp_map, corp_to_stock, report_year)
            if not use_multi:
                if not allow_single:
                    log("[financials] 다중 조회를 쓸 수 없어 이번 재무 수집을 건너뜁니다. "
                        "단일 조회(느림)로 진행하려면 --allow-single 옵션을 지정하세요.")
                    return
                log("[financials] --allow-single 지정: 다중 조회 없이 단일 조회로 진행합니다.")
        matched_total = 0
        log(f"[financials] {report_year} 보고서 조회 시작: 대상 {len(pending)}개")
        for i in range(0, len(pending), BATCH_SIZE):
            batch = pending[i:i + BATCH_SIZE]
            if use_multi:
                rows, failed = collect_batch(key, [corp_map[c] for c in batch], report_year, stats)
            else:
                rows, failed = [], [corp_map[c] for c in batch]
            matched_total += len(add_rows(raw, rows, corp_to_stock, report_year))
            for corp_code in failed:
                if stats["fallback"] >= max_fallback:
                    stats["skipped"] += 1
                    continue
                code = corp_to_stock.get(corp_code)
                try:
                    rows = fetch_single_rows(key, corp_code, report_year)
                    stats["fallback"] += 1
                    matched_total += len(add_rows(raw, rows, corp_to_stock, report_year, force_code=code))
                except Exception as e:
                    log(f"[financials] {code} {report_year} 단일 조회 오류: {e}")
                time.sleep(0.15)
            time.sleep(0.3)
        log(f"[financials] {report_year} 보고서: 대상 {len(pending)}개 중 {matched_total}개 확보")
        if len(pending) >= 200 and matched_total < len(pending) * 0.5:
            log("[financials] 경고: 확보율이 낮습니다. 다중 조회 응답 형식(corp_code/stock_code/fs_div)이나 실패 로그를 확인하세요.")
        save_json(ACCOUNTS_PATH, raw)

    save_json(ACCOUNTS_PATH, raw)
    financials, updated = rebuild_financials(raw, years, latest)
    if stats["skipped"]:
        log(f"[financials] 경고: 단일 조회 상한({MAX_FALLBACK}건) 초과로 {stats['skipped']}개 종목을 건너뛰었습니다.")
    log(f"[financials] 완료: 다중 호출 {stats['calls']}회, 단일 대체 호출 {stats['fallback']}회, corp_code 없음 {no_corp_code}개 종목, "
        f"재무 보유 {len(financials)}개 종목 ({updated}개 연도 항목 갱신)")


if __name__ == "__main__":
    cmd = sys.argv[1] if len(sys.argv) > 1 else ""
    years = 3
    if "--years" in sys.argv:
        years = int(sys.argv[sys.argv.index("--years") + 1])

    if cmd == "rebuild":
        raw = load_json(ACCOUNTS_PATH, {})
        if not raw:
            print("data/dart_accounts.json이 없습니다. 먼저 update를 실행하세요.")
            sys.exit(1)
        financials, updated = rebuild_financials(raw, min(years, MAX_YEARS), datetime.now().year - 1)
        print(f"[financials] rebuild 완료: {len(financials)}개 종목, {updated}개 연도 항목")
    elif cmd == "update":
        key = get_auth_key()
        if not key:
            print("환경변수 OPENDART_API_KEY가 필요합니다.")
            sys.exit(1)
        if not HISTORY_PATH.exists():
            print("data/history.json이 없습니다. 먼저 collect.py를 실행하세요.")
            sys.exit(1)
        history = json.load(open(HISTORY_PATH, encoding="utf-8"))
        update(sorted({r["code"] for r in history}), years, key, allow_single="--allow-single" in sys.argv)
    else:
        print(__doc__)
        sys.exit(1)
