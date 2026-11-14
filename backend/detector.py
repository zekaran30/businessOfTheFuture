"""
The quarterly check: which parts of the business are losing money?

The book is cut three ways ("lenses"). Each lens splits it into non-overlapping segments:
  where  - city and product            (e.g. Montreal / Home)
  who    - broker, agent or online     (e.g. B07)
  price  - product and risk band       (e.g. Home / Medium)

Rules, evaluated for every segment in the chosen quarter:
  1. Losing money   claims + commission + operating costs exceed the premium earned.
                    Critical when claims alone exceed the premium.
  2. Getting worse  loss ratio up 15+ points on the previous four quarters (and at least 60%).
  3. Claims parked under the senior-review line
                    25%+ of a segment's claims priced $9,000-$9,999 (at least 4 claims),
                    against about 3% for the rest of the book.

Safeguards against false alarms (all adjustable in the app):
  - Materiality: rules 1 and 2 need at least $10,000 of money at stake in the quarter.
  - Not bad luck: rules 1 and 2 only fire when the loss is too large to be normal randomness.
    Claim costs are modelled as a compound Poisson process using the book's own claim sizes
    over the last year; a flag needs less than a 5% chance of being bad luck.
  - Size: at least $15,000 of earned premium and 3 claims in the quarter.
  - Large losses: single claims are capped at $25,000 when testing the rules, so one fire
    cannot flag a segment on its own. The uncapped money is still reported.
  - Rule 3 does not depend on premium, so it catches a new broker before its premium builds up.
"""
from __future__ import annotations

import math

import numpy as np
import pandas as pd

from . import metrics as M

LENSES = [
    {"key": "where", "label": "Where the business is", "detail": "City and product", "keys": ["city", "policy_type"],
     "owner": "Pricing & underwriting"},
    {"key": "who", "label": "Who sold it", "detail": "Broker, agent or online", "keys": ["source"],
     "owner": "Head of Distribution"},
    {"key": "price", "label": "How it's priced", "detail": "Product and risk band", "keys": ["policy_type", "risk_band"],
     "owner": "Pricing & underwriting"},
]
SEVERITY_RANK = {"critical": 0, "high": 1, "watch": 2}

DEFAULT_RULES = {
    "min_earned": 15_000, "min_claims": 3, "materiality": 10_000, "confidence": 0.95,
    "large_loss_cap": 25_000, "worsening_points": 15, "worsening_floor": 0.60,
    "band_min_claims": 4, "band_min_share": 0.25, "early_days": 60,
}


# ------------------------------------------------------------------ helpers
def _pct(x):
    return "n/a" if x is None or pd.isna(x) else f"{x:.0%}"


def _money(x):
    if x is None or pd.isna(x):
        return "n/a"
    sign = "-" if x < 0 else ""
    x = abs(x)
    if x >= 1e6:
        return f"{sign}${x / 1e6:.2f}M"
    if x >= 1e4:
        return f"{sign}${x / 1e3:.0f}K"
    return f"{sign}${x:,.0f}"


def _mask(df: pd.DataFrame, keys: list, values: tuple) -> pd.Series:
    m = pd.Series(True, index=df.index)
    for k, v in zip(keys, values):
        m &= df[k] == v
    return m


def normal_tail(z: float) -> float:
    """P(Z >= z) for a standard normal."""
    return 0.5 * math.erfc(z / math.sqrt(2))


def z_for(confidence: float) -> float:
    lo, hi = 0.0, 8.0
    for _ in range(60):  # bisection, avoids a scipy dependency
        mid = (lo + hi) / 2
        lo, hi = (mid, hi) if normal_tail(mid) > 1 - confidence else (lo, mid)
    return (lo + hi) / 2


def binomial_tail(k: int, n: int, p: float) -> float:
    """P(X >= k) for X ~ Binomial(n, p)."""
    if k <= 0:
        return 1.0
    p = min(max(p, 0.0), 1.0)
    return float(sum(math.comb(n, i) * p ** i * (1 - p) ** (n - i) for i in range(k, n + 1)))


def luck_text(p: float | None) -> str:
    if p is None:
        return "n/a"
    if p < 0.001:
        return "under 0.1%"
    if p < 0.01:
        return "under 1%"
    return f"{p:.0%}"


# -------------------------------------------------------------------- check
def run_check(policies, claims, quarter, assumptions, rules, asof, data_start) -> dict:
    rules = {**DEFAULT_RULES, **(rules or {})}
    cap = rules["large_loss_cap"]
    a, b = M.window(quarter, quarter, data_start, asof)
    q_start = M.quarter_bounds(quarter)[0]
    ba = max(data_start, q_start - pd.DateOffset(months=12))
    bb = q_start
    has_baseline = (bb - ba).days >= 60

    book = M.total_row(policies, claims, a, b, assumptions, cap)
    book_base = M.total_row(policies, claims, ba, bb, assumptions, cap) if has_baseline else {}
    hist_all = claims[(claims["loss_date"] >= q_start - pd.DateOffset(months=12)) & (claims["loss_date"] < b)]
    X = hist_all["incurred"].clip(upper=cap)
    ex = float(X.mean()) if len(X) else 1.0
    disp = float((X ** 2).mean() / ex) if len(X) and ex > 0 else 1.0  # variance per $ of expected claim cost
    ctx = {"rules": rules, "zc": z_for(rules["confidence"]), "disp": disp, "assumptions": assumptions,
           "band_sources": set()}

    flags, lens_rows = [], {}
    # "who" runs first. A broker flagged for claims parked under the review line is then taken out of the
    # other two lenses, so one problem is not flagged three times (as B11, Toronto Auto and Auto Low).
    for lens in (LENSES[1], LENSES[0], LENSES[2]):
        keys = lens["keys"]
        out = ctx["band_sources"] if lens["key"] != "who" else set()
        pol_l = policies[~policies["source"].isin(out)] if out else policies
        clm_l = claims[~claims["source"].isin(out)] if out else claims
        ctx["clm_q"] = clm_l[(clm_l["loss_date"] >= a) & (clm_l["loss_date"] < b)]
        ctx["hist"] = clm_l[(clm_l["loss_date"] >= q_start - pd.DateOffset(months=12)) & (clm_l["loss_date"] < b)]
        ctx["book"] = M.total_row(pol_l, clm_l, a, b, assumptions, cap) if out else book
        ctx["book_settle"] = ctx["clm_q"]["days_to_settle"].median()
        tq = M.segment_table(pol_l, clm_l, keys, a, b, assumptions, cap)
        tb = M.segment_table(pol_l, clm_l, keys, ba, bb, assumptions, cap) if has_baseline else None
        rows = []
        for idx, r in tq.iterrows():
            values = idx if isinstance(idx, tuple) else (idx,)
            base = tb.loc[idx] if (tb is not None and idx in tb.index) else None
            seg = _evaluate(lens, keys, values, r, base, ctx)
            rows.append(seg["row"])
            if seg["reasons"]:
                flags.append(_build_flag(lens, keys, values, r, base, seg, ctx, quarter))
                if lens["key"] == "who" and any(x["rule"] == "threshold_cluster" for x in seg["reasons"]):
                    ctx["band_sources"].add(values[0])
        lens_rows[lens["key"]] = sorted(rows, key=lambda x: -(x["loss_ratio_capped"] or 0))

    flags.sort(key=lambda f: (SEVERITY_RANK[f["severity"]], -(f["shortfall"] or 0)))
    losing = [f for f in flags if any(x["rule"] == "losing_money" for x in f["reasons"])]
    counts = {
        "flags": len(flags), "losing": len(losing),
        "new": sum(1 for f in flags if f["group"] == "new"),
        "ongoing": sum(1 for f in flags if f["group"] == "ongoing"),
        "pattern": sum(1 for f in flags if any(x["rule"] == "threshold_cluster" for x in f["reasons"])),
        "critical": sum(1 for f in flags if f["severity"] == "critical"),
        "high": sum(1 for f in flags if f["severity"] == "high"),
        "watch": sum(1 for f in flags if f["severity"] == "watch"),
    }
    return {
        "quarter": quarter,
        "label": M.quarter_label(quarter, asof),
        "window": {"from": a.strftime("%Y-%m-%d"), "to": (b - pd.Timedelta(days=1)).strftime("%Y-%m-%d")},
        "baseline": ({"from": ba.strftime("%Y-%m-%d"), "to": (bb - pd.Timedelta(days=1)).strftime("%Y-%m-%d")}
                     if has_baseline else None),
        "book": book, "book_baseline": book_base,
        "flags": flags, "counts": counts,
        "set_aside": sorted(ctx["band_sources"]),
        "lenses": [{k: v for k, v in L.items() if k != "keys"} for L in LENSES],
        "segments": lens_rows, "rules": rules, "assumptions": assumptions,
        "headline": _headline(quarter, asof, flags, losing),
    }


def _evaluate(lens, keys, values, r, base, ctx) -> dict:
    rules, zc, disp, book = ctx["rules"], ctx["zc"], ctx["disp"], ctx["book"]
    A = ctx["assumptions"]
    credible = r["earned"] >= rules["min_earned"] and r["claims"] >= rules["min_claims"]
    base_credible = (base is not None and base["earned"] >= rules["min_earned"]
                     and base["claims"] >= rules["min_claims"])
    lr = r["loss_ratio_capped"]
    reasons, status = [], None

    # Rule 1: losing money, beyond normal randomness
    broker_mix = r["earned_broker"] / r["earned"] if r["earned"] else 0
    breakeven_cost = r["earned"] * (1 - A["expense_ratio"] - A["broker_commission"] * broker_mix)
    excess = r["capped"] - breakeven_cost
    z = excess / math.sqrt(max(breakeven_cost, 1.0) * disp)
    if credible and excess >= rules["materiality"] and z >= zc:
        luck = normal_tail(z)
        sev = "critical" if lr >= 1 else "high"
        status = ("ongoing" if base["combined_ratio_capped"] >= 1 else "new") if base_credible else "no_history"
        text = f"Loses money after costs: combined ratio {_pct(r['combined_ratio_capped'])}"
        text += ", and claims alone are more than the premium" if lr >= 1 else ""
        reasons.append({"rule": "losing_money", "severity": sev, "luck": luck,
                        "text": text + f". Chance this is just bad luck: {luck_text(luck)}."})
    # Rule 2: getting worse, beyond normal randomness
    elif credible and base_credible and pd.notna(base["loss_ratio_capped"]):
        delta = lr - base["loss_ratio_capped"]
        expected = r["earned"] * base["loss_ratio_capped"]
        excess_w = r["capped"] - expected
        z_w = excess_w / math.sqrt(max(expected, 1.0) * disp)
        if (delta * 100 >= rules["worsening_points"] and lr >= rules["worsening_floor"]
                and excess_w >= rules["materiality"] and z_w >= zc):
            luck = normal_tail(z_w)
            status = "worsening"
            reasons.append({"rule": "worsening", "severity": "watch", "luck": luck,
                            "text": f"Loss ratio up {delta * 100:.0f} points, to {_pct(lr)} from "
                                    f"{_pct(base['loss_ratio_capped'])} over the previous four quarters (single claims "
                                    f"capped at {_money(rules['large_loss_cap'])}). "
                                    f"Chance this is just bad luck: {luck_text(luck)}."})

    # Rule 3: claims parked just under the senior-review line
    band_n, sized = int(r["band"]), int(r["sized"])
    band_share = r["band_share"]
    explained_by, band_ref = None, None
    if band_n >= rules["band_min_claims"] and pd.notna(band_share) and band_share >= rules["band_min_share"]:
        hist = ctx["hist"]
        rest = hist[~_mask(hist, keys, values) & hist["claim_amount"].notna()]
        band_ref = float(rest["near_threshold"].mean()) if len(rest) else 0.03
        p_band = binomial_tail(band_n, sized, max(band_ref, 0.005))
        if lens["key"] != "who" and ctx["band_sources"]:
            C = ctx["clm_q"]
            seg_band = C[_mask(C, keys, values) & C["near_threshold"]]
            if len(seg_band) and seg_band["source"].isin(ctx["band_sources"]).mean() >= 0.6:
                explained_by = ", ".join(sorted(ctx["band_sources"]))
        if explained_by is None and p_band < 1 - rules["confidence"]:
            reasons.append({"rule": "threshold_cluster", "severity": "critical", "luck": p_band,
                            "text": f"{band_n} of {sized} claims ({_pct(band_share)}) were priced between $9,000 and "
                                    f"$9,999, just under the $10,000 senior-review line. Rest of the book: "
                                    f"{_pct(band_ref)}. Chance this is just bad luck: {luck_text(p_band)}."})
            status = status or "pattern"

    row = M.clean_record(r.to_dict())
    row.update({
        "label": M.label_of(keys, values), "key": dict(zip(keys, values)), "credible": bool(credible),
        "baseline_loss_ratio_capped": (float(base["loss_ratio_capped"])
                                       if base_credible and pd.notna(base["loss_ratio_capped"]) else None),
        "flagged": bool(reasons), "z": float(z) if np.isfinite(z) else None,
    })
    return {"reasons": reasons, "status": status, "row": row, "explained_by": explained_by, "band_ref": band_ref}


def _build_flag(lens, keys, values, r, base, seg, ctx, quarter) -> dict:
    rules, book = ctx["rules"], ctx["book"]
    C = ctx["clm_q"][_mask(ctx["clm_q"], keys, values)]
    label = M.label_of(keys, values)
    severity = min((x["severity"] for x in seg["reasons"]), key=lambda s: SEVERITY_RANK[s])
    rules_hit = {x["rule"] for x in seg["reasons"]}

    # Compare with the rest of the book (the book minus this segment)
    rest_claims = (book.get("claims") or 0) - r["claims"]
    rest_expo = (book.get("exposure") or 0) - r["exposure"]
    rest_inc = (book.get("incurred") or 0) - r["incurred"]
    rest_early = (book.get("early") or 0) - r["early"]
    rest_freq = rest_claims / rest_expo if rest_expo > 0 else None
    rest_sev = rest_inc / rest_claims if rest_claims > 0 else None
    rest_early_share = rest_early / rest_claims if rest_claims > 0 else None

    drivers = []
    if base is None or base["earned"] < rules["min_earned"] or pd.isna(base["loss_ratio"]):
        drivers.append("No reliable history: this segment was too small, or did not exist, in the previous four quarters.")
    # (d) one-off event? Most of the cost landing within three days points to a storm or similar
    event = None
    if len(C) >= 5 and C["incurred"].sum() > 0:
        daily = C.groupby(C["loss_date"].dt.normalize())["incurred"].sum()
        days = pd.date_range(daily.index.min(), daily.index.max(), freq="D")
        three = daily.reindex(days, fill_value=0.0).rolling(3, min_periods=1).sum()
        end = three.idxmax()
        start = max(end - pd.Timedelta(days=2), days[0])
        n_in = int(((C["loss_date"] >= start) & (C["loss_date"] <= end)).sum())
        share = float(three.max() / C["incurred"].sum())
        if share >= 0.4 and n_in >= 5:
            event = f"{start.strftime('%b')} {start.day} to {end.strftime('%b')} {end.day}"
            drivers.append(f"{share:.0%} of the cost comes from {n_in} claims dated {event}: "
                           f"it looks like a single event, such as a storm.")
    if rest_freq and pd.notna(r["frequency"]) and r["frequency"] / rest_freq >= 1.3:
        drivers.append(f"Claims happen {r['frequency'] / rest_freq:.1f}x as often as in the rest of the book "
                       f"({r['frequency']:.2f} vs {rest_freq:.2f} per policy-year).")
    if rest_sev and pd.notna(r["severity"]) and r["severity"] / rest_sev >= 1.3:
        drivers.append(f"The average claim costs {r['severity'] / rest_sev:.1f}x the rest of the book "
                       f"({_money(r['severity'])} vs {_money(rest_sev)}).")
    if len(C) and C["incurred"].sum() > 0:
        by_type = C.groupby("claim_type")["incurred"].sum().sort_values(ascending=False)
        drivers.append(f"{by_type.index[0]} is {by_type.iloc[0] / by_type.sum():.0%} of the claim cost.")
    if r["claims"] and pd.notna(r["early_share"]) and rest_early_share is not None:
        if r["early_share"] >= max(0.35, 1.8 * rest_early_share):
            drivers.append(f"{r['early_share']:.0%} of claims came within {rules['early_days']} days of the policy "
                           f"starting (rest of the book: {rest_early_share:.0%}).")
    if r["large"]:
        big = C[C["incurred"] > rules["large_loss_cap"]]
        drivers.append(f"{len(big)} large claim(s) over {_money(rules['large_loss_cap'])} "
                       f"({_money(big['incurred'].sum())}), capped when testing the rules.")
    if seg["explained_by"]:
        drivers.append(f"Most claims just under $10,000 here come from {seg['explained_by']}, which is already flagged.")
    if len(C) and C["incurred"].sum() > 0:  # cross-lens: where a broker's losses are, and who is behind a place
        if lens["key"] == "who":
            g = C.groupby(["city", "policy_type"])["incurred"].sum().sort_values(ascending=False)
            if g.iloc[0] / g.sum() >= 0.4:
                drivers.append(f"{g.iloc[0] / g.sum():.0%} of the cost is {g.index[0][0]} {g.index[0][1]}.")
        else:
            g = C.groupby("source")["incurred"].sum().sort_values(ascending=False)
            if g.iloc[0] / g.sum() >= 0.3 and g.index[0] not in ("Agent", "Online"):
                drivers.append(f"Broker {g.index[0]} accounts for {g.iloc[0] / g.sum():.0%} of the cost.")
    settle = C["days_to_settle"].median() if len(C) else np.nan
    if pd.notna(settle) and pd.notna(ctx["book_settle"]) and settle >= 1.5 * ctx["book_settle"] and settle >= 20:
        drivers.append(f"Claims take a median {settle:.0f} days to settle (book: {ctx['book_settle']:.0f}). "
                       f"Customers whose claim took over 30 days renewed far less often.")

    # Recommended next step, and who receives it
    owners = [lens["owner"], "CFO"]
    seg_name = label.replace(" / ", " ")
    be = r["breakeven_change"]
    if "threshold_cluster" in rules_hit:
        paid = C.loc[C["near_threshold"], "paid"].sum()
        action = (f"Send {seg_name}'s claims between $9,000 and $9,999 to senior review before payment, audit the "
                  f"{int(C['near_threshold'].sum())} from this quarter ({_money(paid)} already paid), and pause new "
                  f"business from {seg_name} until the audit is done.")
        owners = ["VP of Claims", lens["owner"], "CFO"]
    elif "losing_money" in rules_hit and lens["key"] == "who":
        if values[0] in ("Agent", "Online"):
            action = (f"The {seg_name} channel is losing money overall. Check pricing on the products it sells most "
                      f"before changing the channel itself.")
        else:
            action = (f"Review broker {seg_name}'s book before its next renewal: tighten underwriting on its "
                      f"submissions and revisit its commission. Premium would need to be about {be:+.0%} higher to "
                      f"break even.")
    elif "losing_money" in rules_hit and event:
        action = (f"Most of this loss comes from one event ({event}). Confirm the cause with the claims team and check "
                  f"catastrophe cover. Re-price only if losses stay high once the event is set aside.")
    elif "losing_money" in rules_hit:
        top = C.groupby("claim_type")["incurred"].sum().idxmax() if len(C) and C["incurred"].sum() > 0 else ""
        action = (f"Re-price or tighten underwriting for {seg_name}: premium would need to rise about {be:+.0%} "
                  f"to break even" + (f", and {top.lower()} claims are the main cost." if top else "."))
    else:
        action = (f"Watch {seg_name} next month: losses are rising faster than its own history. "
                  f"Hold pricing changes until the trend holds for a second quarter.")

    review = []
    if "threshold_cluster" in rules_hit:
        cols = ["claim_id", "policy_id", "city", "policy_type", "claim_type", "loss_date", "claim_amount", "paid",
                "status", "days_from_start", "adjuster_team"]
        review = [M.clean_record(x) for x in C[C["near_threshold"]][cols].sort_values("loss_date").to_dict("records")]

    def num(x):
        return M.clean_record({"x": x})["x"]

    # New this quarter, or a problem that was already there over the previous four quarters?
    had_pattern = (base is not None and base["band"] >= rules["band_min_claims"]
                   and pd.notna(base["band_share"]) and base["band_share"] >= rules["band_min_share"])
    if severity == "watch":
        group = "watch"
    elif seg["status"] == "ongoing" or ("threshold_cluster" in rules_hit and had_pattern):
        group = "ongoing"
    else:
        group = "new"

    return {
        "id": f"{quarter}|{lens['key']}|{label}",
        "group": group,
        "lens": lens["key"], "lens_label": lens["label"], "lens_detail": lens["detail"],
        "segment": label, "key": dict(zip(keys, values)),
        "severity": severity, "status": seg["status"], "reasons": seg["reasons"],
        "drivers": drivers, "action": action, "owners": owners,
        "shortfall": float(max(0.0, -r["result"])) if pd.notna(r["result"]) else 0.0,
        "metrics": {
            "earned": float(r["earned"]), "incurred": float(r["incurred"]), "paid": float(r["paid"]),
            "reserve": float(r["reserve"]), "claims": int(r["claims"]), "result": float(r["result"]),
            "loss_ratio": num(r["loss_ratio"]), "combined_ratio": num(r["combined_ratio"]),
            "loss_ratio_capped": num(r["loss_ratio_capped"]),
            "baseline_loss_ratio": num(base["loss_ratio"]) if base is not None else None,
            "breakeven_change": num(be), "band": int(r["band"]), "band_share": num(r["band_share"]),
            "band_ref": seg["band_ref"],
        },
        "review": review,
    }


def _headline(quarter, asof, flags, losing) -> str:
    label = M.quarter_label(quarter, asof)
    if not flags:
        return f"{label}: nothing crossed the alert lines."
    new = [f for f in flags if f["group"] == "new"]
    ongoing = [f for f in flags if f["group"] == "ongoing"]
    watch = [f for f in flags if f["group"] == "watch"]
    parts = []
    if new:
        parts.append(f"{len(new)} new problem{'s' if len(new) != 1 else ''} ({', '.join(f['segment'] for f in new)})")
    if ongoing:
        parts.append(f"{len(ongoing)} still losing money from before")
    if watch:
        parts.append(f"{len(watch)} getting worse")
    text = f"{label}: " + ", ".join(parts) + "."
    worst = max([f for f in flags if f["group"] != "watch"], key=lambda f: f["shortfall"], default=None)
    if worst and worst["shortfall"] > 0:
        text += f" Biggest loss after costs: {worst['segment']}, {_money(worst['shortfall'])}."
    return text
