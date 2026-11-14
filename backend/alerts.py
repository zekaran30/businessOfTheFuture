"""
Claim alerts and client-retention alerts (the CFO's watch list).

CLAIM ALERTS: one per claim, at the highest level that applies.
  Level 1  Critical  every claim from a watched broker (B07 by default)
  Level 2  High      repeat claimant: the customer's 3rd claim or later (2nd or later for a watched broker)
  Level 3  Medium    claim between $8,000 and $10,000, just under the senior-review line (any channel)
  Level 4  Low       claim over $4,000 that was settled in under 14 days

RETENTION ALERTS: one per claim whose decision took too long for its size.
  under $3,000: more than 20 days;  $3,000 to $8,000: more than 30 days;  over $8,000: more than 45 days.
  Claims still open past their limit count too (days waited so far). All customers, including past ones.

Claims are counted per customer (all of a customer's policies together), in loss-date order.
"""
from __future__ import annotations

from collections import Counter

import numpy as np
import pandas as pd

DEFAULT_WATCH = {
    "fast_amount": 4000, "fast_days": 14,
    "band_low": 8000, "band_high": 10000,
    "repeat_claims": 3, "repeat_claims_watched": 2,
    "watchlist": ["B07"],
    "ret_small": 3000, "ret_large": 8000,
    "ret_small_days": 20, "ret_mid_days": 30, "ret_large_days": 45,
}
INT_KEYS = ["fast_days", "repeat_claims", "repeat_claims_watched", "ret_small_days", "ret_mid_days", "ret_large_days"]
FLOAT_KEYS = ["fast_amount", "band_low", "band_high", "ret_small", "ret_large"]
LEVEL_LABEL = {1: "Critical", 2: "High", 3: "Medium", 4: "Low"}
LEVEL_RULE = {1: "Watched broker", 2: "Repeat claimant", 3: "Just under senior review", 4: "Big claim, paid fast"}
CLAIM_STATUSES = ["New", "Reviewing", "Cleared", "Escalated"]
RETENTION_STATUSES = ["To call", "Satisfied", "Offer made", "No answer"]
PRIORITIES = ["Urgent", "High", "Standard", "Win-back"]
CALL = "Check up on the client to make sure they're satisfied with their service, and offer them something if they're not."


def money(x) -> str:
    if x is None or pd.isna(x):
        return "n/a"
    return f"${x:,.0f}"


def ordinal(n: int) -> str:
    n = int(n)
    suffix = "th" if 10 <= n % 100 <= 20 else {1: "st", 2: "nd", 3: "rd"}.get(n % 10, "th")
    return f"{n}{suffix}"


def _date(ts):
    return None if ts is None or pd.isna(ts) else pd.Timestamp(ts).strftime("%Y-%m-%d")


def _num(x):
    return None if x is None or pd.isna(x) else float(x)


def normalise(settings: dict | None) -> dict:
    s = {**DEFAULT_WATCH, **(settings or {})}
    wl = s.get("watchlist") or []
    if isinstance(wl, str):
        wl = wl.replace(";", ",").split(",")
    s["watchlist"] = sorted({str(w).strip().upper() for w in wl if str(w).strip()})
    for k in INT_KEYS:
        s[k] = max(0, int(round(float(s[k]))))
    for k in FLOAT_KEYS:
        s[k] = max(0.0, float(s[k]))
    if s["band_high"] < s["band_low"]:
        s["band_low"], s["band_high"] = s["band_high"], s["band_low"]
    if s["ret_large"] < s["ret_small"]:
        s["ret_small"], s["ret_large"] = s["ret_large"], s["ret_small"]
    return s


def claim_alerts(c: pd.DataFrame, s: dict) -> list:
    amt = c["claim_amount"]
    watched = c["source"].isin(s["watchlist"])
    settle = c["days_to_settle"]
    fast = amt.gt(s["fast_amount"]) & settle.lt(s["fast_days"])
    band = amt.ge(s["band_low"]) & amt.le(s["band_high"])
    need = np.where(watched, s["repeat_claims_watched"], s["repeat_claims"])
    repeat = c["claim_seq"].to_numpy() >= need
    hit = (watched.to_numpy() | repeat | band.to_numpy() | fast.to_numpy())
    out = []
    for i in np.flatnonzero(hit):
        r = c.iloc[i]
        rules, reasons = [], []
        if watched.iat[i]:
            rules.append("watched")
            reasons.append(f"Watched broker {r['source']}: every claim gets checked")
        if repeat[i]:
            rules.append("repeat")
            reasons.append(f"{ordinal(r['claim_seq'])} claim from this customer ({int(r['customer_claims'])} in total)")
        if band.iat[i]:
            rules.append("band")
            reasons.append(f"Priced at {money(r['claim_amount'])}, just under the $10,000 senior-review line")
        if fast.iat[i]:
            rules.append("fast")
            reasons.append(f"Over {money(s['fast_amount'])} and settled in {int(r['days_to_settle'])} days")
        level = 1 if watched.iat[i] else 2 if repeat[i] else 3 if band.iat[i] else 4
        if level == 1:
            action = (f"Senior review before any further payment, and audit this customer's claims. "
                      f"Pause new business from {r['source']} until the audit is done.")
            owners = ["VP of Claims", "Head of Distribution", "CFO"]
        elif level == 2:
            action = "Check this customer's claim history for staged or duplicate losses before paying."
            owners = ["VP of Claims", "CFO"]
        elif level == 3:
            action = "Send to senior review even though it's under $10,000."
            owners = ["VP of Claims"]
        else:
            action = "Spot-check: a large claim was approved unusually fast."
            owners = ["VP of Claims"]
        out.append({
            "key": f"claim:{r['claim_id']}", "kind": "claim", "level": level, "level_label": LEVEL_LABEL[level],
            "level_rule": LEVEL_RULE[level], "claim_id": r["claim_id"], "policy_id": r["policy_id"],
            "customer_id": r["customer_id"], "source": r["source"], "city": r["city"], "policy_type": r["policy_type"],
            "claim_type": r["claim_type"], "claim_amount": _num(r["claim_amount"]), "paid": _num(r["paid"]),
            "status": r["status"], "loss_date": _date(r["loss_date"]),
            "days_to_settle": None if pd.isna(r["days_to_settle"]) else int(r["days_to_settle"]),
            "claim_seq": int(r["claim_seq"]), "customer_claims": int(r["customer_claims"]),
            "claim_number": r["claim_number"], "rules": rules, "reasons": reasons, "action": action, "owners": owners,
        })
    out.sort(key=lambda a: (a["level"], -(a["claim_amount"] or 0)))
    return out


def retention_alerts(c: pd.DataFrame, s: dict) -> list:
    estimated = c["claim_amount"].isna()
    amt = c["claim_amount"].fillna(c["paid"] + c["deductible"])  # no amount: paid + deductible (a floor)
    limit = np.where(amt < s["ret_small"], s["ret_small_days"],
                     np.where(amt <= s["ret_large"], s["ret_mid_days"], s["ret_large_days"]))
    wait = c["wait_days"].to_numpy(dtype=float)
    hit = np.nan_to_num(wait, nan=-1) > limit
    out = []
    for i in np.flatnonzero(hit):
        r = c.iloc[i]
        w, lim = int(wait[i]), int(limit[i])
        status = r["renewal_status"]
        if r["is_open"]:
            priority = "Urgent"
            action = (f"Still waiting after {w} days. Call the client today, tell them where their claim stands, "
                      f"make sure they're satisfied with the service, and offer them something if they're not.")
        elif status == "In Force":
            priority = "High"
            action = f"Waited {w} days and their renewal is still to come. {CALL}"
        elif status == "Renewed":
            priority = "Standard"
            action = f"Renewed, but waited {w} days for a decision. {CALL}"
        else:
            priority = "Win-back"
            action = (f"Left after waiting {w} days for a decision. Call to understand why, and offer them "
                      f"something to come back.")
        out.append({
            "key": f"ret:{r['claim_id']}", "kind": "retention", "priority": priority,
            "claim_id": r["claim_id"], "policy_id": r["policy_id"], "customer_id": r["customer_id"],
            "source": r["source"], "city": r["city"], "policy_type": r["policy_type"], "claim_type": r["claim_type"],
            "adjuster_team": r["adjuster_team"], "amount": float(amt.iat[i]), "amount_estimated": bool(estimated.iat[i]),
            "status": r["status"], "is_open": bool(r["is_open"]), "wait_days": w, "limit": lim, "days_over": w - lim,
            "renewal_status": status, "reported_date": _date(r["reported_date"]), "closed_date": _date(r["closed_date"]),
            "action": action, "owners": ["Head of Customer Retention"],
        })
    out.sort(key=lambda a: (PRIORITIES.index(a["priority"]), -a["days_over"]))
    return out


def summarise(claims: list, retention: list) -> dict:
    levels = {}
    for lvl in (1, 2, 3, 4):
        items = [a for a in claims if a["level"] == lvl]
        levels[str(lvl)] = {"label": LEVEL_LABEL[lvl], "rule": LEVEL_RULE[lvl], "count": len(items),
                            "amount": sum(a["claim_amount"] or 0 for a in items),
                            "paid": sum(a["paid"] or 0 for a in items)}
    priorities = {}
    for p in PRIORITIES:
        items = [a for a in retention if a["priority"] == p]
        priorities[p] = {"count": len(items), "amount": sum(a["amount"] for a in items)}
    sources = [s for s, _ in Counter(a["source"] for a in claims).most_common(14)]
    teams = sorted({a["adjuster_team"] for a in retention if a["adjuster_team"]})
    return {
        "claims_total": len(claims), "retention_total": len(retention),
        "levels": levels, "priorities": priorities,
        "chart_claims": {"labels": sources, "series": {str(l): [sum(1 for a in claims if a["source"] == s and a["level"] == l)
                                                                 for s in sources] for l in (1, 2, 3, 4)}},
        "chart_retention": {"labels": teams, "series": {p: [sum(1 for a in retention if a["adjuster_team"] == t and a["priority"] == p)
                                                             for t in teams] for p in PRIORITIES}},
    }
