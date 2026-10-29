"""수집기(목록 기간 분할·필터·정정 처리·원문 zip·캐시·한도) 및 빌드 검증. 네트워크 없이 가짜 세션으로 실행."""
import io
import json
import zipfile
from datetime import date

import pytest

import pledge
from conftest import FIXTURES


class FakeResp:
    def __init__(self, payload=None, content=None, status=200):
        self._payload, self.content, self.status_code = payload, content, status

    def json(self):
        return self._payload


class FakeSession:
    """list.json은 items를 기간 필터·페이지로 나눠 돌려주고, document.xml은 fixture를 zip으로 돌려준다."""

    def __init__(self, items, page_size=2):
        self.items, self.page_size, self.calls = items, page_size, []

    def get(self, url, params, timeout):
        self.calls.append((url.rsplit("/", 1)[-1], dict(params)))
        if url.endswith("/list.json"):
            hit = [i for i in self.items if params["bgn_de"] <= i["rcept_dt"] <= params["end_de"]]
            if not hit:
                return FakeResp({"status": "013", "message": "조회된 데이타가 없습니다."})
            pages = [hit[k:k + self.page_size] for k in range(0, len(hit), self.page_size)]
            p = params["page_no"]
            return FakeResp({"status": "000", "total_page": len(pages), "list": pages[p - 1]})
        if url.endswith("/document.xml"):
            no = params["rcept_no"]
            buf = io.BytesIO()
            with zipfile.ZipFile(buf, "w") as z:
                z.writestr(f"{no}.xml", (FIXTURES / f"{no}.xml").read_bytes())
                z.writestr(f"{no}_00760.xml", b"<DOCUMENT/>")  # 첨부(작은 파일)
            return FakeResp(content=buf.getvalue())
        raise AssertionError(url)


def item(no, code, name, flr, dt, report_nm="주식등의대량보유상황보고서(일반)", cls="K"):
    return {"rcept_no": no, "corp_code": "0" + code + "0", "corp_name": name, "stock_code": code, "corp_cls": cls,
            "report_nm": report_nm, "flr_nm": flr, "rcept_dt": dt}


@pytest.fixture(autouse=True)
def no_sleep(monkeypatch):
    monkeypatch.setattr(pledge.time, "sleep", lambda s: None)


@pytest.fixture
def tmp_paths(tmp_path, monkeypatch):
    monkeypatch.setattr(pledge, "DOCS_PATH", tmp_path / "pledge_docs.json")
    monkeypatch.setattr(pledge, "EXTRA_PATH", tmp_path / "pledge_extra.json")
    monkeypatch.setattr(pledge, "OUT_PATH", tmp_path / "pledge.json")
    monkeypatch.setattr(pledge, "CACHE_DIR", tmp_path / "cache")
    monkeypatch.setattr(pledge, "SUMMARY_PATH", tmp_path / "summary.json")
    monkeypatch.setattr(pledge, "HISTORY_PATH", tmp_path / "history.json")
    monkeypatch.setattr(pledge, "FINANCIALS_PATH", tmp_path / "financials.json")
    monkeypatch.setattr(pledge, "EXT_PATH", tmp_path / "fin_ext.json")
    monkeypatch.setattr(pledge, "STAGE_PATH", tmp_path / "stage.json")
    return tmp_path


def test_split_windows():
    w = pledge.split_windows("20260101", "20261028")
    assert w[0] == ("20260101", "20260331") and w[-1][1] == "20261028"
    assert all((date(int(e[:4]), int(e[4:6]), int(e[6:])) - date(int(b[:4]), int(b[4:6]), int(b[6:]))).days < 90 for b, e in w)
    assert pledge.split_windows("20261028", "20261028") == [("20261028", "20261028")]


@pytest.mark.parametrize("nm,cls,ok", [
    ("주식등의대량보유상황보고서(일반)", "K", True),
    ("[기재정정]주식등의대량보유상황보고서(일반)", "Y", True),
    ("주식등의대량보유상황보고서(약식)", "K", False),
    ("임원ㆍ주요주주특정증권등소유상황보고서", "K", False),
    ("주식등의대량보유상황보고서(일반)", "N", False),
    ("주식등의대량보유상황보고서(일반)", "E", False),
])
def test_is_target(nm, cls, ok):
    assert pledge.is_target(item("1", "000001", "x", "y", "20261027", nm, cls)) is ok


def test_list_paging_and_windows():
    items = [item(str(i), "000001", "a", "b", d) for i, d in enumerate(["20260105", "20260110", "20260115", "20260501", "20260502"])]
    s = FakeSession(items, page_size=2)
    got = pledge.Dart("KEY", session=s).list_reports("20260101", "20260630")
    assert [g["rcept_no"] for g in got] == ["0", "1", "2", "3", "4"]
    windows = {(p["bgn_de"], p["end_de"]) for n, p in s.calls}
    assert windows == {("20260101", "20260331"), ("20260401", "20260629"), ("20260630", "20260630")}
    assert all(p["pblntf_detail_ty"] == "D001" and p["crtfc_key"] == "KEY" for _, p in s.calls)


def test_limit_status_stops():
    class S:
        def get(self, url, params, timeout):
            return FakeResp({"status": "020", "message": "사용한도를 초과하였습니다."})
    with pytest.raises(pledge.DartLimitError):
        pledge.Dart("K", session=S()).json("/list.json", {})


def test_retry_backoff_then_success():
    class S:
        n = 0

        def get(self, url, params, timeout):
            S.n += 1
            if S.n < 3:
                raise pledge.requests.ConnectionError("reset")
            return FakeResp({"status": "000", "list": []})
    assert pledge.Dart("K", session=S()).json("/list.json", {}) == {"status": "000", "list": []}
    assert S.n == 3


def test_budget():
    d = pledge.Dart("K", budget=1, session=FakeSession([]))
    d.json("/list.json", {"bgn_de": "20260101", "end_de": "20260101", "page_no": 1})
    with pytest.raises(pledge.DartLimitError):
        d.json("/list.json", {"bgn_de": "20260101", "end_de": "20260101", "page_no": 1})


def test_unzip_prefers_named_and_decodes_euckr():
    buf = io.BytesIO()
    with zipfile.ZipFile(buf, "w") as z:
        z.writestr("big_attach.xml", "x" * 1000)
        z.writestr("20260101000001.xml", "<DOCUMENT>한글</DOCUMENT>".encode("euc-kr"))
    assert pledge.unzip_document(buf.getvalue(), "20260101000001") == "<DOCUMENT>한글</DOCUMENT>"


def test_snapshot_and_correction():
    R = lambda dt, rd, st="parsed": {"stock_code": "000001", "reporter_key": "홍길동|600101", "rcept_dt": dt,
                                     "report_date": rd, "status": st}
    reports = {
        "20260101000001": R("20260101", "20251230"),  # 원보고서 A
        "20260201000001": R("20260201", "20260130"),  # 최신 보고서 B
        "20260210000001": R("20260210", "20251230"),  # A의 기재정정(B보다 늦게 접수) -> B를 덮으면 안 됨
        "20260105000001": R("20260105", "20260103"),  # A 이후, B 이전 보고서
    }
    cur = pledge.select_snapshot(reports)
    assert cur == {"20260201000001"}
    assert reports["20260101000001"]["superseded_by"] == "20260201000001"
    # B가 없으면 정정본이 원보고서 A를 대체
    del reports["20260201000001"], reports["20260105000001"]
    assert pledge.select_snapshot(reports) == {"20260210000001"}


def test_update_end_to_end(tmp_paths, monkeypatch):
    items = [
        item("20261027000304", "299030", "하나기술", "오태봉", "20261027"),
        item("20261027000385", "090410", "덕신이피씨", "김명환", "20261027"),
        item("20261027000368", "069540", "빛과전자", "비엔에스조합", "20261027"),
        item("20250101000001", "299030", "하나기술", "오태봉", "20250101"),  # 같은 보고자의 오래된 보고서 -> 원문 생략
        item("20261027999999", "299030", "하나기술", "오태봉", "20261027", "주식등의대량보유상황보고서(약식)"),
    ]
    s = FakeSession(items, page_size=100)
    monkeypatch.setattr(pledge.requests, "get", s.get)
    pledge.update("KEY", months=36)
    store = json.loads(pledge.DOCS_PATH.read_text(encoding="utf-8"))
    rep = store["reports"]
    assert "20261027999999" not in rep
    assert rep["20261027000304"]["status"] == "parsed" and rep["20261027000304"]["has_pledge"]
    assert rep["20261027000385"]["status"] == "parsed" and not rep["20261027000385"]["has_contracts"]
    assert rep["20250101000001"]["status"] == "skipped"
    assert sum(1 for n, _ in s.calls if n == "document.xml") == 3
    assert (pledge.CACHE_DIR / "20261027000304.xml").exists()
    assert store["collected_through"] == date.today().strftime("%Y%m%d")

    # 두 번째 실행: 증분(겹침 3일)만 조회하고 원문은 다시 받지 않는다
    s.calls.clear()
    pledge.update("KEY")
    assert sum(1 for n, _ in s.calls if n == "document.xml") == 0
    expect_bgn = (date.today() - pledge.timedelta(days=pledge.LIST_OVERLAP_DAYS)).strftime("%Y%m%d")
    assert {p["bgn_de"] for n, p in s.calls if n == "list.json"} == {expect_bgn}


def test_failed_document_goes_to_review(tmp_paths, monkeypatch):
    class S(FakeSession):
        def get(self, url, params, timeout):
            if url.endswith("/document.xml"):
                return FakeResp(content=b"<result><status>014</status><message>file not found</message></result>")
            return super().get(url, params, timeout)
    s = S([item("20261027000304", "299030", "하나기술", "오태봉", "20261027")])
    monkeypatch.setattr(pledge.requests, "get", s.get)
    pledge.update("KEY")
    rep = json.loads(pledge.DOCS_PATH.read_text(encoding="utf-8"))["reports"]["20261027000304"]
    assert rep["status"] == "failed" and rep["tries"] == 1 and "014" in rep["error"]
    out = pledge.build(today=date(2026, 10, 28))
    assert out["review"][0]["reasons"] == ["PARSE_FAILED"]


def test_match_ceo():
    rows = [{"provider": "오태봉", "provider_id": "660307", "provider_type": "INDIVIDUAL", "rel": "M"},
            {"provider": "오가족", "provider_id": "900101", "provider_type": "INDIVIDUAL", "rel": "S"},
            {"provider": "(주)지주", "provider_id": "123-45-67890", "provider_type": "CORPORATE", "rel": "S"}]
    reps = [{"reporter_name": "오태봉", "reporter_id": "660307"}]
    ex = {"src": "2026-11012", "ceos": [{"nm": "오태봉", "birth": "6603", "ofcps": "대표이사"}]}
    assert pledge.match_ceo(rows, reps, ex) == {"오태봉": "CEO", "오가족": "CEO_RELATED", "(주)지주": "CORPORATE"}
    # 생년월 불일치(동명이인) -> 대표이사 아님
    ex2 = {"src": "x", "ceos": [{"nm": "오태봉", "birth": "7001", "ofcps": "대표이사"}]}
    assert pledge.match_ceo(rows[:1], reps, ex2) == {"오태봉": "NON_CEO_INDIVIDUAL"}
    # 생년월 누락 / 임원현황 없음 -> UNMATCHED
    ex3 = {"src": "x", "ceos": [{"nm": "오태봉", "birth": None, "ofcps": "대표이사"}]}
    assert pledge.match_ceo(rows[:1], reps, ex3) == {"오태봉": "UNMATCHED"}
    assert pledge.match_ceo(rows[:1], reps, None) == {"오태봉": "UNMATCHED"}
    assert pledge.birth_yymm("1966년 03월") == "6603"


def test_build_joins(tmp_paths, monkeypatch):
    items = [item(n, c, nm, f, "20261027") for n, c, nm, f in [
        ("20261027000304", "299030", "하나기술", "오태봉"), ("20261027000280", "044990", "에이치엔에스하이텍", "김정희"),
        ("20261027000260", "091970", "LSK아이로봇", "엘에스케이디비유한회사"), ("20261027000385", "090410", "덕신이피씨", "김명환")]]
    monkeypatch.setattr(pledge.requests, "get", FakeSession(items, 100).get)
    pledge.update("KEY")
    pledge.SUMMARY_PATH.write_text(json.dumps({"asof": "20261027", "stocks": [
        {"c": "299030", "n": "하나기술", "m": "KOSDAQ", "i": "기계", "px": 15000, "sh": 7994910, "cap": [1200.5], "h": "0"}]}), encoding="utf-8")
    pledge.FINANCIALS_PATH.write_text(json.dumps({"299030": {
        y: {"revenue": 1e11, "operating_profit": op, "net_income": 1e9, "liabilities": 5e10, "equity": 1e11}
        for y, op in (("2025", 5e9), ("2024", 4e9), ("2023", -1e9))}}), encoding="utf-8")
    pledge.EXT_PATH.write_text(json.dumps({"299030": {"op": 50.0, "da": 10.0, "nd": 120.0, "ocf": 40.0}}), encoding="utf-8")
    pledge.HISTORY_PATH.write_text(json.dumps([{"code": "299030", "date": f"202610{d:02d}", "trd_val_eok": float(d)} for d in range(1, 28)]), encoding="utf-8")
    out = pledge.build(today=date(2026, 10, 28))
    codes = [c["code"] for c in out["companies"]]
    assert codes == ["044990", "091970", "299030"]  # 덕신이피씨(계약 없음) 제외
    hana = next(c for c in out["companies"] if c["code"] == "299030")
    assert hana["px"] == 15000 and hana["cap"] == 1200.5 and hana["ind"] == "기계"
    assert hana["fin"]["op3"] is False and hana["fin"]["debt_ratio"] == 50.0
    assert hana["fin"]["nd_ebitda"] == 2.0 and hana["fin"]["ebitda_margin"] == 6.0
    assert hana["tv"] == pytest.approx(sum(range(8, 28)) / 20)
    assert hana["m"]["score"] == 4 and len(hana["rows"]) == 2
    lsk = next(c for c in out["companies"] if c["code"] == "091970")
    assert lsk["px"] is None and lsk["m"]["pledged_value"] is None
    assert set(lsk["excluded"]) == {"CB_COLLATERAL"}
    assert any(r["code"] == "091970" and "LEDGER_FORMAT" in r["reasons"] for r in out["review"])
    assert out["status"]["companies"] == 3


def test_max_docs_counts_downloads_only(tmp_paths, monkeypatch):
    """원문 상한은 실제로 받은 원문 수만 센다. 남은 건은 pending으로 남아 다음 실행(백필)이 이어 받는다."""
    items = [
        item("20261027000304", "299030", "하나기술", "오태봉", "20261027"),
        item("20261027000385", "090410", "덕신이피씨", "김명환", "20261027"),
        item("20261027000368", "069540", "빛과전자", "비엔에스조합", "20261027"),
    ]
    s = FakeSession(items, page_size=100)
    monkeypatch.setattr(pledge.requests, "get", s.get)
    pledge.update("KEY", max_docs=1)
    rep = json.loads(pledge.DOCS_PATH.read_text(encoding="utf-8"))["reports"]
    assert sorted(r["status"] for r in rep.values()) == ["parsed", "pending", "pending"]
    assert rep["20261027000385"]["status"] == "parsed"  # 접수번호가 가장 큰(최신) 것부터
    pledge.update("KEY")  # 다음 실행이 나머지를 이어서 받는다
    rep = json.loads(pledge.DOCS_PATH.read_text(encoding="utf-8"))["reports"]
    assert all(r["status"] == "parsed" for r in rep.values())


def test_max_minutes_stops_and_saves(tmp_paths, monkeypatch):
    items = [item("20261027000304", "299030", "하나기술", "오태봉", "20261027"),
             item("20261027000385", "090410", "덕신이피씨", "김명환", "20261027")]
    monkeypatch.setattr(pledge.requests, "get", FakeSession(items, page_size=100).get)
    clock = iter([0, 0, 10_000, 10_000, 10_000])
    monkeypatch.setattr(pledge.time, "monotonic", lambda: next(clock))
    pledge.update("KEY", max_minutes=1)
    store = json.loads(pledge.DOCS_PATH.read_text(encoding="utf-8"))
    assert sorted(r["status"] for r in store["reports"].values()) == ["parsed", "pending"]
    assert store["collected_through"]  # 목록 조회 진행분은 저장됨
