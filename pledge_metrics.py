"""
주담대 대출 단위 집계(Facility)·지표·스트레스 스코어
=====================================================
pledge_parse.parse_document() 결과를 받아 화면용 수치를 만든다. 화면(index.html)의 가정 패널은
같은 산식을 자바스크립트로 다시 계산하므로, 산식을 바꾸면 index.html의 pledgeCompute()도 함께 바꾼다.

Facility 키 = (담보제공자, 대출기관, 만기일). 공시상 추가담보와 별개 대출이 구분되지 않으므로
같은 키의 담보주식 수·대출금액을 합산한다(aggregate_mode='row'면 행 단위 그대로).

PEF 투자(지분 일부 인수 + 대환) 관점이므로 주가 기반 반대매매·안전마진·LTV는 계산하지 않는다.
담보유지비율은 공시 기재값을 그대로 보여주기만 한다.

산식
  담보지분율     = Σ담보주식 ÷ 의결권 있는 발행주식 총수(보고서 기재값)
  보유 대비 담보 = Σ담보주식 ÷ 보고자+특별관계자 보유주식(보고서 '이번 보고서' 보유주식등의 수)
  가중평균 금리  = Σ(대출액×금리) ÷ Σ대출액 (금리 기재 행만)
"""
from datetime import date

import pledge_parse


def norm(name):
    return pledge_parse.norm_name(name)


def pledged_rows(contracts):
    """스크리닝 포함 유형(주담대·금전소비대차) 중 현재 유효하고 완전 중복이 아닌 행."""
    return [c for c in contracts if c["included"] and c["active"] and c.get("dup") != "exact"]


def build_facilities(contracts, assumptions, mode=None):
    mode = mode or assumptions.get("aggregate_mode", "facility")
    short_days = assumptions.get("short_rollover_days", 92)
    groups = {}
    for i, c in enumerate(pledged_rows(contracts)):
        key = (norm(c["provider"]), norm(c["lender"]), c["maturity"]) if mode == "facility" else (i,)
        groups.setdefault(key, []).append(c)

    facilities = []
    for rows in groups.values():
        first = rows[0]
        shares = sum(abs(r["shares"] or 0) for r in rows)
        loans = [r["loan"] for r in rows if r["loan"] is not None]
        loan = sum(loans) if loans else None
        rated = [(r["loan"], r["rate"]) for r in rows if r["loan"] and r["rate"] is not None]
        rate = sum(l * x for l, x in rated) / sum(l for l, _ in rated) if rated else None
        keps = [r["maint_ratio"] for r in rows if r["maint_ratio"]]
        kep = max(keps) if keps else None
        flags = sorted({f for r in rows for f in r["flags"]})
        if len(set(keps)) > 1:
            flags.append("KEP_VARIES")
        if len(loans) < len(rows):
            flags.append("PARTIAL_LOAN")
        if len({r["contract_date"] for r in rows if r["contract_date"]}) > 1:
            flags.append("ADDITIONAL_COLLATERAL")
        short = False
        for r in rows:
            if r["start_date"] and r["maturity"]:
                if (date.fromisoformat(r["maturity"]) - date.fromisoformat(r["start_date"])).days <= short_days:
                    short = True
        if short:
            flags.append("SHORT_ROLLOVER")
        facilities.append({
            "provider": first["provider"], "provider_type": first["provider_type"], "rel": first["rel"],
            "lender": first["lender"], "lender_type": first["lender_type"], "lender_score": first["lender_score"],
            "maturity": first["maturity"], "contract_type": first["contract_type"],
            "seqs": [r["seq"] for r in rows], "contract_dates": sorted({r["contract_date"] for r in rows if r["contract_date"]}),
            "shares": shares, "loan": loan, "rate": rate,
            "maint_ratio": kep,
            "debtor": first["debtor"], "flags": flags,
        })
    facilities.sort(key=lambda f: (f["maturity"] or "9999", -(f["loan"] or 0)))
    return facilities


def band_score(value, bands):
    if value is None:
        return 0
    for b in bands:
        if ("lt" in b and value < b["lt"]) or ("lte" in b and value <= b["lte"]) \
                or ("gt" in b and value > b["gt"]) or ("gte" in b and value >= b["gte"]):
            return b["score"]
    return 0


def stress_score(m, facilities, rules):
    """우선순위 정렬용 참고지표(투자판단 기준 아님). 항목별 점수를 함께 돌려준다."""
    cfg = rules["stress_score"]
    days = m["days_to_maturity"]
    parts = {
        # 만기가 이미 지났는데 후속 보고가 없으면(연장 여부 불명) 가장 높은 구간 점수
        "maturity": cfg["maturity_days"][0]["score"] if days is not None and days < 0 else band_score(days, cfg["maturity_days"]),
        "pledge_to_holding": band_score(m["pledge_to_holding"], cfg["pledge_to_holding"]),
        "lender": max((f["lender_score"] for f in facilities), default=0),
        "short_rollover": cfg["short_rollover"] if any("SHORT_ROLLOVER" in f["flags"] for f in facilities) else 0,
        "additional_collateral": cfg["additional_collateral"] if any("ADDITIONAL_COLLATERAL" in f["flags"] for f in facilities) else 0,
    }
    return sum(parts.values()), parts


def company_metrics(docs, price, asof, rules, assumptions=None, mode=None):
    """같은 회사의 현재 스냅샷 보고서들(보고자별 최신 1건) -> 회사 단위 지표.
    price: 현재가(원, 없으면 None), asof: 주가 기준일(date)."""
    a = dict(rules["assumptions"], **(assumptions or {}))
    contracts = [c for d in docs for c in d["contracts"]]
    facilities = build_facilities(contracts, a, mode)
    reports = [d["report"] for d in docs]
    latest = max(reports, key=lambda r: r["report_date"] or "")
    flags = sorted({f for d in docs for f in d["flags"]})

    pledged = sum(f["shares"] for f in facilities)
    loans = [f["loan"] for f in facilities if f["loan"] is not None]
    total_loan = sum(loans) if loans else None
    voting = latest["voting_shares"]
    holding = max((r["holding_cnt"] or 0 for r in reports), default=0) or None
    rated = [(f["loan"], f["rate"]) for f in facilities if f["loan"] and f["rate"] is not None]

    m = {
        "facility_count": len(facilities),
        "pledged_shares": pledged,
        "total_loan": total_loan,
        "pledge_ratio": pledged / voting if voting and pledged else None,
        "pledge_to_holding": pledged / holding if holding and pledged else None,
        "holding_rt": latest["holding_rt"],
        "wavg_rate": sum(l * x for l, x in rated) / sum(l for l, _ in rated) if rated else None,
        "price": price,
        "asof": asof.isoformat() if asof else None,
    }

    # 담보지분율 교차검증: 계약 행이 모두 스크리닝 포함 유형일 때만 SUM_TRD_RT와 비교할 수 있다
    all_included = all(c["included"] or not c["active"] for c in contracts)
    sum_rt = sum(r["sum_trd_rt"] or 0 for r in reports)
    if all_included and m["pledge_ratio"] is not None and sum_rt and \
            abs(m["pledge_ratio"] * 100 - sum_rt) > a["ratio_check_tolerance_pctp"]:
        flags.append("RATIO_MISMATCH")

    # 만기: 기준일 이후 가장 가까운 만기. 모두 지났으면 가장 최근 만기(음수 D-)와 함께 플래그
    mats = sorted({f["maturity"] for f in facilities if f["maturity"]})
    ref = asof or date.today()
    future = [x for x in mats if x >= ref.isoformat()]
    nearest = future[0] if future else (mats[-1] if mats else None)
    if mats and not future:
        flags.append("MATURITY_PASSED")
    m["nearest_maturity"] = nearest
    m["days_to_maturity"] = (date.fromisoformat(nearest) - ref).days if nearest else None
    m["maturity_within_30"] = sum(1 for x in future if (date.fromisoformat(x) - ref).days <= 30)

    m["pledged_value"] = pledged * price if pledged and price else None  # 담보주식 시가(참고: 지분 인수 규모 가늠용)

    m["latest_report_date"] = latest["report_date"]
    m["report_age_days"] = (ref - date.fromisoformat(latest["report_date"])).days if latest["report_date"] else None
    if m["report_age_days"] is not None and m["report_age_days"] > a["stale_report_days"]:
        flags.append("STALE_REPORT")
    m["control_buffer_pctp"] = latest["holding_rt"] - a["control_sale_pctp"] if latest["holding_rt"] is not None else None

    for f in facilities:
        flags.extend(x for x in f["flags"] if x in ("THIRD_PARTY_DEBT", "HIGH_RISK_CONTRACT", "PARTNERSHIP_PROVIDER", "DUP_SUSPECT"))
    m["score"], m["score_parts"] = stress_score(m, facilities, rules)
    m["flags"] = sorted(set(flags))
    return m, facilities
