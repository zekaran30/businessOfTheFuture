"""
Builds a SYNTHETIC "2026 Q4 extract" for demoing the New quarter screen.

It continues the patterns found in the real data (claim frequency by city and product,
claim-type mix, claim sizes, settlement speed by adjuster team, renewal statuses) and
adds two demo events so the check has something new to find:

  1. A new broker, B11, signed in November, whose customers file theft claims priced just
     under the $10,000 senior-review line within weeks of buying a policy (B07's pattern).
  2. An ice storm in Calgary on December 8-10 that causes a burst of auto collision claims.

It also re-sends October's rows (some with updated claim statuses) and includes the kind of
mess found in the original extract: city spellings, "$1,234.56" premiums, day-first dates,
a test policy, duplicate claim rows and a claim for a policy that doesn't exist.

Run from the project folder:  python tools/make_sample_quarter.py
"""
from __future__ import annotations

import sys
from pathlib import Path

import numpy as np
import pandas as pd

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

from backend import pipeline as P  # noqa: E402

OUT = ROOT / "data" / "sample_new_quarter"
Q_START, NEW_START, Q_END = pd.Timestamp("2026-10-01"), pd.Timestamp("2026-11-01"), pd.Timestamp("2026-12-31")
SEED = 42  # chosen with --search: ordinary business matches the last four real quarters
rng = np.random.default_rng(SEED)

CITY_VARIANTS = {"Toronto": ["toronto", "Toronto "], "Montreal": ["MTL", "montreal", "Montréal"],
                 "Quebec City": ["QC City", "Québec City"], "Ottawa": ["ottawa"], "Calgary": ["calgary", "Calgary "]}
TEAM = {"Toronto": "Team A", "Ottawa": "Team A", "Calgary": "Team B", "Montreal": "Team C", "Quebec City": "Team C"}
SETTLE_MEDIAN = {"Team A": 11, "Team B": 10, "Team C": 24}  # days, from 2026 claims


def load_clean():
    log = P.CleaningLog()
    max_date = P.EXTRACTION_DATE + pd.DateOffset(years=2)
    raw_p, raw_c = P.read_text_csv(ROOT / "data" / "policies.csv"), P.read_text_csv(ROOT / "data" / "claims.csv")
    pol, _ = P.clean_policies(raw_p, log, max_date)
    clm, _ = P.clean_claims(raw_c, log, set(pol["policy_id"]), max_date)
    return raw_p, raw_c, pol, P.enrich_claims(clm, pol)


def d(ts, messy=False):
    return ts.strftime("%d/%m/%Y") if messy else ts.strftime("%Y-%m-%d")


def settle_days(team, amount):
    days = rng.lognormal(np.log(SETTLE_MEDIAN[team]), 0.6)
    if amount >= P.SENIOR_REVIEW_THRESHOLD:
        days += rng.uniform(10, 30)  # senior review adds time
    return max(1, int(round(days)))


def outcome(amount, deductible, reported, team, fast=False):
    """Status, paid amount and closed date as of the end of the quarter."""
    days = rng.integers(4, 12) if fast else settle_days(team, amount)
    closed = reported + pd.Timedelta(days=int(days))
    if closed > Q_END:
        return ("Under Review" if amount >= P.SENIOR_REVIEW_THRESHOLD else "Open"), None, None
    if rng.random() < 0.048 and not fast:
        return "Denied", 0.0, closed
    paid = max(0.0, amount - deductible) * rng.uniform(0.9, 1.0)
    return "Paid", round(paid, 2), closed


def build(seed, loaded):
    global rng
    rng = np.random.default_rng(seed)
    raw_p, raw_c, pol, clm = loaded
    hist = clm[clm["source"] != "B07"]

    # ---------------------------------------------------------- policies
    rows = []
    starts = pd.Series(P.parse_dates(P._trim(raw_p["start_date"]))[0])
    october_raw = raw_p[(starts >= Q_START) & (starts < NEW_START)]  # re-sent rows, as exported

    pool = pol[(pol["start"] >= "2026-01-01") & (pol["source"] != "B07")]
    next_pol, next_cust = 4681, 13311
    days = pd.date_range(NEW_START, Q_END, freq="D")
    for day in days:
        n = rng.poisson(5.6 if day.month == 11 else 4.6)  # quieter December
        for _ in range(n):
            t = pool.sample(1, random_state=int(rng.integers(1e9))).iloc[0]
            premium = max(450.0, t["premium"] * rng.uniform(0.95, 1.07))
            rows.append({"policy_id": f"POL-{next_pol:05d}", "customer_id": f"CU{next_cust}",
                         "customer_age": t["customer_age"], "city": t["city"], "policy_type": t["policy_type"],
                         "risk_band": t["risk_band"], "channel": t["channel"], "broker_code": t["broker_code"],
                         "start": day, "deductible": int(t["deductible"]), "premium": round(premium, 2),
                         "renewal_status": "Cancelled" if rng.random() < 0.02 else "In Force"})
            next_pol += 1
            next_cust += 1

    # Demo event 1: broker B11 joins in November; B07 writes a few more
    for broker, n, window in (("B11", 48, ("2026-11-02", "2026-12-18")), ("B07", 3, ("2026-11-03", "2026-11-25"))):
        for day in sorted(pd.to_datetime(rng.uniform(pd.Timestamp(window[0]).value, pd.Timestamp(window[1]).value, n))):
            city = rng.choice(["Toronto", "Ottawa", "Montreal"], p=[0.6, 0.25, 0.15])
            ptype = "Auto" if rng.random() < 0.7 else "Home"
            risk = "Low" if rng.random() < 0.85 else "Medium"
            ref = pol[(pol["policy_type"] == ptype) & (pol["risk_band"] == risk) & (pol["city"] == city)]["premium"]
            rows.append({"policy_id": f"POL-{next_pol:05d}", "customer_id": f"CU{next_cust}",
                         "customer_age": int(rng.integers(24, 56)), "city": city, "policy_type": ptype,
                         "risk_band": risk, "channel": "Broker", "broker_code": broker,
                         "start": day.normalize(), "deductible": int(rng.choice([500, 1000], p=[0.6, 0.4])),
                         "premium": round(float(ref.quantile(rng.uniform(0.1, 0.45))), 2), "renewal_status": "In Force"})
            next_pol += 1
            next_cust += 1
    new_pol = pd.DataFrame(rows).sort_values("start").reset_index(drop=True)
    new_pol["policy_id"] = [f"POL-{4681 + i:05d}" for i in range(len(new_pol))]
    new_pol["end"] = new_pol["start"] + pd.Timedelta(days=365)

    # ------------------------------------------------------------ claims
    claims = []
    all_pol = pd.concat([pol[["policy_id", "city", "policy_type", "source", "deductible", "start", "end"]],
                         new_pol.assign(source=np.where(new_pol["channel"] == "Broker", new_pol["broker_code"],
                                                        new_pol["channel"]))[
                             ["policy_id", "city", "policy_type", "source", "deductible", "start", "end"]]])
    freq = {("Calgary", "Auto"): .242, ("Calgary", "Home"): .149, ("Montreal", "Auto"): .216,
            ("Montreal", "Home"): .282, ("Ottawa", "Auto"): .237, ("Ottawa", "Home"): .174,
            ("Quebec City", "Auto"): .256, ("Quebec City", "Home"): .122, ("Toronto", "Auto"): .272,
            ("Toronto", "Home"): .173}
    type_mix = {ptype: hist[hist["policy_type"] == ptype]["claim_type"].value_counts(normalize=True)
                for ptype in ("Auto", "Home")}
    mtl_home_mix = hist[(hist["policy_type"] == "Home") & (hist["city"] == "Montreal")]["claim_type"].value_counts(normalize=True)
    sizes = {t: hist[(hist["claim_type"] == t) & hist["claim_amount"].notna()]["claim_amount"].to_numpy()
             for t in hist["claim_type"].unique()}

    def add_claim(p, loss, ctype, amount, fast=False):
        reported = loss + pd.Timedelta(days=int(min(rng.geometric(0.33) - 1, 12)))
        team = TEAM[p["city"]]
        status, paid, closed = outcome(amount, p["deductible"], reported, team, fast)
        claims.append({"policy_id": p["policy_id"], "loss": loss, "reported": reported, "claim_type": ctype,
                       "claim_amount": round(float(amount), 2), "adjuster_team": team, "status": status,
                       "paid": paid, "closed": closed})

    for _, p in all_pol.iterrows():
        if p["source"] in ("B07", "B11"):
            continue
        lo, hi = max(p["start"], NEW_START), min(p["end"], Q_END + pd.Timedelta(days=1))
        exposure = (hi - lo).days / 365
        if exposure <= 0:
            continue
        for _ in range(rng.poisson(freq[(p["city"], p["policy_type"])] * exposure)):
            loss = lo + pd.Timedelta(days=int(rng.integers(0, max(1, (hi - lo).days))))
            mix = mtl_home_mix if (p["city"], p["policy_type"]) == ("Montreal", "Home") else type_mix[p["policy_type"]]
            ctype = rng.choice(mix.index, p=mix.values)
            add_claim(p, loss, ctype, rng.choice(sizes[ctype]) * rng.uniform(0.92, 1.08))

    # Demo event 2: ice storm in Calgary, December 8-10
    calgary_auto = all_pol[(all_pol["city"] == "Calgary") & (all_pol["policy_type"] == "Auto") &
                           (all_pol["start"] <= "2026-12-08") & (all_pol["end"] > "2026-12-10")]
    for _, p in calgary_auto.sample(16, random_state=7).iterrows():
        loss = pd.Timestamp("2026-12-08") + pd.Timedelta(days=int(rng.integers(0, 3)))
        add_claim(p, loss, "Collision", rng.uniform(3500, 12500))

    # B11 and B07: theft claims priced just under $10,000, within weeks of the policy starting
    for _, p in all_pol[all_pol["source"].isin(["B11", "B07"]) & (all_pol["start"] >= NEW_START)].iterrows():
        if rng.random() < (0.45 if p["source"] == "B11" else 0.67):
            loss = p["start"] + pd.Timedelta(days=int(rng.integers(6, 46)))
            if loss <= Q_END - pd.Timedelta(days=2):
                add_claim(p, loss, "Theft", rng.uniform(8300, 9950), fast=True)

    new_clm = pd.DataFrame(claims)
    new_clm = new_clm[new_clm["reported"] <= Q_END]  # not yet reported when the extract is taken
    new_clm = new_clm.sort_values(["reported", "loss"]).reset_index(drop=True)
    new_clm["claim_id"] = [f"CLM-{848 + i:05d}" for i in range(len(new_clm))]

    # October claims and older open claims, re-sent with their year-end status
    loss_dates = pd.Series(P.parse_dates(P._trim(raw_c["loss_date"]))[0])
    open_ids = set(clm.loc[clm["is_open"], "claim_id"])
    resend = raw_c[((loss_dates >= Q_START) | raw_c["claim_id"].isin(open_ids))].drop_duplicates().copy()
    for i, r in resend.iterrows():
        if r["claim_id"] in open_ids:
            c = clm[clm["claim_id"] == r["claim_id"]].iloc[0]
            status, paid, closed = outcome(c["claim_amount"], c["deductible"], c["reported_date"], c["adjuster_team"])
            if closed is not None and closed <= P.EXTRACTION_DATE:  # already past: close it in November instead
                closed = P.EXTRACTION_DATE + pd.Timedelta(days=int(rng.integers(3, 40)))
            resend.loc[i, ["status", "paid_amount", "closed_date"]] = [
                status, None if paid is None else f"{paid:.2f}", None if closed is None else d(closed)]

    # --------------------------------------------------- write, with mess
    def city_text(city):
        return rng.choice(CITY_VARIANTS[city]) if rng.random() < 0.05 else city

    pol_out = pd.DataFrame({
        "policy_id": new_pol["policy_id"], "customer_id": new_pol["customer_id"],
        "customer_age": [("" if pd.isna(a) else str(int(a))) for a in new_pol["customer_age"]],
        "city": [city_text(c) for c in new_pol["city"]], "policy_type": new_pol["policy_type"],
        "risk_band": new_pol["risk_band"], "channel": new_pol["channel"],
        "broker_code": new_pol["broker_code"].fillna(""),
        "start_date": [d(s, messy=rng.random() < 0.02) for s in new_pol["start"]],
        "deductible": new_pol["deductible"].astype(str),
        "annual_premium": [(f"${x:,.2f}" if rng.random() < 0.03 else f"{x:.2f}") for x in new_pol["premium"]],
        "renewal_status": new_pol["renewal_status"],
    })
    test = pd.DataFrame([{"policy_id": "TEST-5", "customer_id": "CU00000", "customer_age": "30", "city": "Toronto",
                          "policy_type": "Auto", "risk_band": "Low", "channel": "Online", "broker_code": "",
                          "start_date": "2026-11-15", "deductible": "1000", "annual_premium": "0.0",
                          "renewal_status": "In Force"}])
    pol_file = pd.concat([october_raw.fillna(""), pol_out, test], ignore_index=True)[P.POLICY_COLUMNS]

    clm_out = pd.DataFrame({
        "claim_id": new_clm["claim_id"], "policy_id": new_clm["policy_id"],
        "loss_date": [d(x, messy=rng.random() < 0.02) for x in new_clm["loss"]],
        "reported_date": [d(x) for x in new_clm["reported"]], "claim_type": new_clm["claim_type"],
        "claim_amount": [f"{x:.2f}" for x in new_clm["claim_amount"]], "adjuster_team": new_clm["adjuster_team"],
        "status": new_clm["status"], "paid_amount": ["" if pd.isna(x) else f"{x:.2f}" for x in new_clm["paid"]],
        "closed_date": ["" if pd.isna(x) else d(x) for x in new_clm["closed"]],
    })
    bad = clm_out.iloc[[5]].copy()                    # a claim for a policy that doesn't exist
    bad["claim_id"], bad["policy_id"] = f"CLM-{848 + len(new_clm):05d}", "POL-99999"
    clm_out.loc[12, "reported_date"] = d(pd.Timestamp(clm_out.loc[12, "loss_date"] if "-" in clm_out.loc[12, "loss_date"]
                                                      else pd.to_datetime(clm_out.loc[12, "loss_date"], dayfirst=True))
                                         - pd.Timedelta(days=3))   # reported before the loss
    dups = clm_out.iloc[[20, 40]]                     # two exact duplicate rows
    clm_file = pd.concat([resend.fillna(""), clm_out, bad, dups], ignore_index=True)[P.CLAIM_COLUMNS]

    # Calibration: ordinary business (no storm, no B07/B11) against the last four real quarters
    normal = new_clm[~new_clm["policy_id"].isin(all_pol.loc[all_pol["source"].isin(["B07", "B11"]), "policy_id"])]
    storm = (normal["claim_type"] == "Collision") & normal["loss"].between("2026-12-08", "2026-12-10") & \
        normal["policy_id"].isin(calgary_auto["policy_id"])
    normal = normal[~storm.to_numpy()]
    ded = normal["policy_id"].map(all_pol.set_index("policy_id")["deductible"])
    cost = np.where(normal["status"].isin(["Open", "Under Review"]), (normal["claim_amount"] - ded).clip(lower=0),
                    normal["paid"].fillna(0))
    prem = pd.concat([pol[["policy_id", "premium", "start", "end"]], new_pol[["policy_id", "premium", "start", "end"]]])
    prem = prem[~prem["policy_id"].isin(all_pol.loc[all_pol["source"].isin(["B07", "B11"]), "policy_id"])]
    lo = prem["start"].where(prem["start"] > NEW_START, NEW_START)
    hi = prem["end"].where(prem["end"] < Q_END + pd.Timedelta(days=1), Q_END + pd.Timedelta(days=1))
    earned = (prem["premium"] * ((hi - lo).dt.days.clip(lower=0)) / 365).sum()
    stats = {"seed": seed, "normal_loss_ratio": float(cost.sum() / earned), "normal_severity": float(np.mean(cost)),
             "normal_claims": int(len(normal)), "b11_claims": int(new_clm["policy_id"].isin(
                 all_pol.loc[all_pol["source"] == "B11", "policy_id"]).sum())}
    return pol_file, clm_file, stats, (len(october_raw), len(pol_out), int((new_pol["broker_code"] == "B11").sum()),
                                       len(resend), len(clm_out))


def write(seed):
    loaded = load_clean()
    pol_file, clm_file, stats, n = build(seed, loaded)
    OUT.mkdir(parents=True, exist_ok=True)
    pol_file.to_csv(OUT / "policies_2026Q4.csv", index=False)
    clm_file.to_csv(OUT / "claims_2026Q4.csv", index=False)
    print(f"seed {seed}: ordinary business loss ratio {stats['normal_loss_ratio']:.0%}, "
          f"average claim ${stats['normal_severity']:,.0f}, B11 claims {stats['b11_claims']}")
    print(f"policies_2026Q4.csv: {len(pol_file)} rows ({n[0]} re-sent from October, {n[1]} new incl. {n[2]} from B11, "
          f"1 test row)")
    print(f"claims_2026Q4.csv:   {len(clm_file)} rows ({n[3]} re-sent/updated, {n[4]} new, 1 orphan, 2 duplicates)")


def search(seeds=range(1, 61), target_lr=0.64, target_sev=4050):
    """Pick the draw whose ordinary business looks most like the last four real quarters
    (loss ratio 64%, average claim about $4,050 excluding B07), so the demo isolates the two events."""
    loaded = load_clean()
    scored = []
    for s in seeds:
        stats = build(s, loaded)[2]
        score = abs(stats["normal_loss_ratio"] - target_lr) / target_lr + abs(stats["normal_severity"] - target_sev) / target_sev
        scored.append((score, stats))
    scored.sort(key=lambda x: x[0])
    for score, st in scored[:5]:
        print(f"seed {st['seed']:3d}  score {score:.3f}  loss ratio {st['normal_loss_ratio']:.0%}  "
              f"avg claim ${st['normal_severity']:,.0f}  claims {st['normal_claims']}  B11 claims {st['b11_claims']}")
    return scored[0][1]["seed"]


if __name__ == "__main__":
    if "--search" in sys.argv:
        print("best seed:", search())
    else:
        write(SEED)
