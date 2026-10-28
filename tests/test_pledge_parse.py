"""대량보유상황보고서 원문 fixture(tests/fixtures/{rcept_no}.xml) 기반 파서 검증."""
from datetime import date

import pytest

import pledge_parse as P

ALL = ["20261027000385", "20261027000304", "20261026000065", "20261027000280", "20261027000275",
       "20261027000062", "20261027000372", "20261028000466", "20261027000368", "20261027000260"]


def pledges(d):
    return [c for c in d["contracts"] if c["included"] and c["active"]]


@pytest.mark.parametrize("rcept_no", ALL)
def test_sum_stk_cnt_matches(doc, rcept_no):
    """파서 결과 Σ주식 수가 원문 합계(SUM_STK_CNT)와 일치(누적 변동표 형식은 유효 담보성 행의 절대값 합)."""
    d = doc(rcept_no)
    assert "SUM_MISMATCH" not in d["flags"]
    if d["report"]["sum_stk_cnt"] is not None:
        assert d["report"]["computed_stk_cnt"] == d["report"]["sum_stk_cnt"]


def test_no_contract_dukshin(doc):
    d = doc("20261027000385")
    assert d["report"]["trd_rvq_sel"] == "N"
    assert d["contracts"] == []
    assert d["report"]["sum_trd_rt"] is None
    assert not d["report"]["has_pledge"]


def test_hana_tech(doc):
    d = doc("20261027000304")
    r = d["report"]
    assert (r["stock_code"], r["voting_shares"], r["holding_cnt"], r["sum_trd_rt"]) == ("299030", 7994910, 2343884, 7.29)
    rows = pledges(d)
    assert [(c["rel"], c["shares"], c["loan"], c["rate"], c["maint_ratio"]) for c in rows] == [
        ("M", 499277, 3_000_000_000, 6.30, 170), ("M", 83753, 1_000_000_000, 6.50, 200)]
    assert all(c["contract_type"] == "PLEDGE_LOAN" for c in rows)
    assert rows[0]["contract_date"] == "2026-10-22" and rows[0]["maturity"] == "2027-01-22"


def test_daesung_seq_gap_join(doc):
    """SEQ_NO2 비연속(2·3=공탁, 9=CB 매수선택권) 조인.
    지침서 기대값(주담대 6건·967,260주·20억)은 1~8행만 본 값이다. 실제 원문에는 2026.10.22 신규 주담대 2건(10·11행)이
    더 있어 전체 파싱 기준 8건·1,288,977주·32억이 맞다. 1~8행 부분집합이 지침서 값과 일치하는지도 함께 검증한다."""
    d = doc("20261026000065")
    assert len(d["contracts"]) == 11
    types = {c["seq"]: c["contract_type"] for c in d["contracts"]}
    assert types[2] == types[3] == "TAX_DEPOSIT"
    assert types[9] == "CALL_OPTION"
    rows = pledges(d)
    first8 = [c for c in rows if c["seq"] <= 8]
    assert [c["seq"] for c in first8] == [1, 4, 5, 6, 7, 8]
    assert sum(c["shares"] for c in first8) == 967_260
    assert sum(c["loan"] for c in first8) == 2_000_000_000
    assert len(rows) == 8
    assert sum(c["shares"] for c in rows) == 1_288_977
    assert sum(c["loan"] for c in rows) == 3_200_000_000
    assert all(c["sec_join"] == "seq" for c in rows)
    assert {c["provider"]: c["rel"] for c in rows} == {"최호형": "M", "최호익": "S"}


def test_hns_hitech(doc):
    d = doc("20261027000280")
    rows = pledges(d)
    assert len(rows) == 5
    assert {c["maturity"] for c in rows} == {"2026-11-11"}  # '2025.11.12-2026.11.11' 형식
    assert {c["lender"] for c in rows} == {"NH투자증권"} and {c["sec_etc"] for c in rows} == {"NH투자증권"}
    assert sum(c["shares"] for c in rows) == 540_399
    assert sum(c["loan"] for c in rows) == 5_000_000_000


def test_ninetech_korean_dates(doc):
    d = doc("20261027000275")
    (c,) = pledges(d)
    assert c["contract_kind"] == "증권담보" and c["contract_type"] == "PLEDGE_LOAN"
    assert (c["start_date"], c["maturity"]) == ("2023-04-24", "2027-01-18")
    assert (c["shares"], c["loan"], c["maint_ratio"]) == (553_252, 800_000_000, 200)


def test_duol_related_party(doc):
    d = doc("20261027000062")
    (c,) = pledges(d)
    assert c["rel"] == "S" and c["provider"] == "정재열"
    assert (c["shares"], c["loan"], c["rate"], c["maint_ratio"]) == (58_651, 99_998_200, 8.0, 168.89)
    assert d["report"]["reporter_type"] == "CORPORATE"


def test_pond_group_many_rows(doc):
    d = doc("20261027000372")
    rows = pledges(d)
    assert len(rows) == 21  # 8행 초과 전체 파싱
    assert d["report"]["sum_trd_rt"] == 60.55
    assert d["report"]["reporter_type"] == "CORPORATE"
    assert all(c["contract_kind"] == "일반대출" and c["contract_type"] == "PLEDGE_LOAN" for c in rows)
    assert rows[0]["loan"] == 7_236_000_000
    assert all(c["rate"] is None for c in rows)  # 이자율 미기재
    assert next(c for c in rows if c["seq"] == 17)["maint_ratio"] is None
    assert next(c for c in rows if c["seq"] == 6)["lender_type"] == "SPC·대부"
    assert rows[0]["lender_type"] == "증권금융"


def test_sec_moy_whitespace():
    assert P.to_int(" 7,236,000,000 ") == 7_236_000_000
    assert P.to_int(P.clean_text("\n  1,500,000,000\n")) == 1_500_000_000


def test_blue_industrial(doc):
    """채무자=(주)파일엔지니어링(담보제공자 본인 법인), 보고자=케이비비조합.
    지침서는 '채무자=타 법인 → 타인 채무 담보'로 기대했으나, 채무자가 담보제공자 자신이므로 타인 채무가 아니다.
    대신 법인 담보제공자 + 조합 보고자(REPORTER_PARTNERSHIP) 플래그로 위험을 표시한다."""
    d = doc("20261028000466")
    (c,) = pledges(d)
    assert d["report"]["sum_trd_rt"] == 3.32
    assert c["maint_ratio"] == 131
    assert c["provider_type"] == "CORPORATE"
    assert c["debtor"] == "(주)파일엔지니어링"
    assert "THIRD_PARTY_DEBT" not in c["flags"]
    assert "REPORTER_PARTNERSHIP" in d["flags"]


def test_bitnara_partnership_private_loan(doc):
    d = doc("20261027000368")
    rows = pledges(d)
    assert d["report"]["reporter_type"] == "PARTNERSHIP"
    assert all(c["contract_type"] == "PRIVATE_LOAN" and "HIGH_RISK_CONTRACT" in c["flags"] for c in rows)
    assert all(c["provider_type"] == "PARTNERSHIP" for c in rows)
    suspects = [c["seq"] for c in rows if c["dup"] == "suspect"]
    assert suspects == [1, 2]  # 계약일만 하루 차이로 반복 기재 -> 검토 큐
    assert "DUP_SUSPECT" in d["flags"]
    assert rows[0]["lender_type"] == "법인·조합(사채)"


def test_lsk_ledger(doc):
    """CB 담보는 제외. 누적 변동표 형식(음수 주식 수·해지 행)이라 해지된 담보를 상쇄하고,
    SEC_CONT 연번(1~3)이 계약 행 연번과 맞지 않아 주식 수·상대방 기준으로 조인한다."""
    d = doc("20261027000260")
    assert "LEDGER_FORMAT" in d["flags"]
    cb = [c for c in d["contracts"] if c["contract_type"] == "CB_COLLATERAL"]
    assert cb and not any(c["included"] for c in cb)
    assert [c["seq"] for c in cb if c["active"]] == [40]
    rows = pledges(d)
    assert [c["seq"] for c in rows] == [42, 43, 45, 46]
    s45 = next(c for c in rows if c["seq"] == 45)
    assert (s45["loan"], s45["rate"], s45["maint_ratio"], s45["lender_type"]) == (3_000_000_000, 13.0, 160, "저축은행")
    wonys = [c for c in rows if c["lender"] == "원영식"]
    assert sum(c["loan"] for c in wonys) == 1_000_000_000  # 1건(575,059주/10억)을 주식 수 비율로 배분
    assert all(c["lender_type"] == "개인(사채)" for c in wonys)
    assert "THIRD_PARTY_DEBT" in next(c for c in rows if c["seq"] == 46)["flags"]  # 법인 주식으로 이상규 채무 담보
    assert d["sec_unmatched"] == []


@pytest.mark.parametrize("text,expected", [
    ("2026.10.22~2027.01.22", (date(2026, 10, 22), date(2027, 1, 22))),
    ("2025.11.12-2026.11.11", (date(2025, 11, 12), date(2026, 11, 11))),
    ("2023년 04월 24일 ~ 2027년 01월 18일", (date(2023, 4, 24), date(2027, 1, 18))),
    ("2026.10.22∼2027.01.22", (date(2026, 10, 22), date(2027, 1, 22))),
    ("2027.01.22까지", (None, date(2027, 1, 22))),
    ("공탁 해지시까지", (None, None)),
    (None, (None, None)),
])
def test_parse_period(text, expected):
    assert P.parse_period(text) == expected


@pytest.mark.parametrize("kind,remark,expected", [
    ("담보계약", "주식담보대출", "PLEDGE_LOAN"),
    ("일반대출", "주식담보", "PLEDGE_LOAN"),
    ("증권담보", "주1) 주식담보", "PLEDGE_LOAN"),
    ("공탁", "증여세 연부연납 납세담보", "TAX_DEPOSIT"),
    ("9회차 전환사채 담보계약 체결", None, "CB_COLLATERAL"),
    ("담보계약", "전환사채 담보", "CB_COLLATERAL"),
    ("금전소비대차", "금전소비대차", "PRIVATE_LOAN"),
    ("주식대차거래계약", None, "LENDING"),
    ("주식담보제공 해지", None, "TERMINATION"),
    ("전환사채매수선택권행사자 지정", None, "CALL_OPTION"),
    ("의결권 공동행사 약정", None, "OTHER"),
])
def test_classify_contract(kind, remark, expected):
    assert P.classify_contract(kind, remark)["type"] == expected


@pytest.mark.parametrize("name,pid,expected", [
    ("오태봉", "660307", "INDIVIDUAL"),
    ("(주)대명화학", "104-81-51381", "CORPORATE"),
    ("비엔에스조합", "529-80-03344", "PARTNERSHIP"),
    ("엘에스케이디비유한회사", "250-81-00865", "SPV"),
])
def test_classify_provider(name, pid, expected):
    assert P.classify_provider(name, pid) == expected


def test_blank_and_encoding_tolerance():
    xml = ('<TABLE-GROUP ACLASS="TRD_RVL"><TR><TE ACODE="SEQ_NO2">1</TE><TE ACODE="SPC_NM">A &amp; B</TE>'
           '<TE ACODE="STK_CNT"> 1,000 </TE><TE ACODE="TRD_KND">담보계약</TE><TE ACODE="TRD_RMK">-</TE></TR>'
           '<TR><TE ACODE="SEQ_NO2">-</TE><TE ACODE="SPC_NM">-</TE></TR></TABLE-GROUP>')
    d = P.parse_document(xml)
    assert len(d["contracts"]) == 1
    assert d["contracts"][0]["provider"] == "A & B" and d["contracts"][0]["shares"] == 1000
