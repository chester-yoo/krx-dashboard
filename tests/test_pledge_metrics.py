"""대출 단위 집계·회사 지표·스트레스 스코어 검증."""
from datetime import date

import pytest

import pledge_metrics as M
import pledge_parse as P

RULES = P.load_rules()
A = RULES["assumptions"]


def test_hana_two_facilities(doc):
    """하나기술: 기관이 달라 Facility 2개(키움 499,277주/30억/170%, 한투 83,753주/10억/200%)."""
    facs = M.build_facilities(doc("20261027000304")["contracts"], A)
    got = sorted((f["shares"], f["loan"], f["maint_ratio"]) for f in facs)
    assert got == [(83_753, 1_000_000_000, 200), (499_277, 3_000_000_000, 170)]
    assert all("forced_price" not in f for f in facs)  # PEF 관점: 반대매매가 미산출


def test_hns_single_facility(doc):
    """동일 기관·동일 만기 5건 -> Facility 1개. 540,399주/50억/5.70%/140%."""
    facs = M.build_facilities(doc("20261027000280")["contracts"], A)
    assert len(facs) == 1
    f = facs[0]
    assert (f["shares"], f["loan"], f["maint_ratio"]) == (540_399, 5_000_000_000, 140)
    assert f["rate"] == pytest.approx(5.70)
    assert "ADDITIONAL_COLLATERAL" in f["flags"]  # 같은 Facility 내 계약일 상이


def test_row_mode(doc):
    facs = M.build_facilities(doc("20261027000280")["contracts"], A, mode="row")
    assert len(facs) == 5


def test_kep_missing_kept_as_none(doc):
    """폰드그룹 17행(하나은행 220억)은 담보유지비율 미기재 -> 가정값을 넣지 않고 그대로 비워 둔다."""
    facs = M.build_facilities(doc("20261027000372")["contracts"], A)
    f = next(x for x in facs if x["lender"] == "하나은행")
    assert f["maint_ratio"] is None and f["loan"] == 22_000_000_000


def test_company_metrics_hana(doc):
    d = doc("20261027000304")
    m, facs = M.company_metrics([d], price=15_000, asof=date(2026, 10, 27), rules=RULES)
    assert m["total_loan"] == 4_000_000_000
    assert m["pledged_shares"] == 583_030
    assert m["pledge_ratio"] * 100 == pytest.approx(7.29, abs=0.01)
    assert m["pledge_to_holding"] == pytest.approx(583_030 / 2_343_884)
    assert m["wavg_rate"] == pytest.approx((30 * 6.30 + 10 * 6.50) / 40)
    assert m["nearest_maturity"] == "2026-10-30" and m["days_to_maturity"] == 3
    assert m["pledged_value"] == 583_030 * 15_000
    assert "min_margin" not in m and "ltv" not in m
    assert "RATIO_MISMATCH" not in m["flags"]
    # 만기 ≤30일(3) + 보유 대비 24.9%(0) + 증권사(0) + 3개월 롤오버(1)
    assert m["score_parts"] == {"maturity": 3, "pledge_to_holding": 0, "lender": 0,
                                "short_rollover": 1, "additional_collateral": 0}
    assert m["score"] == 4


def test_company_metrics_no_price(doc):
    m, _ = M.company_metrics([doc("20261027000275")], price=None, asof=date(2026, 10, 27), rules=RULES)
    assert m["pledged_value"] is None
    assert m["total_loan"] == 800_000_000


def test_stale_and_passed(doc):
    m, _ = M.company_metrics([doc("20261027000304")], price=20_000, asof=date(2027, 12, 1), rules=RULES)
    assert "STALE_REPORT" in m["flags"] and "MATURITY_PASSED" in m["flags"]
    assert m["score_parts"]["maturity"] == 3


def test_no_pledge_company(doc):
    m, facs = M.company_metrics([doc("20261027000385")], price=5_000, asof=date(2026, 10, 27), rules=RULES)
    assert facs == [] and m["total_loan"] is None and m["score"] == 0
