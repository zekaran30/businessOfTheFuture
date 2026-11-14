"""
Holds the data in memory, rebuilds it when a new extract arrives, and carries out
the automated actions: logging alerts, building claim review lists, and sending
notifications (to an outbox, and to a chat webhook if ALERT_WEBHOOK_URL is set).
"""
from __future__ import annotations

import json
import os
import shutil
import threading
import urllib.request
import uuid
from collections import Counter
from datetime import datetime
from pathlib import Path

import numpy as np
import pandas as pd

from . import alerts as A
from . import metrics as M
from . import pipeline as P

ROOT = Path(__file__).resolve().parent.parent
DATA = ROOT / "data"
RUNTIME = ROOT / "runtime"
UPLOADS = RUNTIME / "uploads"
MANUAL_ID = "manual-entries"

DEFAULT_ASSUMPTIONS = {"expense_ratio": 0.25, "broker_commission": 0.12}


def now_iso() -> str:
    return datetime.now().isoformat(timespec="seconds")


class Store:
    def __init__(self):
        self.lock = threading.RLock()
        RUNTIME.mkdir(exist_ok=True)
        UPLOADS.mkdir(exist_ok=True)
        self.base_policies_raw = P.read_text_csv(DATA / "policies.csv")
        self.base_claims_raw = P.read_text_csv(DATA / "claims.csv")
        self.batches = self._load_batches()
        self.alerts = self._load_json("alerts.json")
        self.outbox = self._load_json("outbox.json")
        self.rebuild()
        self.watch_settings = A.normalise(self._load_json_obj("watch_settings.json"))
        self.watch_status = self._load_json_obj("watch_status.json")
        seen = self._load_json("watch_seen.json")  # alert key -> when it was first opened
        self.watch_seen = seen if isinstance(seen, dict) else {k: now_iso() for k in seen}
        self.watch_cache = None
        self.run_watch("First scan of the data")

    # ------------------------------------------------------------ persistence
    def _load_json(self, name):
        path = RUNTIME / name
        if path.exists():
            try:
                return json.loads(path.read_text())
            except json.JSONDecodeError:
                return []
        return []

    def _load_json_obj(self, name):
        obj = self._load_json(name)
        return obj if isinstance(obj, dict) else {}

    def _save_json(self, name, obj):
        (RUNTIME / name).write_text(json.dumps(obj, indent=2, default=str))

    def _load_batches(self) -> list:
        batches = []
        for d in sorted(UPLOADS.iterdir()) if UPLOADS.exists() else []:
            meta_path = d / "meta.json"
            if not meta_path.exists():
                continue
            meta = json.loads(meta_path.read_text())
            meta["policies_raw"] = P.read_text_csv(d / "policies.csv") if (d / "policies.csv").exists() else None
            meta["claims_raw"] = P.read_text_csv(d / "claims.csv") if (d / "claims.csv").exists() else None
            batches.append(meta)
        batches.sort(key=lambda b: b["uploaded_at"])
        return batches

    def _save_batch(self, batch: dict) -> None:
        d = UPLOADS / batch["id"]
        d.mkdir(parents=True, exist_ok=True)
        if batch.get("policies_raw") is not None:
            batch["policies_raw"].to_csv(d / "policies.csv", index=False)
        if batch.get("claims_raw") is not None:
            batch["claims_raw"].to_csv(d / "claims.csv", index=False)
        meta = {k: v for k, v in batch.items() if k not in ("policies_raw", "claims_raw", "report")}
        (d / "meta.json").write_text(json.dumps(meta, indent=2))

    # ---------------------------------------------------------------- rebuild
    def rebuild(self) -> None:
        """Clean the original extract, then apply each uploaded batch in order."""
        with self.lock:
            max_date = P.EXTRACTION_DATE + pd.DateOffset(years=2)
            base_log = P.CleaningLog()
            pol, base_rej = P.clean_policies(self.base_policies_raw, base_log, max_date)
            pol["batch"] = "original"
            reports = {}
            for b in self.batches:
                log = P.CleaningLog()
                rep = {"policies": None, "claims": None, "rejected": []}
                if b.get("policies_raw") is not None and len(b["policies_raw"]):
                    bp, rej = P.clean_policies(b["policies_raw"], log, max_date)
                    bp["batch"] = b["id"]
                    pol, added, updated = P.upsert(pol, bp, "policy_id")
                    rep["policies"] = {"received": len(b["policies_raw"]), "added": added, "updated": updated,
                                       "rejected": len(rej)}
                    rep["rejected"] += rej
                reports[b["id"]] = {"log": log, "rep": rep}

            ids = set(pol["policy_id"])
            clm, base_rej_c = P.clean_claims(self.base_claims_raw, base_log, ids, max_date)
            clm["batch"] = "original"
            for b in self.batches:
                if b.get("claims_raw") is not None and len(b["claims_raw"]):
                    log = reports[b["id"]]["log"]
                    bc, rej = P.clean_claims(b["claims_raw"], log, ids, max_date)
                    bc["batch"] = b["id"]
                    clm, added, updated = P.upsert(clm, bc, "claim_id")
                    reports[b["id"]]["rep"]["claims"] = {"received": len(b["claims_raw"]), "added": added,
                                                         "updated": updated, "rejected": len(rej)}
                    reports[b["id"]]["rep"]["rejected"] += rej

            clm = P.enrich_claims(clm, pol)
            P.check_term(clm[clm["batch"] == "original"], base_log)
            for b in self.batches:
                P.check_term(clm[clm["batch"] == b["id"]], reports[b["id"]]["log"])
                b["report"] = {**reports[b["id"]]["rep"], "log": reports[b["id"]]["log"].entries}

            asofs = [pd.Timestamp(b["as_of"]) for b in self.batches if b.get("as_of")]
            self.asof = max([P.EXTRACTION_DATE] + asofs)
            closed = clm["closed_date"].notna()  # days a client waited for a decision (so far, if still open)
            wait = np.where(closed, (clm["closed_date"] - clm["reported_date"]).dt.days,
                            (self.asof - clm["reported_date"]).dt.days)
            clm["wait_days"] = pd.Series(wait, index=clm.index, dtype=float).where(lambda w: w >= 0)
            self.data_start = pol["start"].min().normalize()
            self.policies, self.claims = pol, clm
            self.base_log = base_log.entries
            self.base_rejected = base_rej + base_rej_c

    # --------------------------------------------------------------- batches
    def add_batch(self, name: str, policies_raw, claims_raw, as_of: str | None) -> dict:
        """Validate structure, store the batch, rebuild. Raises ValueError on unusable files."""
        for df, cols, table in ((policies_raw, P.POLICY_COLUMNS, "policies"), (claims_raw, P.CLAIM_COLUMNS, "claims")):
            if df is not None:
                P._require(df, cols, table)
        with self.lock:
            dates = []
            if policies_raw is not None:
                d, _, _ = P.parse_dates(P._trim(policies_raw["start_date"]))
                dates += list(d.dropna())
            if claims_raw is not None:
                for col in ("loss_date", "reported_date", "closed_date"):
                    d, _, _ = P.parse_dates(P._trim(claims_raw[col]))
                    dates += list(d.dropna())
            limit = P.EXTRACTION_DATE + pd.DateOffset(years=2)
            dates = [x for x in dates if P.EARLIEST_VALID <= x <= limit]
            latest = max(dates) if dates else self.asof
            if as_of:
                as_of_ts = pd.Timestamp(as_of)
            else:  # extracts are taken at month end: use the end of the month of the latest date
                as_of_ts = (latest + pd.offsets.MonthEnd(0)).normalize()
            loss_dates = []
            if claims_raw is not None:
                d, _, _ = P.parse_dates(P._trim(claims_raw["loss_date"]))
                loss_dates = [x for x in d.dropna() if P.EARLIEST_VALID <= x <= limit]
            starts = []
            if policies_raw is not None:
                d, _, _ = P.parse_dates(P._trim(policies_raw["start_date"]))
                starts = [x for x in d.dropna() if P.EARLIEST_VALID <= x <= limit]
            anchor = max(loss_dates + starts) if (loss_dates + starts) else latest
            batch = {
                "id": f"{datetime.now():%Y%m%d-%H%M%S}-{uuid.uuid4().hex[:4]}",
                "name": name, "uploaded_at": now_iso(), "as_of": as_of_ts.strftime("%Y-%m-%d"),
                "quarter": M.quarter_of(anchor),
                "policies_raw": policies_raw, "claims_raw": claims_raw,
            }
            self.batches.append(batch)
            self._save_batch(batch)
            self.rebuild()
            return batch

    def add_manual(self, kind: str, row: dict) -> dict:
        """Append one hand-entered policy or claim to the manual-entries batch."""
        with self.lock:
            batch = next((b for b in self.batches if b["id"] == MANUAL_ID), None)
            if batch is None:
                batch = {"id": MANUAL_ID, "name": "Entered by hand", "uploaded_at": now_iso(), "as_of": None,
                         "quarter": None, "policies_raw": None, "claims_raw": None}
                self.batches.append(batch)
            cols = P.POLICY_COLUMNS if kind == "policy" else P.CLAIM_COLUMNS
            id_col = "policy_id" if kind == "policy" else "claim_id"
            frame_key = "policies_raw" if kind == "policy" else "claims_raw"
            current = batch[frame_key]
            n = 0 if current is None else len(current)
            row = {c: (None if row.get(c) in ("", None) else str(row.get(c))) for c in cols}
            if not row[id_col]:
                row[id_col] = f"{'POL' if kind == 'policy' else 'CLM'}-M{n + 1:04d}"
            new = pd.DataFrame([row], columns=cols).astype(object)
            batch[frame_key] = new if current is None else pd.concat([current, new], ignore_index=True)
            date_col = "start_date" if kind == "policy" else "loss_date"
            d, _, _ = P.parse_dates(P._trim(new[date_col]))
            if d.notna().iloc[0]:
                ts = d.iloc[0]
                month_end = (ts + pd.offsets.MonthEnd(0)).normalize()
                if batch["as_of"] is None or pd.Timestamp(batch["as_of"]) < month_end:
                    batch["as_of"] = month_end.strftime("%Y-%m-%d")
                batch["quarter"] = M.quarter_of(ts)
            self._save_batch(batch)
            self.rebuild()
            rejected = [r for r in batch["report"]["rejected"] if r["id"] == row[id_col]]
            return {"id": row[id_col], "accepted": not rejected, "rejected": rejected,
                    "quarter": batch["quarter"], "batch": batch["id"]}

    def reset(self) -> None:
        with self.lock:
            shutil.rmtree(UPLOADS, ignore_errors=True)
            UPLOADS.mkdir(parents=True, exist_ok=True)
            self.batches, self.alerts, self.outbox = [], [], []
            self._save_json("alerts.json", [])
            self._save_json("outbox.json", [])
            self.watch_settings, self.watch_status, self.watch_seen = A.normalise({}), {}, {}
            for name, obj in (("watch_settings.json", {}), ("watch_status.json", {}), ("watch_seen.json", {})):
                self._save_json(name, obj)
            self.rebuild()
            self.run_watch("First scan after reset")

    # --------------------------------------------------------------- actions
    def log_check(self, result: dict, trigger: str) -> dict:
        """The automated response to a check: log alerts, route review lists, notify owners."""
        with self.lock:
            created, updated = [], []
            for f in result["flags"]:
                existing = next((a for a in self.alerts if a["key"] == f["id"]), None)
                earlier = sorted({a["quarter"] for a in self.alerts
                                  if a["lens"] == f["lens"] and a["segment"] == f["segment"] and a["quarter"] != result["quarter"]})
                record = {
                    "key": f["id"], "quarter": result["quarter"], "quarter_label": result["label"],
                    "lens": f["lens"], "lens_label": f["lens_label"], "segment": f["segment"],
                    "severity": f["severity"], "status_in_data": f["status"], "reasons": f["reasons"],
                    "drivers": f["drivers"], "action": f["action"], "owners": f["owners"],
                    "metrics": f["metrics"], "shortfall": f["shortfall"], "review": f["review"],
                    "flagged_before": earlier, "trigger": trigger,
                }
                if existing:
                    existing.update(record)
                    existing["updated_at"] = now_iso()
                    updated.append(existing)
                else:
                    record.update({"id": uuid.uuid4().hex[:10], "created_at": now_iso(), "updated_at": now_iso(),
                                   "workflow": "New"})
                    self.alerts.append(record)
                    created.append(record)

            messages = self._notify(result, created, trigger)
            self._save_json("alerts.json", self.alerts)
            self._save_json("outbox.json", self.outbox)
            routed = sum(len(a["review"]) for a in created + updated)
            return {"alerts_created": len(created), "alerts_updated": len(updated), "claims_routed": routed,
                    "messages": messages, "created_ids": [a["id"] for a in created]}

    def _notify(self, result: dict, created: list, trigger: str) -> list:
        """One message per owner, only for alerts that are new (no repeat spam)."""
        by_owner: dict = {}
        for a in created:
            for owner in a["owners"]:
                by_owner.setdefault(owner, []).append(a)
        messages = []
        for owner, items in by_owner.items():
            items = sorted(items, key=lambda a: {"critical": 0, "high": 1, "watch": 2}[a["severity"]])
            subject = f"{result['label']} check: {len(items)} new alert{'s' if len(items) != 1 else ''}"
            lines = [result["headline"], ""]
            for a in items:
                lines.append(f"[{a['severity'].upper()}] {a['segment']} ({a['lens_label'].lower()})")
                lines += [f"  - {r['text']}" for r in a["reasons"]]
                lines.append(f"  Next step: {a['action']}")
                if a["review"]:
                    lines.append(f"  {len(a['review'])} claim(s) attached for senior review.")
                lines.append("")
            msg = {"id": uuid.uuid4().hex[:10], "sent_at": now_iso(), "to": owner, "subject": subject,
                   "body": "\n".join(lines).strip(), "trigger": trigger, "delivered": "outbox"}
            url = os.environ.get("ALERT_WEBHOOK_URL")
            if url:
                try:
                    req = urllib.request.Request(url, data=json.dumps({"text": f"*{subject}* (to {owner})\n{msg['body']}"}).encode(),
                                                 headers={"Content-Type": "application/json"})
                    urllib.request.urlopen(req, timeout=5)
                    msg["delivered"] = "outbox + webhook"
                except Exception as exc:  # never let a chat outage break the check
                    msg["delivered"] = f"outbox (webhook failed: {exc.__class__.__name__})"
            self.outbox.insert(0, msg)
            messages.append(msg)
        return messages

    def set_alert_workflow(self, alert_id: str, workflow: str) -> dict | None:
        with self.lock:
            for a in self.alerts:
                if a["id"] == alert_id:
                    a["workflow"] = workflow
                    a["updated_at"] = now_iso()
                    self._save_json("alerts.json", self.alerts)
                    return a
        return None

    # ------------------------------------------------------- claim + retention alerts
    def run_watch(self, trigger: str, notify: bool = True) -> dict:
        """Re-evaluate every claim against the alert rules; message owners about alerts not seen before."""
        with self.lock:
            s = A.normalise(self.watch_settings)
            claims, retention = A.claim_alerts(self.claims, s), A.retention_alerts(self.claims, s)
            seen = dict(self.watch_seen)
            first = not seen
            stamp = now_iso()
            new_c = [a for a in claims if a["key"] not in seen]
            new_r = [a for a in retention if a["key"] not in seen]
            messages = self._notify_watch(trigger, new_c, new_r, first) if notify and (new_c or new_r) else []
            for a in claims + retention:
                a["opened_at"] = seen.get(a["key"], stamp)
            seen.update({a["key"]: stamp for a in new_c + new_r})
            self.watch_seen = seen
            self._save_json("watch_seen.json", self.watch_seen)
            self.watch_cache = {"settings": s, "claims": claims, "retention": retention,
                                "summary": A.summarise(claims, retention), "ran_at": now_iso(), "trigger": trigger,
                                "new_keys": sorted({a["key"] for a in new_c + new_r})}
            return {"claim_new": len(new_c), "retention_new": len(new_r),
                    "claim_new_by_level": {str(l): sum(1 for a in new_c if a["level"] == l) for l in (1, 2, 3, 4)},
                    "retention_new_by_priority": {p: sum(1 for a in new_r if a["priority"] == p) for p in A.PRIORITIES},
                    "messages": messages}

    def _deliver(self, msg: dict) -> dict:
        url = os.environ.get("ALERT_WEBHOOK_URL")
        if url:
            try:
                req = urllib.request.Request(url, data=json.dumps({"text": f"*{msg['subject']}* (to {msg['to']})\n{msg['body']}"}).encode(),
                                             headers={"Content-Type": "application/json"})
                urllib.request.urlopen(req, timeout=5)
                msg["delivered"] = "outbox + webhook"
            except Exception as exc:
                msg["delivered"] = f"outbox (webhook failed: {exc.__class__.__name__})"
        self.outbox.insert(0, msg)
        return msg

    def _notify_watch(self, trigger, new_c, new_r, first) -> list:
        prefix = "First scan: " if first else ""
        out = []

        def message(to, subject, lines):
            return self._deliver({"id": uuid.uuid4().hex[:10], "sent_at": now_iso(), "to": to, "subject": subject,
                                  "body": "\n".join(lines).strip(), "trigger": trigger, "delivered": "outbox"})
        if new_c:
            counts = Counter(a["level"] for a in new_c)
            lines = [f"{len(new_c)} claim(s) need a look ({trigger}).",
                     ", ".join(f"Level {l} {A.LEVEL_LABEL[l]}: {counts[l]}" for l in (1, 2, 3, 4) if counts[l]), ""]
            for a in new_c[:12]:
                lines.append(f"[Level {a['level']}] {a['claim_id']} {a['source']} {a['city']} {a['policy_type']}, "
                             f"{a['claim_type']} {A.money(a['claim_amount'])}: {'; '.join(a['reasons'])}")
            if len(new_c) > 12:
                lines.append(f"... and {len(new_c) - 12} more in Alerts > Claim alerts.")
            out.append(message("VP of Claims", f"{prefix}{len(new_c)} new claim alert{'s' if len(new_c) != 1 else ''}"
                               + (f", {counts[1]} critical" if counts[1] else ""), lines))
        if new_r:
            counts = Counter(a["priority"] for a in new_r)
            lines = [A.CALL, "", ", ".join(f"{p}: {counts[p]}" for p in A.PRIORITIES if counts[p]), ""]
            for a in new_r[:12]:
                lines.append(f"[{a['priority']}] Customer {a['customer_id']} ({a['claim_id']}, {a['city']} {a['policy_type']}, "
                             f"{a['claim_type']} {A.money(a['amount'])}): {a['wait_days']} days, limit {a['limit']}")
            if len(new_r) > 12:
                lines.append(f"... and {len(new_r) - 12} more in Alerts > Retention calls.")
            out.append(message("Head of Customer Retention",
                               f"{prefix}{len(new_r)} client{'s' if len(new_r) != 1 else ''} to call", lines))
        crit = sum(1 for a in new_c if a["level"] == 1)
        out.append(message("CFO", f"{prefix}{len(new_c)} claim alerts, {len(new_r)} clients to call",
                           [f"New claim alerts: {len(new_c)} ({crit} from watched brokers), "
                            f"{A.money(sum(a['claim_amount'] or 0 for a in new_c))} claimed.",
                            f"New retention calls: {len(new_r)}.", f"Trigger: {trigger}."]))
        self._save_json("outbox.json", self.outbox)
        return out

    def watch_payload(self) -> dict:
        with self.lock:
            w = self.watch_cache
            for a in w["claims"]:
                a["workflow"] = self.watch_status.get(a["key"], "New")
            for a in w["retention"]:
                a["workflow"] = self.watch_status.get(a["key"], "To call")
            return {**w, "statuses": {"claims": A.CLAIM_STATUSES, "retention": A.RETENTION_STATUSES}, "defaults": A.DEFAULT_WATCH}

    def set_watch_status(self, key: str, status: str) -> bool:
        allowed = A.CLAIM_STATUSES if key.startswith("claim:") else A.RETENTION_STATUSES
        if status not in allowed:
            return False
        with self.lock:
            self.watch_status[key] = status
            self._save_json("watch_status.json", self.watch_status)
        return True

    def set_watch_settings(self, settings: dict) -> dict:
        with self.lock:
            self.watch_settings = A.normalise({**self.watch_settings, **settings})
            self._save_json("watch_settings.json", self.watch_settings)
            self.run_watch("Alert rules changed", notify=False)
            return self.watch_payload()

    def watch_brief(self) -> dict:
        w = self.watch_cache or {"claims": [], "retention": []}
        crit_new = sum(1 for a in w["claims"] if a["level"] == 1 and self.watch_status.get(a["key"], "New") == "New")
        return {"claims": len(w["claims"]), "retention": len(w["retention"]), "critical_new": crit_new}
