"""
Money in, money out.

Definitions (also shown in the app under "Data notes"):
  Written premium  - annual premium of policies that START in the period (sales).
  Earned premium   - the part of each annual premium that covers days inside the period
                     (premium x days covered / 365). This is the fair yardstick for claims.
  Claims paid      - money paid on claims whose loss happened in the period.
  Open claims      - claims not settled yet, estimated at claim amount minus deductible.
  Incurred claims  - paid + open.
  Loss ratio       - incurred / earned premium.
  Commission       - broker commission rate x earned premium sold by brokers (assumption).
  Operating costs  - operating cost rate x earned premium (assumption).
  Result           - earned premium - incurred - commission - operating costs.
  Combined ratio   - (incurred + commission + operating costs) / earned premium. Above 100% = losing money.
"""
from __future__ import annotations

import numpy as np
import pandas as pd

DAY = np.timedelta64(1, "D")

POLICY_FILTERS = ["city", "policy_type", "risk_band", "channel", "source", "deductible", "age_band", "renewal_status"]
CLAIM_FILTERS = ["claim_type", "adjuster_team", "status"]

DIMENSIONS = {
    "city": ("City", ["city"], "policy"),
    "policy_type": ("Product", ["policy_type"], "policy"),
    "city_product": ("City and product", ["city", "policy_type"], "policy"),
    "risk_band": ("Risk band", ["risk_band"], "policy"),
    "product_risk": ("Product and risk band", ["policy_type", "risk_band"], "policy"),
    "channel": ("Channel", ["channel"], "policy"),
    "source": ("Broker or direct channel", ["source"], "policy"),
    "source_product": ("Broker or direct channel, by product", ["source", "policy_type"], "policy"),
    "deductible": ("Deductible", ["deductible"], "policy"),
    "age_band": ("Customer age", ["age_band"], "policy"),
    "renewal_status": ("Renewal status", ["renewal_status"], "policy"),
    "claim_type": ("Claim type", ["claim_type"], "claim"),
    "adjuster_team": ("Adjuster team", ["adjuster_team"], "claim"),
}

ORDER = {
    "risk_band": ["Low", "Medium", "High"],
    "age_band": ["Under 25", "25–34", "35–49", "50–64", "65+", "Unknown"],
    "renewal_status": ["Renewed", "Not Renewed", "In Force", "Cancelled", "Unknown"],
    "status": ["Paid", "Denied", "Open", "Under Review"],
}


# ----------------------------------------------------------------- periods
def quarter_of(ts: pd.Timestamp) -> str:
    return f"{ts.year}Q{(ts.month - 1) // 3 + 1}"


def quarter_bounds(q: str):
    p = pd.Period(q, freq="Q")
    return p.start_time.normalize(), (p.end_time.normalize() + pd.Timedelta(days=1))


def quarter_label(q: str, asof: pd.Timestamp) -> str:
    start, end = quarter_bounds(q)
    label = f"{q[:4]} {q[4:]}"
    if end > asof + pd.Timedelta(days=1) and start <= asof:
        label += f" (to {asof.strftime('%b')} {asof.day})"
    return label


def quarters_between(first: pd.Timestamp, last: pd.Timestamp) -> list:
    return [str(p) for p in pd.period_range(first, last, freq="Q")]


def window(period_from: str | None, period_to: str | None, data_start: pd.Timestamp, asof: pd.Timestamp):
    """[a, b) date window for a quarter range, capped by the data."""
    a = quarter_bounds(period_from)[0] if period_from else data_start
    b = quarter_bounds(period_to)[1] if period_to else asof + pd.Timedelta(days=1)
    a = max(a, data_start)
    b = min(b, asof + pd.Timedelta(days=1))
    return a, b


# --------------------------------------------------------------- filtering
def apply_filters(policies: pd.DataFrame, claims: pd.DataFrame, filters: dict):
    pmask = pd.Series(True, index=policies.index)
    cmask = pd.Series(True, index=claims.index)
    for col in POLICY_FILTERS:
        values = filters.get(col) or []
        if values:
            if col == "deductible":
                values = [float(v) for v in values]
            pmask &= policies[col].isin(values)
            cmask &= claims[col].isin(values)
    for col in CLAIM_FILTERS:
        values = filters.get(col) or []
        if values:
            cmask &= claims[col].isin(values)
    return policies[pmask], claims[cmask]


# ---------------------------------------------------------- earned premium
def _days(ts) -> np.ndarray:
    return np.asarray(ts, dtype="datetime64[D]").astype(np.int64)


def earned(policies: pd.DataFrame, a: pd.Timestamp, b: pd.Timestamp):
    """Earned premium and exposure (policy-years) of each policy inside [a, b)."""
    lo = np.maximum(_days(policies["start"]), _days([a])[0])
    hi = np.minimum(_days(policies["end"]), _days([b])[0])
    covered = np.clip(hi - lo, 0, None)
    return policies["premium"].to_numpy() * covered / 365.0, covered / 365.0


# ------------------------------------------------------------- aggregation
def segment_table(pol, clm, keys, a, b, assumptions, cap=None) -> pd.DataFrame:
    """Profit and loss for every segment defined by `keys` inside [a, b)."""
    e, x = earned(pol, a, b)
    in_window = ((pol["start"] >= a) & (pol["start"] < b)).to_numpy()
    P = pol.assign(
        _earned=e, _expo=x,
        _earned_broker=np.where(pol["channel"].to_numpy() == "Broker", e, 0.0),
        _written=np.where(in_window, pol["premium"].to_numpy(), 0.0),
        _new=in_window.astype(int),
        _inforce=(x > 0).astype(int),
    )
    C = clm[(clm["loss_date"] >= a) & (clm["loss_date"] < b)]
    C = C.assign(
        _capped=C["incurred"].clip(upper=cap) if cap else C["incurred"],
        _band=C["near_threshold"].astype(int),
        _early=(C["days_from_start"] <= 60).astype(int),
        _sized=C["claim_amount"].notna().astype(int),
        _large=(C["incurred"] > cap).astype(int) if cap else 0,
    )
    gp = P.groupby(keys, dropna=False).agg(
        earned=("_earned", "sum"), exposure=("_expo", "sum"), earned_broker=("_earned_broker", "sum"),
        written=("_written", "sum"), policies_written=("_new", "sum"), policies_in_force=("_inforce", "sum"),
    )
    gc = C.groupby(keys, dropna=False).agg(
        claims=("claim_id", "count"), paid=("paid", "sum"), reserve=("reserve", "sum"),
        incurred=("incurred", "sum"), capped=("_capped", "sum"), band=("_band", "sum"),
        early=("_early", "sum"), sized=("_sized", "sum"), large=("_large", "sum"),
        settle_days=("days_to_settle", "median"),
    )
    t = gp.join(gc, how="outer")
    count_cols = [c for c in t.columns if c != "settle_days"]
    t[count_cols] = t[count_cols].fillna(0)
    return derive(t, assumptions)


def derive(t: pd.DataFrame, assumptions: dict) -> pd.DataFrame:
    t = t.copy()
    earned_ = t["earned"].replace(0, np.nan)
    t["commission"] = t["earned_broker"] * assumptions["broker_commission"]
    t["expenses"] = t["earned"] * assumptions["expense_ratio"]
    t["result"] = t["earned"] - t["incurred"] - t["commission"] - t["expenses"]
    t["loss_ratio"] = t["incurred"] / earned_
    t["loss_ratio_capped"] = t["capped"] / earned_
    t["combined_ratio"] = (t["incurred"] + t["commission"] + t["expenses"]) / earned_
    t["combined_ratio_capped"] = (t["capped"] + t["commission"] + t["expenses"]) / earned_
    t["frequency"] = t["claims"] / t["exposure"].replace(0, np.nan)
    t["severity"] = t["incurred"] / t["claims"].replace(0, np.nan)
    t["band_share"] = t["band"] / t["sized"].replace(0, np.nan)
    t["early_share"] = t["early"] / t["claims"].replace(0, np.nan)
    # Premium that would be needed to break even with the same claims and cost rates
    broker_mix = (t["earned_broker"] / earned_).fillna(0)
    margin = 1 - assumptions["expense_ratio"] - assumptions["broker_commission"] * broker_mix
    t["breakeven_change"] = t["capped"] / margin / earned_ - 1
    return t


def total_row(pol, clm, a, b, assumptions, cap=None) -> dict:
    t = segment_table(pol.assign(_all="all"), clm.assign(_all="all"), ["_all"], a, b, assumptions, cap)
    return clean_record(t.iloc[0].to_dict()) if len(t) else {}


def clean_record(rec: dict) -> dict:
    out = {}
    for k, v in rec.items():
        if isinstance(v, (np.floating, float)):
            out[k] = None if (np.isnan(v) or np.isinf(v)) else float(round(v, 6))
        elif isinstance(v, (np.integer,)):
            out[k] = int(v)
        elif isinstance(v, pd.Timestamp):
            out[k] = v.strftime("%Y-%m-%d")
        else:
            out[k] = v
    return out


def label_of(keys: list, values) -> str:
    if not isinstance(values, tuple):
        values = (values,)
    parts = []
    for k, v in zip(keys, values):
        parts.append(f"${v:,.0f} deductible" if k == "deductible" else str(v))
    return " / ".join(parts)


def sort_key(col: str, value):
    order = ORDER.get(col)
    if order and value in order:
        return (0, order.index(value), "")
    if col == "source":
        return (0 if value in ("Agent", "Online") else 1, 0, str(value))
    if isinstance(value, (int, float, np.floating)):
        return (0, float(value), "")
    return (0, 0, str(value))


def table_records(t: pd.DataFrame, keys: list) -> list:
    rows = []
    for idx, rec in t.iterrows():
        values = idx if isinstance(idx, tuple) else (idx,)
        r = clean_record(rec.to_dict())
        r["key"] = {k: (float(v) if k == "deductible" else v) for k, v in zip(keys, values)}
        r["label"] = label_of(keys, values)
        rows.append(r)
    return rows


# ------------------------------------------------------------------- ledger
def monthly_series(pol, clm, a, b) -> list:
    months = pd.period_range(a, b - pd.Timedelta(days=1), freq="M")
    if len(months) == 0:
        return []
    m_start = np.array([_days([m.start_time])[0] for m in months])
    m_end = np.array([_days([m.end_time.normalize() + pd.Timedelta(days=1)])[0] for m in months])
    m_start = np.maximum(m_start, _days([a])[0])
    m_end = np.minimum(m_end, _days([b])[0])
    s = _days(pol["start"])[:, None]
    e = _days(pol["end"])[:, None]
    covered = np.clip(np.minimum(e, m_end[None, :]) - np.maximum(s, m_start[None, :]), 0, None)
    earned_m = (pol["premium"].to_numpy()[:, None] * covered / 365.0).sum(axis=0)
    written_m = pol.groupby(pol["start"].dt.to_period("M"))["premium"].sum()
    C = clm[(clm["loss_date"] >= a) & (clm["loss_date"] < b)]
    cm = C.groupby(C["loss_date"].dt.to_period("M")).agg(paid=("paid", "sum"), reserve=("reserve", "sum"),
                                                          claims=("claim_id", "count"))
    out = []
    for i, m in enumerate(months):
        paid = float(cm["paid"].get(m, 0.0)) if len(cm) else 0.0
        reserve = float(cm["reserve"].get(m, 0.0)) if len(cm) else 0.0
        out.append({
            "month": str(m), "earned": round(float(earned_m[i]), 2), "written": round(float(written_m.get(m, 0.0)), 2),
            "paid": round(paid, 2), "reserve": round(reserve, 2),
            "claims": int(cm["claims"].get(m, 0)) if len(cm) else 0,
            "loss_ratio": (paid + reserve) / earned_m[i] if earned_m[i] > 0 else None,
        })
    return out


def breakdown(pol, clm, dim, a, b, assumptions, total_earned) -> dict:
    label, keys, scope = DIMENSIONS.get(dim, DIMENSIONS["city"])
    if scope == "claim":
        C = clm[(clm["loss_date"] >= a) & (clm["loss_date"] < b)]
        g = C.groupby(keys).agg(claims=("claim_id", "count"), paid=("paid", "sum"), reserve=("reserve", "sum"),
                                incurred=("incurred", "sum"), settle_days=("days_to_settle", "median"))
        tot = g["incurred"].sum()
        g["incurred_share"] = g["incurred"] / tot if tot else np.nan
        g["loss_ratio_points"] = g["incurred"] / total_earned if total_earned else np.nan
        rows = table_records(g, keys)
    else:
        t = segment_table(pol, clm, keys, a, b, assumptions)
        rows = table_records(t, keys)
    rows.sort(key=lambda r: tuple(sort_key(k, r["key"][k]) for k in keys))
    return {"dim": dim, "label": label, "keys": keys, "scope": scope, "rows": rows}


def heatmap(pol, clm, rows_dim, cols_dim, a, b, assumptions, min_earned) -> dict:
    r_keys, c_keys = DIMENSIONS[rows_dim][1], DIMENSIONS[cols_dim][1]
    keys = list(dict.fromkeys(r_keys + c_keys))
    t = segment_table(pol, clm, keys, a, b, assumptions)
    recs = table_records(t, keys)
    row_vals = sorted({tuple(r["key"][k] for k in r_keys) for r in recs},
                      key=lambda v: tuple(sort_key(k, x) for k, x in zip(r_keys, v)))
    col_vals = sorted({tuple(r["key"][k] for k in c_keys) for r in recs},
                      key=lambda v: tuple(sort_key(k, x) for k, x in zip(c_keys, v)))
    index = {(tuple(r["key"][k] for k in r_keys), tuple(r["key"][k] for k in c_keys)): r for r in recs}
    cells = []
    for rv in row_vals:
        line = []
        for cv in col_vals:
            r = index.get((rv, cv))
            if r is None or not r.get("earned"):
                line.append(None)
            else:
                line.append({
                    "loss_ratio": r["loss_ratio"], "earned": r["earned"], "incurred": r["incurred"],
                    "result": r["result"], "claims": r["claims"], "thin": r["earned"] < min_earned,
                    "key": r["key"],
                })
        cells.append(line)
    return {
        "rows_dim": rows_dim, "cols_dim": cols_dim,
        "rows_label": DIMENSIONS[rows_dim][0], "cols_label": DIMENSIONS[cols_dim][0],
        "rows": [label_of(r_keys, v) for v in row_vals], "cols": [label_of(c_keys, v) for v in col_vals],
        "cells": cells,
    }


def histogram(clm_window: pd.DataFrame, book_window: pd.DataFrame) -> dict:
    sized = clm_window["claim_amount"].dropna()
    edges = list(range(0, 20001, 1000))
    bins = []
    for lo, hi in zip(edges[:-1], edges[1:]):
        bins.append({"lo": lo, "hi": hi, "count": int(((sized >= lo) & (sized < hi)).sum())})
    bins.append({"lo": 20000, "hi": None, "count": int((sized >= 20000).sum())})
    n = len(sized)
    book = book_window["claim_amount"].dropna()

    def share(s, lo, hi):
        return float(((s >= lo) & (s < hi)).sum() / len(s)) if len(s) else None
    return {
        "bins": bins, "n_sized": n,
        "band_share": share(sized, 9000, 10000), "above_share": share(sized, 10000, 11000),
        "below_share": share(sized, 8000, 9000),
        "book_band_share": share(book, 9000, 10000),
        "band_count": int(((sized >= 9000) & (sized < 10000)).sum()),
        "band_paid": float(clm_window.loc[clm_window["near_threshold"], "paid"].sum()),
    }


def ledger(policies, claims, filters, assumptions, asof, data_start, dim, heat_rows, heat_cols, min_earned) -> dict:
    a, b = window(filters.get("period_from"), filters.get("period_to"), data_start, asof)
    pol, clm = apply_filters(policies, claims, filters)
    totals = total_row(pol, clm, a, b, assumptions)
    clm_w = clm[(clm["loss_date"] >= a) & (clm["loss_date"] < b)]
    book_w = claims[(claims["loss_date"] >= a) & (claims["loss_date"] < b)]
    earned_total = totals.get("earned") or 0.0
    per_dollar = {}
    if earned_total > 0:
        per_dollar = {k: totals[k] / earned_total for k in ["paid", "reserve", "commission", "expenses", "result"]}
    settle = clm_w["days_to_settle"].dropna()
    return {
        "window": {"from": a.strftime("%Y-%m-%d"), "to": (b - pd.Timedelta(days=1)).strftime("%Y-%m-%d")},
        "totals": totals,
        "per_dollar": per_dollar,
        "settle_median_days": float(settle.median()) if len(settle) else None,
        "monthly": monthly_series(pol, clm, a, b),
        "breakdown": breakdown(pol, clm, dim, a, b, assumptions, earned_total),
        "heatmap": heatmap(pol, clm, heat_rows, heat_cols, a, b, assumptions, min_earned),
        "histogram": histogram(clm_w, book_w),
        "breakeven_loss_ratio": 1 - assumptions["expense_ratio"]
        - assumptions["broker_commission"] * ((totals.get("earned_broker") or 0) / earned_total if earned_total else 0),
    }


def filter_options(policies: pd.DataFrame, claims: pd.DataFrame) -> dict:
    opts = {}
    for col in POLICY_FILTERS:
        vals = [v for v in policies[col].dropna().unique()]
        opts[col] = sorted(vals, key=lambda v: sort_key(col, v))
    for col in CLAIM_FILTERS:
        vals = [v for v in claims[col].dropna().unique()]
        opts[col] = sorted(vals, key=lambda v: sort_key(col, v))
    opts["deductible"] = [float(v) for v in opts["deductible"]]
    return opts
