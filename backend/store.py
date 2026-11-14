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
from datetime import datetime
from pathlib import Path

import pandas as pd

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

    # ------------------------------------------------------------ persistence
    def _load_json(self, name):
        path = RUNTIME / name
        if path.exists():
            try:
                return json.loads(path.read_text())
            except json.JSONDecodeError:
                return []
        return []

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
            self.rebuild()

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
