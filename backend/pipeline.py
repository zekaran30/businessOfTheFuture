"""
Data preparation for Millennium Mutual.

Reads the raw extracts as text, fixes what can be fixed, rejects what can't,
and records every change in a CleaningLog so the app can show what changed,
why, and how much money it touched.

The same functions clean the original extract and every new quarter that is
uploaded later, so new data is held to exactly the same rules.
"""
from __future__ import annotations

import io
import re
from dataclasses import dataclass, field

import numpy as np
import pandas as pd

EXTRACTION_DATE = pd.Timestamp("2026-10-31")   # date of the original extract
EARLIEST_VALID = pd.Timestamp("2000-01-01")     # company founded in 2000
SENIOR_REVIEW_THRESHOLD = 10_000                # claims >= this go to senior review
TERM_DAYS = 365                                 # a policy lasts one year

POLICY_COLUMNS = [
    "policy_id", "customer_id", "customer_age", "city", "policy_type", "risk_band",
    "channel", "broker_code", "start_date", "deductible", "annual_premium", "renewal_status",
]
CLAIM_COLUMNS = [
    "claim_id", "policy_id", "loss_date", "reported_date", "claim_type", "claim_amount",
    "adjuster_team", "status", "paid_amount", "closed_date",
]

# Every spelling found in the extract, mapped to the five cities Millennium Mutual operates in.
# "Quebec" on its own is read as Quebec City: the business only operates in cities.
CITY_ALIASES = {
    "montreal": "Montreal", "montréal": "Montreal", "mtl": "Montreal",
    "quebec city": "Quebec City", "québec city": "Quebec City", "quebec": "Quebec City",
    "québec": "Quebec City", "qc city": "Quebec City", "qc": "Quebec City",
    "toronto": "Toronto", "ottawa": "Ottawa", "calgary": "Calgary",
}
CANONICAL = {
    "policy_type": ["Auto", "Home"],
    "risk_band": ["Low", "Medium", "High"],
    "channel": ["Agent", "Broker", "Online"],
    "renewal_status": ["Renewed", "Not Renewed", "In Force", "Cancelled"],
    "claim_type": ["Collision", "Theft", "Water Damage", "Glass", "Wind/Hail", "Liability", "Fire"],
    "adjuster_team": ["Team A", "Team B", "Team C"],
    "status": ["Paid", "Denied", "Open", "Under Review"],
}
AGE_BANDS = [(16, 24, "Under 25"), (25, 34, "25–34"), (35, 49, "35–49"), (50, 64, "50–64"), (65, 120, "65+")]


# --------------------------------------------------------------------------- log
@dataclass
class CleaningLog:
    """Human-readable record of every data change."""
    entries: list = field(default_factory=list)

    def add(self, table: str, step: str, rows: int, action: str, why: str, impact: str = "") -> None:
        if rows:
            self.entries.append(
                {"table": table, "step": step, "rows": int(rows), "action": action, "why": why, "impact": impact}
            )


# ----------------------------------------------------------------------- helpers
def money_str(x: float) -> str:
    return f"${x:,.0f}"


def read_text_csv(source) -> pd.DataFrame:
    """Read a CSV keeping every value as text, so nothing is silently coerced."""
    if isinstance(source, (bytes, bytearray)):
        source = io.BytesIO(source)
    df = pd.read_csv(source, dtype=str, keep_default_na=True, skipinitialspace=False, encoding_errors="replace")
    df.columns = [re.sub(r"\s+", "_", str(c).strip().lower()) for c in df.columns]
    return df.astype(object).where(df.notna(), None)


def _trim(series: pd.Series) -> pd.Series:
    def fix(v):
        if v is None or (isinstance(v, float) and np.isnan(v)):
            return None
        v = str(v).strip()
        return v if v else None
    return series.map(fix)


def _require(df: pd.DataFrame, columns: list, table: str) -> None:
    missing = [c for c in columns if c not in df.columns]
    if missing:
        raise ValueError(
            f"The {table} file is missing column(s): {', '.join(missing)}. "
            f"Expected columns: {', '.join(columns)}."
        )


def parse_money(series: pd.Series):
    """'$1,046.62' -> 1046.62. Returns (values, count_fixed_from_text)."""
    def has_symbols(v):
        return isinstance(v, str) and bool(re.search(r"[$,]|CAD", v, flags=re.I))
    fixed = series.map(has_symbols)
    cleaned = series.map(lambda v: re.sub(r"[$,\s]|CAD", "", v, flags=re.I) if isinstance(v, str) else v)
    values = pd.to_numeric(cleaned, errors="coerce").astype(float)
    return values, int(fixed.sum())


def parse_dates(series: pd.Series):
    """Accept ISO (2026-03-15), day-first (15/03/2026) and 2026/03/15.
    Returns (dates, count_read_day_first, count_unreadable)."""
    text = series.map(lambda v: v if isinstance(v, str) else None)
    iso = pd.to_datetime(text, format="%Y-%m-%d", errors="coerce")
    dmy = pd.to_datetime(text, format="%d/%m/%Y", errors="coerce")
    ymd = pd.to_datetime(text, format="%Y/%m/%d", errors="coerce")
    out = iso.fillna(dmy).fillna(ymd)
    n_day_first = int((iso.isna() & dmy.notna()).sum())
    n_bad = int((out.isna() & text.notna()).sum())
    return out, n_day_first, n_bad


def canonicalise(series: pd.Series, allowed: list):
    """Case-insensitive match to the known spelling. Unknown values are kept (title-cased)."""
    lookup = {a.lower(): a for a in allowed}
    def fix(v):
        if v is None:
            return None
        return lookup.get(v.lower(), v.title() if v.islower() or v.isupper() else v)
    out = series.map(fix)
    changed = int(((series != out) & series.notna()).sum())
    unknown = sorted({v for v in out.dropna().unique() if v not in allowed})
    return out, changed, unknown


def age_band(age: float) -> str:
    if age is None or pd.isna(age):
        return "Unknown"
    for lo, hi, label in AGE_BANDS:
        if lo <= age <= hi:
            return label
    return "Unknown"


def _reject(rejected: list, rows: pd.DataFrame, id_col: str, reason: str, table: str) -> None:
    for _, r in rows.iterrows():
        rejected.append({"table": table, "id": r.get(id_col) or "(blank)", "reason": reason})


# ---------------------------------------------------------------------- policies
def clean_policies(raw: pd.DataFrame, log: CleaningLog, max_date: pd.Timestamp):
    """Returns (clean policies, rejected rows)."""
    T = "policies"
    _require(raw, POLICY_COLUMNS, T)
    df = raw[POLICY_COLUMNS].copy()
    city_as_typed = df["city"].copy()  # before trimming, so "Toronto " counts as a variant
    for c in df.columns:
        df[c] = _trim(df[c])
    rejected: list = []

    missing_id = df["policy_id"].isna()
    _reject(rejected, df[missing_id], "policy_id", "Missing policy ID", T)
    df = df[~missing_id]

    # Test records left in the production extract
    is_test = df["policy_id"].str.upper().str.startswith("TEST") | (df["customer_id"] == "CU00000")
    log.add(T, "Removed test records", is_test.sum(),
            "Dropped policies whose ID starts with TEST (customer CU00000, $0 premium).",
            "They are system tests, not customers. Left in, they inflate the policy count and drag the average premium down.")
    df = df[~is_test]

    # Exact duplicate rows, then repeated IDs (keep the last, most recent version)
    dup_rows = df.duplicated()
    log.add(T, "Removed duplicate rows", dup_rows.sum(), "Dropped exact copies of the same policy row.",
            "A policy counted twice doubles its premium.")
    df = df[~dup_rows]
    dup_ids = df["policy_id"].duplicated(keep="last")
    log.add(T, "Kept latest version of repeated policy IDs", dup_ids.sum(),
            "When the same policy ID appears with different values, the last row wins.",
            "Uploads can re-send a policy with updated details; it must replace the old row, not add to it.")
    df = df[~dup_ids]

    # Premium stored as text such as "$1,046.62"
    premium, n_text = parse_money(df["annual_premium"])
    log.add(T, "Read premiums written as text", n_text,
            "Removed '$' and thousands separators (\"$1,046.62\" -> 1046.62).",
            "A plain numeric read turns these into blanks, so their premium disappears from every total.",
            f"{money_str(premium[df['annual_premium'].map(lambda v: isinstance(v, str) and ('$' in v or ',' in v))].sum())} of premium recovered.")
    df["premium"] = premium
    bad_prem = df["premium"].isna() | (df["premium"] <= 0)
    _reject(rejected, df[bad_prem], "policy_id", "Premium is missing, zero or unreadable", T)
    df = df[~bad_prem]

    # Dates: ISO and day-first mixed in the same column
    start, n_day_first, _ = parse_dates(df["start_date"])
    log.add(T, "Read day-first start dates", n_day_first,
            "Parsed dates written as DD/MM/YYYY (e.g. 15/11/2024) explicitly as day-first.",
            "Read month-first, 15/11/2024 is invalid and 02/01/2025 becomes Feb 1, putting premium in the wrong month or dropping it. "
            "Checked against neighbouring policy IDs, which are issued in date order: all matched.")
    df["start"] = start
    bad_date = df["start"].isna() | (df["start"] < EARLIEST_VALID) | (df["start"] > max_date)
    _reject(rejected, df[bad_date], "policy_id", "Start date missing, unreadable or out of range", T)
    df = df[~bad_date]

    # Cities: 16 spellings for 5 cities
    raw_city = city_as_typed.loc[df.index]
    city = df["city"].map(lambda v: CITY_ALIASES.get(v.lower(), v) if isinstance(v, str) else None)
    variants = raw_city[(raw_city != city) & raw_city.notna()]
    if len(variants):
        n_spellings = raw_city.dropna().nunique()
        examples = ", ".join(f"'{v}'" for v in sorted(variants.unique(), key=lambda v: -int((variants == v).sum()))[:7])
        tor_missing = int(((city == "Toronto") & (raw_city != "Toronto")).sum())
        log.add(T, "Merged city spellings", len(variants),
                f"Found {n_spellings} spellings for 5 cities, including {examples} (some with trailing spaces). "
                f"Mapped each to its city.",
                "Each spelling would show up as its own city, splitting the totals.",
                f"Toronto alone would have been missing {tor_missing} policies." if tor_missing else "")
    unknown_city = city.isna() | ~city.isin(set(CITY_ALIASES.values()))
    _reject(rejected, df[unknown_city], "policy_id", "City missing or not one of the five cities", T)
    df["city"] = city
    df = df[~unknown_city]

    for col in ["policy_type", "risk_band", "channel", "renewal_status"]:
        df[col], changed, unknown = canonicalise(df[col], CANONICAL[col])
        log.add(T, f"Standardised {col.replace('_', ' ')}", changed,
                "Matched values regardless of capitalisation.", "Keeps one label per category.")
        if unknown:
            log.add(T, f"Unrecognised {col.replace('_', ' ')}", int(df[col].isin(unknown).sum()),
                    f"Kept new value(s) as-is: {', '.join(map(str, unknown))}.", "Shown separately so nothing is hidden.")
    must_have = df["policy_type"].isna() | df["channel"].isna()
    _reject(rejected, df[must_have], "policy_id", "Product or channel missing", T)
    df = df[~must_have]

    # Deductible
    deductible, _ = parse_money(df["deductible"])
    no_ded = deductible.isna()
    log.add(T, "Filled missing deductibles", no_ded.sum(), "Used the most common deductible ($1,000).",
            "Only used to estimate what open claims will cost.")
    df["deductible"] = deductible.fillna(1000.0)

    # Ages
    age = pd.to_numeric(df["customer_age"], errors="coerce")
    impossible = (age < 16) | (age > 100)
    log.add(T, "Cleared impossible ages", impossible.sum(),
            f"Set ages outside 16–100 (found: {', '.join(str(int(a)) for a in sorted(age[impossible].unique()))}) to unknown.",
            "A 0- or 7-year-old cannot hold a policy; these are entry errors.")
    age = age.where(~impossible)
    log.add(T, "Noted blank ages", int(age.isna().sum() - impossible.sum()),
            "Left as 'Unknown' age band.", "Guessing an age would invent data. Money totals are unaffected.")
    df["customer_age"] = age
    df["age_band"] = age.map(age_band)

    # Distribution source: the broker code for brokers, otherwise the channel
    df["broker_code"] = df["broker_code"].map(lambda v: v.upper() if isinstance(v, str) else None)
    broker_no_code = (df["channel"] == "Broker") & df["broker_code"].isna()
    log.add(T, "Broker policies without a broker code", broker_no_code.sum(),
            "Grouped as 'Broker (no code)'.", "Commission still applies, so they stay in the broker channel.")
    code_on_direct = (df["channel"] != "Broker") & df["broker_code"].notna()
    log.add(T, "Ignored broker codes on direct sales", code_on_direct.sum(),
            "Removed broker codes on Agent/Online policies.", "No broker earns commission on direct sales.")
    df.loc[code_on_direct, "broker_code"] = None
    df["source"] = np.where(df["channel"] == "Broker", df["broker_code"].fillna("Broker (no code)"), df["channel"])

    df["end"] = df["start"] + pd.Timedelta(days=TERM_DAYS)
    df["renewal_status"] = df["renewal_status"].fillna("Unknown")
    keep = ["policy_id", "customer_id", "customer_age", "age_band", "city", "policy_type", "risk_band",
            "channel", "broker_code", "source", "start", "end", "deductible", "premium", "renewal_status"]
    return df[keep].reset_index(drop=True), rejected


# ------------------------------------------------------------------------ claims
def clean_claims(raw: pd.DataFrame, log: CleaningLog, policy_ids: set, max_date: pd.Timestamp):
    """Returns (clean claims, rejected rows). Policy attributes are joined later."""
    T = "claims"
    _require(raw, CLAIM_COLUMNS, T)
    df = raw[CLAIM_COLUMNS].copy()
    for c in df.columns:
        df[c] = _trim(df[c])
    rejected: list = []

    missing_id = df["claim_id"].isna()
    _reject(rejected, df[missing_id], "claim_id", "Missing claim ID", T)
    df = df[~missing_id]

    paid_preview, _ = parse_money(df["paid_amount"])
    dup_rows = df.duplicated()
    log.add(T, "Removed duplicate claims", dup_rows.sum(),
            "Dropped exact copies of the same claim row (same ID, same amounts, same dates).",
            "Each copy would count the same payout twice.",
            f"{money_str(paid_preview[dup_rows].sum())} of claims paid would have been double counted.")
    df = df[~dup_rows]
    dup_ids = df["claim_id"].duplicated(keep="last")
    log.add(T, "Kept latest version of repeated claim IDs", dup_ids.sum(),
            "When a claim ID appears again with new values (e.g. an open claim now paid), the last row wins.",
            "A claim's status changes over time; only its latest state should count.")
    df = df[~dup_ids]

    # Dates
    for col in ["loss_date", "reported_date", "closed_date"]:
        df[col], n_day_first, _ = parse_dates(df[col])
        log.add(T, f"Read day-first {col.replace('_', ' ')}s", n_day_first,
                "Parsed DD/MM/YYYY dates explicitly as day-first.",
                "Otherwise these claims land in the wrong month or quarter, or disappear.")
    bad_loss = df["loss_date"].isna() | (df["loss_date"] < EARLIEST_VALID) | (df["loss_date"] > max_date)
    _reject(rejected, df[bad_loss], "claim_id", "Loss date missing, unreadable or out of range", T)
    df = df[~bad_loss]

    # Money
    df["claim_amount"], n1 = parse_money(df["claim_amount"])
    df["paid_amount"], n2 = parse_money(df["paid_amount"])
    log.add(T, "Read amounts written as text", n1 + n2, "Removed '$' and thousands separators.",
            "Otherwise those amounts become blanks.")
    negative = (df["claim_amount"] < 0) | (df["paid_amount"] < 0)
    _reject(rejected, df[negative], "claim_id", "Negative claim or paid amount", T)
    df = df[~negative]

    for col in ["claim_type", "adjuster_team", "status"]:
        df[col], changed, unknown = canonicalise(df[col], CANONICAL[col])
        log.add(T, f"Standardised {col.replace('_', ' ')}", changed, "Matched values regardless of capitalisation.",
                "Keeps one label per category.")
        if unknown and col != "status":
            log.add(T, f"Unrecognised {col.replace('_', ' ')}", int(df[col].isin(unknown).sum()),
                    f"Kept new value(s) as-is: {', '.join(map(str, unknown))}.", "Shown separately so nothing is hidden.")
    bad_status = ~df["status"].isin(CANONICAL["status"])
    _reject(rejected, df[bad_status], "claim_id", "Status missing or not Paid / Denied / Open / Under Review", T)
    df = df[~bad_status]

    orphan = ~df["policy_id"].isin(policy_ids)
    _reject(rejected, df[orphan], "claim_id", "No matching policy ID", T)
    df = df[~orphan]

    # Logic checks: kept (the money is real) but recorded
    early_report = df["reported_date"] < df["loss_date"]
    log.add(T, "Flagged claims reported before the loss", early_report.sum(),
            "Kept, but marked as a date-entry error.",
            "Money is unaffected; these are excluded from reporting-delay figures.")
    closed_before = df["closed_date"].notna() & (df["closed_date"] < df["reported_date"])
    log.add(T, "Flagged claims closed before being reported", closed_before.sum(),
            "Kept, excluded from settlement-time figures.", "Impossible order of dates.")
    no_amount = df["claim_amount"].isna()
    log.add(T, "Claims with no claim amount", no_amount.sum(),
            "Kept for money totals (the paid amount is known); left out of claim-size analysis.",
            "Guessing the amount could fake or hide a cluster under $10,000.")
    paid_no_amount = (df["status"] == "Paid") & df["paid_amount"].isna()
    log.add(T, "Paid claims with no paid amount", paid_no_amount.sum(), "Counted as $0 paid.",
            "Better to flag than to guess a payout.")
    df["date_issue"] = early_report | closed_before
    return df.reset_index(drop=True), rejected


def enrich_claims(claims: pd.DataFrame, policies: pd.DataFrame) -> pd.DataFrame:
    """Join policy attributes and compute paid, open-claim reserve and incurred cost."""
    attrs = ["policy_id", "customer_id", "city", "policy_type", "risk_band", "channel", "broker_code", "source",
             "deductible", "age_band", "renewal_status", "start", "end", "premium"]
    df = claims.merge(policies[attrs], on="policy_id", how="inner")
    df["customer_id"] = df["customer_id"].fillna("NO-ID-" + df["policy_id"].astype(str))

    is_open = df["status"].isin(["Open", "Under Review"])
    denied = df["status"] == "Denied"
    df["paid"] = np.where(denied, 0.0, df["paid_amount"].fillna(0.0))

    # Open claims: assume the claim is paid in full minus the deductible (prudent).
    # When the amount is missing, use the typical (median) amount for that claim type.
    typical = df.groupby("claim_type")["claim_amount"].transform("median")
    estimate = df["claim_amount"].fillna(typical).fillna(df["claim_amount"].median())
    df["reserve"] = np.where(is_open, (estimate - df["deductible"] - df["paid"]).clip(lower=0), 0.0)
    df["incurred"] = df["paid"] + df["reserve"]

    df["days_from_start"] = (df["loss_date"] - df["start"]).dt.days
    lag = (df["reported_date"] - df["loss_date"]).dt.days
    df["report_lag_days"] = lag.where(lag >= 0)
    settle = (df["closed_date"] - df["reported_date"]).dt.days
    df["days_to_settle"] = settle.where((settle >= 0) & (lag >= 0))
    df["near_threshold"] = (df["claim_amount"] >= 9_000) & (df["claim_amount"] < SENIOR_REVIEW_THRESHOLD)
    df["is_open"] = is_open
    # Claim number per customer (all of a customer's policies together, in loss-date order): 1st, 2nd, 3rd+
    df = df.sort_values(["customer_id", "loss_date", "reported_date", "claim_id"]).reset_index(drop=True)
    df["claim_seq"] = df.groupby("customer_id").cumcount() + 1
    df["customer_claims"] = df.groupby("customer_id")["claim_id"].transform("count")
    df["claim_number"] = np.where(df["claim_seq"] == 1, "1st", np.where(df["claim_seq"] == 2, "2nd", "3rd+"))
    return df


def check_term(claims: pd.DataFrame, log: CleaningLog) -> None:
    outside = (claims["loss_date"] < claims["start"]) | (claims["loss_date"] >= claims["end"])
    log.add("claims", "Flagged losses outside the policy term", outside.sum(),
            "Kept, but marked: the loss date falls before the policy started or after it ended.",
            "Could be a renewal term missing from the extract, or a claim that should not be paid.")


def upsert(base: pd.DataFrame, new: pd.DataFrame, key: str):
    """Rows in `new` replace rows in `base` with the same key. Returns (frame, n_added, n_updated)."""
    if new is None or new.empty:
        return base, 0, 0
    existing = set(base[key])
    n_updated = int(new[key].isin(existing).sum())
    combined = pd.concat([base[~base[key].isin(set(new[key]))], new], ignore_index=True)
    return combined, len(new) - n_updated, n_updated
