"""대출 단위 집계·반대매매 추정가·스트레스 스코어 검증."""
from datetime import date

import pytest

import pledge_metrics as M
import pledge_parse as P

RULES = P.load_rules()
A = RULES["assumptions"]


def test_hana_forced_price(doc):
    """하나기술: 30억×170%÷499,277주 ≈ 10,215원, 10억×200%÷83,753주 ≈ 23,880원 (±1원)."""
    facs = M.build_facilities(doc("20261027000304")["contracts"], A)
    prices = sorted(f["forced_price"] for f in facs)
    assert prices[0] == pytest.approx(10_215, abs=1)
    assert prices[1] == pytest.approx(23_880, abs=1)


def test_hns_single_facility(doc):
    """동일 기관·동일 만기 5건 -> Facility 1개. 540,399주/50억/5.70%/140%, 반대매매 추정가 ≈12,953원."""
    facs = M.build_facilities(doc("20261027000280")["contracts"], A)
    assert len(facs) == 1
    f = facs[0]
    assert (f["shares"], f["loan"], f["maint_ratio"]) == (540_399, 5_000_000_000, 140)
    assert f["rate"] == pytest.approx(5.70)
    assert f["forced_price"] == pytest.approx(12_953, abs=1)
    assert "ADDITIONAL_COLLATERAL" in f["flags"]  # 같은 Facility 내 계약일 상이


def test_row_mode(doc):
    facs = M.build_facilities(doc("20261027000280")["contracts"], A, mode="row")
    assert len(facs) == 5


def test_kep_default_assumption(doc):
    """폰드그룹 17행(하나은행 220억)은 담보유지비율 미기재 -> 기본 가정값 140% 적용 + 가정 적용 표시."""
    facs = M.build_facilities(doc("20261027000372")["contracts"], A)
    f = next(x for x in facs if x["lender"] == "하나은행")
    assert f["kep_assumed"] and f["maint_ratio_used"] == 140
    assert f["forced_price"] == pytest.approx(22_000_000_000 * 1.4 / 9_101_679)


def test_company_metrics_hana(doc):
    d = doc("20261027000304")
    m, facs = M.company_metrics([d], price=15_000, asof=date(2026, 10, 27), rules=RULES)
    assert m["total_loan"] == 4_000_000_000
    assert m["pledged_shares"] == 583_030
    assert m["pledge_ratio"] * 100 == pytest.approx(7.29, abs=0.01)
    assert m["pledge_to_holding"] == pytest.approx(583_030 / 2_343_884)
    assert m["wavg_rate"] == pytest.approx((30 * 6.30 + 10 * 6.50) / 40)
    assert m["nearest_maturity"] == "2026-10-30" and m["days_to_maturity"] == 3
    assert m["min_margin"] == pytest.approx(15_000 / (10e8 * 2 / 83_753) - 1)
    assert m["ltv"] == pytest.approx(4e9 / (583_030 * 15_000))
    assert "RATIO_MISMATCH" not in m["flags"]
    # 안전마진 <20%(3) + 만기 ≤30일(3) + 보유 대비 24.9%(0) + 증권사(0) + 3개월 롤오버(1)
    assert m["score_parts"] == {"safety_margin": 3, "maturity": 3, "pledge_to_holding": 0, "lender": 0,
                                "short_rollover": 1, "additional_collateral": 0}
    assert m["score"] == 7


def test_company_metrics_no_price(doc):
    m, _ = M.company_metrics([doc("20261027000275")], price=None, asof=date(2026, 10, 27), rules=RULES)
    assert m["min_margin"] is None and m["ltv"] is None
    assert m["score_parts"]["safety_margin"] == 0


def test_stale_and_passed(doc):
    m, _ = M.company_metrics([doc("20261027000304")], price=20_000, asof=date(2027, 12, 1), rules=RULES)
    assert "STALE_REPORT" in m["flags"] and "MATURITY_PASSED" in m["flags"]
    assert m["score_parts"]["maturity"] == 3


def test_no_pledge_company(doc):
    m, facs = M.company_metrics([doc("20261027000385")], price=5_000, asof=date(2026, 10, 27), rules=RULES)
    assert facs == [] and m["total_loan"] is None and m["score"] == 0
