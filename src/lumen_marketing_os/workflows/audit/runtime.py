from __future__ import annotations

import hashlib
import hmac
import json
import re
import sqlite3
from datetime import datetime, timedelta, timezone
from typing import Any, Callable
from uuid import uuid4

from lumen_marketing_os.core.errors import InvalidStateError, NotFoundError
from lumen_marketing_os.core.time import parse_utc, utc_now, utc_text
from lumen_marketing_os.db.database import Database, append_audit
from lumen_marketing_os.policy.pause import PauseControl
from lumen_marketing_os.workflows.content.model import content_hash


DEFAULT_GUARD_RULES: dict[str, Any] = {
    "max_body_chars": 20_000,
    "max_subject_chars": 200,
    "blocked_patterns": [
        {"key": "guaranteed_result", "pattern": r"\bguarantee(?:d|s)?\b", "severity": "blocking"},
        {"key": "absolute_accuracy", "pattern": r"\b100\s*%\s+accurate\b", "severity": "blocking"},
        {"key": "fabricated_social_proof", "pattern": r"\bthousands of (?:happy )?customers\b", "severity": "blocking"},
        {"key": "deceptive_urgency", "pattern": r"\b(?:act now|last chance|final warning)\b", "severity": "warning"},
    ],
    "require_evidence_for_cold_email": True,
    "rule_version": "department-7-practical-v1",
}


def _json(value: Any) -> str:
    return json.dumps(value, sort_keys=True, separators=(",", ":"), ensure_ascii=False,
                      allow_nan=False)


def _required(value: str, name: str) -> str:
    if not value or not value.strip():
        raise ValueError(f"{name} is required")
    return value.strip()


class AuditRuntime:
    """Durable implementation of Department 7's six control roles.

    The runtime only creates internal records, findings, gates and recommendations. It never
    sends, publishes, changes campaign allocation, or silently resumes a paused lane.
    """

    def __init__(self, database: Database, pause: PauseControl, *,
                 suppression_key: bytes | None = None,
                 clock: Callable[[], datetime] = utc_now) -> None:
        self.db, self.pause, self.clock = database, pause, clock
        self.suppression_key = suppression_key

    def _now(self) -> str:
        return utc_text(self.clock())

    def ensure_default_policy(self, *, actor: str = "system") -> str:
        return self.create_policy(policy_key="copy-outreach-guard",
                                  rules=DEFAULT_GUARD_RULES, actor=actor, activate=True)

    def create_policy(self, *, policy_key: str, rules: dict[str, Any], actor: str,
                      activate: bool = False) -> str:
        policy_key, actor = _required(policy_key, "policy_key"), _required(actor, "actor")
        if not isinstance(rules, dict) or not rules:
            raise ValueError("rules must be a non-empty object")
        encoded = _json(rules)
        digest = hashlib.sha256(encoded.encode()).hexdigest()
        now, policy_id = self._now(), uuid4().hex
        with self.db.transaction() as c:
            existing = c.execute("SELECT id FROM audit_policy_versions WHERE policy_key=? "
                                 "AND rules_hash=?", (policy_key, digest)).fetchone()
            if existing:
                if activate:
                    self._activate_policy_locked(c, existing["id"], actor, now)
                return existing["id"]
            version = c.execute("SELECT COALESCE(MAX(version),0)+1 FROM audit_policy_versions "
                                "WHERE policy_key=?", (policy_key,)).fetchone()[0]
            c.execute("INSERT INTO audit_policy_versions(id,policy_key,version,rules_json,rules_hash,"
                      "state,created_by,created_at_utc) VALUES (?,?,?,?,?,'draft',?,?)",
                      (policy_id, policy_key, version, encoded, digest, actor, now))
            if activate:
                self._activate_policy_locked(c, policy_id, actor, now)
            append_audit(c, actor=actor, event_type="audit.policy_created",
                         entity_type="audit_policy", entity_id=policy_id,
                         details_json=_json({"policy_key": policy_key, "version": version,
                                             "activated": activate}), occurred_at_utc=now)
        return policy_id

    @staticmethod
    def _activate_policy_locked(c: sqlite3.Connection, policy_id: str, actor: str, now: str) -> None:
        row = c.execute("SELECT policy_key,state FROM audit_policy_versions WHERE id=?",
                        (policy_id,)).fetchone()
        if not row:
            raise NotFoundError("audit policy not found")
        c.execute("UPDATE audit_policy_versions SET state='retired' WHERE policy_key=? "
                  "AND state='active' AND id<>?", (row["policy_key"], policy_id))
        c.execute("UPDATE audit_policy_versions SET state='active',activated_by=?,activated_at_utc=? "
                  "WHERE id=?", (actor, now, policy_id))

    def activate_policy(self, *, policy_id: str, actor: str) -> None:
        actor, now = _required(actor, "actor"), self._now()
        with self.db.transaction() as c:
            self._activate_policy_locked(c, policy_id, actor, now)
            append_audit(c, actor=actor, event_type="audit.policy_activated",
                         entity_type="audit_policy", entity_id=policy_id, occurred_at_utc=now)

    def guard_draft(self, *, draft_id: str, reviewer: str = "7A",
                    checker_available: bool = True) -> dict[str, Any]:
        reviewer = _required(reviewer, "reviewer")
        now = self._now()
        with self.db.transaction() as c:
            draft = c.execute("SELECT * FROM drafts WHERE id=?", (draft_id,)).fetchone()
            if not draft:
                raise NotFoundError("draft not found")
            policy = c.execute("SELECT * FROM audit_policy_versions WHERE policy_key="
                               "'copy-outreach-guard' AND state='active'").fetchone()
            if not policy:
                raise InvalidStateError("no active 7A guard policy")
            existing = c.execute("SELECT * FROM audit_guard_reviews WHERE draft_id=? AND "
                                 "content_hash=? AND policy_id=?",
                                 (draft_id, draft["content_hash"], policy["id"])).fetchone()
            if existing:
                return self._guard_view(existing)
            findings: list[dict[str, str]] = []
            checker_state = "available" if checker_available else "unavailable"
            if not checker_available:
                findings.append({"key": "critical_checker_unavailable", "severity": "blocking",
                                 "message": "critical deterministic checker unavailable"})
            rules = json.loads(policy["rules_json"])
            subject, body = draft["subject"] or "", draft["body"]
            if content_hash(draft["subject"], body) != draft["content_hash"]:
                findings.append({"key": "content_hash_mismatch", "severity": "blocking",
                                 "message": "stored content no longer matches its hash"})
            if len(subject) > int(rules.get("max_subject_chars", 200)):
                findings.append({"key": "subject_too_long", "severity": "blocking",
                                 "message": "subject exceeds configured length"})
            if len(body) > int(rules.get("max_body_chars", 20_000)):
                findings.append({"key": "body_too_long", "severity": "blocking",
                                 "message": "body exceeds configured length"})
            text = f"{subject}\n{body}"
            for rule in rules.get("blocked_patterns", []):
                if re.search(rule["pattern"], text, flags=re.IGNORECASE):
                    findings.append({"key": rule["key"], "severity": rule["severity"],
                                     "message": "configured content rule matched"})
            evidence = json.loads(draft["evidence_json"] or "{}")
            if (draft["channel"] == "email" and rules.get("require_evidence_for_cold_email")
                    and not evidence):
                findings.append({"key": "missing_evidence", "severity": "blocking",
                                 "message": "email draft has no evidence packet"})
            decision = "blocked" if any(f["severity"] == "blocking" for f in findings) else \
                       "needs_review" if any(f["severity"] == "warning" for f in findings) else "passed"
            review_id = uuid4().hex
            c.execute("INSERT INTO audit_guard_reviews(id,draft_id,content_hash,policy_id,decision,"
                      "findings_json,checker_state,reviewed_by,reviewed_at_utc) VALUES (?,?,?,?,?,?,?,?,?)",
                      (review_id, draft_id, draft["content_hash"], policy["id"], decision,
                       _json(findings), checker_state, reviewer, now))
            if decision in {"passed", "blocked"}:
                round_no = c.execute("SELECT COALESCE(MAX(review_round),0)+1 FROM content_reviews "
                                     "WHERE draft_id=?", (draft_id,)).fetchone()[0]
                content_review_id = uuid4().hex
                c.execute("INSERT INTO content_reviews(id,draft_id,content_hash,input_hash,rule_version,"
                          "review_round,decision,reviewer_role,reviewed_at_utc) VALUES (?,?,?,?,?,?,?,'7A',?)",
                          (content_review_id, draft_id, draft["content_hash"],
                           draft["input_hash"] or draft["content_hash"],
                           json.loads(policy["rules_json"]).get("rule_version", policy["rules_hash"]),
                           round_no, decision, now))
                for finding in findings:
                    c.execute("INSERT INTO review_findings(id,draft_id,reviewer_role,rule_version,"
                              "severity,finding,disposition,created_at_utc,content_review_id) "
                              "VALUES (?,?, '7A',?,?,?,?,?,?)",
                              (uuid4().hex, draft_id, policy["rules_hash"], finding["severity"],
                               finding["message"], "block" if finding["severity"] == "blocking" else "review",
                               now, content_review_id))
            append_audit(c, actor=reviewer, event_type=f"audit.7a_{decision}",
                         entity_type="draft", entity_id=draft_id,
                         details_json=_json({"guard_review_id": review_id,
                                             "policy_id": policy["id"]}), occurred_at_utc=now)
            row = c.execute("SELECT * FROM audit_guard_reviews WHERE id=?", (review_id,)).fetchone()
        return self._guard_view(row)

    @staticmethod
    def _guard_view(row: sqlite3.Row) -> dict[str, Any]:
        result = dict(row)
        result["findings"] = json.loads(result.pop("findings_json"))
        return result

    def record_provider_health(self, *, channel: str, account_id: str, provider: str,
                               status: str, reason: str, actor: str,
                               metrics: dict[str, Any] | None = None,
                               expires_at_utc: str | None = None) -> str:
        if status not in {"healthy", "degraded", "unhealthy", "unknown"}:
            raise ValueError("invalid provider health status")
        for value, name in ((channel, "channel"), (account_id, "account_id"),
                            (provider, "provider"), (reason, "reason"), (actor, "actor")):
            _required(value, name)
        if expires_at_utc and parse_utc(expires_at_utc) <= self.clock():
            raise ValueError("health expiry must be in the future")
        now, snapshot_id = self._now(), uuid4().hex
        with self.db.transaction() as c:
            c.execute("INSERT INTO provider_health_snapshots(id,channel,account_id,provider,status,"
                      "metrics_json,reason,observed_at_utc,expires_at_utc,recorded_by) "
                      "VALUES (?,?,?,?,?,?,?,?,?,?)",
                      (snapshot_id, channel, account_id, provider, status, _json(metrics or {}),
                       reason, now, expires_at_utc, actor))
            append_audit(c, actor=actor, event_type=f"audit.7b_health_{status}",
                         entity_type="provider_health", entity_id=snapshot_id,
                         details_json=_json({"channel": channel, "account_id": account_id}),
                         occurred_at_utc=now)
        return snapshot_id

    def rate_health_report(self) -> dict[str, Any]:
        now = self._now()
        with self.db.read() as c:
            policies = [dict(r) for r in c.execute("SELECT q.*,COALESCE(SUM(CASE WHEN l.state IN "
                "('reserved','committed','unknown') THEN l.units ELSE 0 END),0) AS recorded_units "
                "FROM quota_policies q LEFT JOIN quota_ledger l ON l.policy_id=q.id "
                "WHERE q.active=1 GROUP BY q.id ORDER BY q.channel,q.account_id")]
            health = [dict(r) for r in c.execute("SELECT h.* FROM provider_health_snapshots h "
                "WHERE h.rowid=(SELECT x.rowid FROM provider_health_snapshots x WHERE "
                "x.channel=h.channel AND x.account_id=h.account_id ORDER BY x.observed_at_utc DESC,"
                "x.rowid DESC LIMIT 1) ORDER BY h.channel,h.account_id")]
        for row in health:
            row["metrics"] = json.loads(row.pop("metrics_json"))
            row["expired"] = bool(row["expires_at_utc"] and row["expires_at_utc"] <= now)
        return {"quotas": policies, "provider_health": health}

    def integrity_scan(self, *, actor: str = "7C") -> dict[str, Any]:
        run_id, now = self._start_run("7C", "database_integrity", {}, actor)
        findings: list[dict[str, Any]] = []
        check = self.db.integrity_check()
        if not check["ok"]:
            findings.append({"key": "sqlite_integrity", "severity": "critical",
                             "summary": "SQLite integrity or foreign-key check failed",
                             "evidence": check})
        with self.db.read() as c:
            running = [dict(r) for r in c.execute("SELECT id,job_id,attempt_number FROM job_attempts "
                                                  "WHERE result='running'")]
            for row in running:
                job = c.execute("SELECT state,lease_expires_at_utc FROM jobs WHERE id=?",
                                (row["job_id"],)).fetchone()
                if not job or job["state"] != "leased":
                    findings.append({"key": f"orphan_attempt:{row['id']}", "severity": "blocking",
                                     "summary": "running job attempt has no leased job",
                                     "entity_type": "job_attempt", "entity_id": row["id"],
                                     "evidence": row})
            organizations = {r["id"]: r["merged_into_id"] for r in c.execute(
                "SELECT id,merged_into_id FROM organizations WHERE merged_into_id IS NOT NULL")}
            for start in organizations:
                seen, node = set(), start
                while node in organizations:
                    if node in seen:
                        findings.append({"key": f"organization_cycle:{start}", "severity": "blocking",
                                         "summary": "organization merge cycle detected",
                                         "entity_type": "organization", "entity_id": start,
                                         "evidence": {"path_start": start}})
                        break
                    seen.add(node)
                    node = organizations[node]
            duplicate_messages = [dict(r) for r in c.execute(
                "SELECT channel,provider_message_id,COUNT(*) n FROM messages "
                "WHERE provider_message_id IS NOT NULL GROUP BY channel,provider_message_id HAVING n>1")]
            for row in duplicate_messages:
                findings.append({"key": "duplicate_message:" + row["channel"] + ":" + row["provider_message_id"],
                                 "severity": "blocking", "summary": "duplicate provider message identity",
                                 "entity_type": "message", "evidence": row})
        return self._finish_run(run_id, "7C", findings, actor,
                                extra={"database": check, "destructive_changes": 0})

    def growth_audit(self, *, window_start_utc: str, window_end_utc: str,
                     actor: str = "7D", minimum_sample: int = 20) -> dict[str, Any]:
        start, end = parse_utc(window_start_utc), parse_utc(window_end_utc)
        if end <= start or minimum_sample < 1:
            raise ValueError("invalid growth audit window or sample threshold")
        with self.db.read() as c:
            rows = [dict(r) for r in c.execute(
                "SELECT a.channel,COUNT(DISTINCT a.id) actions,"
                "SUM(CASE WHEN a.state='accepted' THEN 1 ELSE 0 END) accepted,"
                "SUM(CASE WHEN a.state='rejected' THEN 1 ELSE 0 END) rejected,"
                "SUM(CASE WHEN a.state='unknown' THEN 1 ELSE 0 END) unknowns "
                "FROM actions a WHERE a.created_at_utc>=? AND a.created_at_utc<? GROUP BY a.channel",
                (utc_text(start), utc_text(end)))]
            paid = c.execute("SELECT COUNT(*),COALESCE(SUM(amount_minor),0) FROM paid_conversions "
                             "WHERE paid_at_utc>=? AND paid_at_utc<?",
                             (utc_text(start), utc_text(end))).fetchone()
            trials = c.execute("SELECT COUNT(*) FROM trials WHERE started_at_utc>=? AND started_at_utc<?",
                               (utc_text(start), utc_text(end))).fetchone()[0]
            usage = c.execute("SELECT COALESCE(SUM(estimated_minor),0) FROM usage_ledger "
                              "WHERE occurred_at_utc>=? AND occurred_at_utc<?",
                              (utc_text(start), utc_text(end))).fetchone()[0]
        total = sum(r["actions"] for r in rows)
        metrics = {"channels": rows, "trials": trials, "verified_paid": paid[0],
                   "verified_revenue_minor": paid[1], "estimated_provider_cost_minor": usage,
                   "action_sample": total}
        sample_warning = total < minimum_sample
        recommendations = ([{"type": "collect_more_data", "reason": "sample below threshold",
                             "autonomous_change": False}] if sample_warning else
                           [{"type": "operator_review", "reason": "compare channel outcomes and costs",
                             "autonomous_change": False}])
        encoded = _json({"window": [utc_text(start), utc_text(end)], "metrics": metrics,
                         "recommendations": recommendations})
        report_hash, report_id, now = hashlib.sha256(encoded.encode()).hexdigest(), uuid4().hex, self._now()
        with self.db.transaction() as c:
            existing = c.execute("SELECT id FROM growth_audit_reports WHERE report_hash=?",
                                 (report_hash,)).fetchone()
            if existing:
                report_id = existing["id"]
            else:
                c.execute("INSERT INTO growth_audit_reports(id,window_start_utc,window_end_utc,metrics_json,"
                          "recommendations_json,sample_warning,report_hash,created_by,created_at_utc) "
                          "VALUES (?,?,?,?,?,?,?,?,?)", (report_id, utc_text(start), utc_text(end),
                          _json(metrics), _json(recommendations), int(sample_warning), report_hash, actor, now))
                append_audit(c, actor=actor, event_type="audit.7d_growth_reported",
                             entity_type="growth_audit_report", entity_id=report_id,
                             details_json=_json({"sample_warning": sample_warning}), occurred_at_utc=now)
        return {"report_id": report_id, "metrics": metrics, "recommendations": recommendations,
                "sample_warning": sample_warning}

    def pipeline_scan(self, *, actor: str = "7E", pending_lag_seconds: int = 3600,
                      heartbeat_lag_seconds: int = 300) -> dict[str, Any]:
        if pending_lag_seconds < 60 or heartbeat_lag_seconds < 60:
            raise ValueError("lag thresholds must be at least 60 seconds")
        run_id, now = self._start_run("7E", "pipeline_lag", {
            "pending_lag_seconds": pending_lag_seconds,
            "heartbeat_lag_seconds": heartbeat_lag_seconds}, actor)
        findings: list[dict[str, Any]] = []
        pending_cutoff = utc_text(self.clock() - timedelta(seconds=pending_lag_seconds))
        heartbeat_cutoff = utc_text(self.clock() - timedelta(seconds=heartbeat_lag_seconds))
        with self.db.read() as c:
            for row in c.execute("SELECT id,lane,workflow,due_at_utc,state FROM jobs WHERE "
                                 "state IN ('pending','retry_wait') AND due_at_utc<?", (pending_cutoff,)):
                findings.append({"key": f"lagged_job:{row['id']}", "severity": "warning",
                                 "summary": "job is overdue beyond configured threshold",
                                 "entity_type": "job", "entity_id": row["id"], "lane": row["lane"],
                                 "evidence": dict(row)})
            for row in c.execute("SELECT id,lane,workflow,lease_expires_at_utc FROM jobs WHERE "
                                 "state='leased' AND lease_expires_at_utc<=?", (now,)):
                findings.append({"key": f"expired_lease:{row['id']}", "severity": "blocking",
                                 "summary": "job lease expired; use queue recovery before replay",
                                 "entity_type": "job", "entity_id": row["id"], "lane": row["lane"],
                                 "evidence": dict(row)})
            for row in c.execute("SELECT process_id,role,heartbeat_at_utc FROM operations_heartbeats "
                                 "WHERE heartbeat_at_utc<?", (heartbeat_cutoff,)):
                findings.append({"key": f"stale_heartbeat:{row['process_id']}", "severity": "warning",
                                 "summary": "operations process heartbeat is stale",
                                 "entity_type": "process", "entity_id": row["process_id"],
                                 "evidence": dict(row)})
            unknowns = c.execute("SELECT COUNT(*) FROM actions WHERE state='unknown'").fetchone()[0]
            if unknowns:
                findings.append({"key": "unknown_external_actions", "severity": "blocking",
                                 "summary": "external actions require reconciliation, not replay",
                                 "entity_type": "action", "evidence": {"count": unknowns}})
        return self._finish_run(run_id, "7E", findings, actor,
                                extra={"automatic_replays": 0, "automatic_restarts": 0})

    def suppression_check(self, *, subject_type: str, value: str,
                          actor: str = "7F") -> dict[str, Any]:
        if subject_type not in {"address", "domain", "organization", "account"}:
            raise ValueError("invalid suppression subject type")
        if not self.suppression_key or len(self.suppression_key) < 32:
            raise InvalidStateError("suppression key unavailable; fail closed")
        value = _required(value, "value").lower()
        digest = hmac.new(self.suppression_key, f"{subject_type}:{value}".encode(),
                          hashlib.sha256).hexdigest()
        now, event_id = self._now(), uuid4().hex
        with self.db.transaction() as c:
            row = c.execute("SELECT id,reason,created_at_utc,expires_at_utc FROM suppression_rules "
                            "WHERE subject_type=? AND keyed_value=? AND "
                            "(expires_at_utc IS NULL OR expires_at_utc>?) ORDER BY created_at_utc DESC LIMIT 1",
                            (subject_type, digest, now)).fetchone()
            result = "suppressed" if row else "clear"
            c.execute("INSERT INTO compliance_events(id,event_type,subject_type,suppression_rule_id,"
                      "result,details_json,occurred_at_utc,actor) VALUES (?,?,?,?,?,?,?,?)",
                      (event_id, "suppression_checked", subject_type, row["id"] if row else None,
                       result, _json({"value_stored": False}), now, actor))
            append_audit(c, actor=actor, event_type=f"audit.7f_{result}",
                         entity_type="suppression_rule", entity_id=row["id"] if row else None,
                         occurred_at_utc=now)
        return {"suppressed": bool(row), "rule_id": row["id"] if row else None,
                "reason": row["reason"] if row else None, "event_id": event_id}

    def retention_review(self, *, actor: str = "7F") -> dict[str, Any]:
        now, event_id = self._now(), uuid4().hex
        with self.db.transaction() as c:
            expired = c.execute("SELECT COUNT(*) FROM suppression_rules WHERE expires_at_utc IS NOT NULL "
                                "AND expires_at_utc<=?", (now,)).fetchone()[0]
            # Suppression deletion is deliberately never automatic: a human can assess the legal and
            # operational basis while the gate ignores expired entries.
            c.execute("INSERT INTO compliance_events(id,event_type,result,details_json,occurred_at_utc,actor) "
                      "VALUES (?,'retention_review','review_required',?,?,?)",
                      (event_id, _json({"expired_rules": expired, "deleted": 0}), now, actor))
            append_audit(c, actor=actor, event_type="audit.7f_retention_review",
                         entity_type="compliance_event", entity_id=event_id,
                         details_json=_json({"expired_rules": expired}), occurred_at_utc=now)
        return {"event_id": event_id, "expired_rules": expired, "deleted": 0,
                "next_action": "operator review" if expired else "none"}

    def full_scan(self, *, actor: str = "department-7") -> dict[str, Any]:
        return {"rate_health": self.rate_health_report(),
                "integrity": self.integrity_scan(actor=actor),
                "pipeline": self.pipeline_scan(actor=actor),
                "retention": self.retention_review(actor=actor),
                "status": self.report()}

    def _start_run(self, role_id: str, run_kind: str, input_data: dict[str, Any], actor: str) -> tuple[str, str]:
        now, run_id = self._now(), uuid4().hex
        with self.db.transaction() as c:
            c.execute("INSERT INTO audit_runs(id,role_id,run_kind,status,input_json,started_at_utc,initiated_by) "
                      "VALUES (?,?,?,'running',?,?,?)", (run_id, role_id, run_kind,
                      _json(input_data), now, actor))
        return run_id, now

    def _finish_run(self, run_id: str, role_id: str, findings: list[dict[str, Any]],
                    actor: str, *, extra: dict[str, Any] | None = None) -> dict[str, Any]:
        now = self._now()
        status = "blocked" if any(f["severity"] in {"blocking", "critical"} for f in findings) else \
                 "warning" if findings else "passed"
        with self.db.transaction() as c:
            for finding in findings:
                c.execute("INSERT INTO audit_findings(id,run_id,role_id,finding_key,severity,entity_type,"
                          "entity_id,lane,summary,evidence_json,state,created_at_utc) "
                          "VALUES (?,?,?,?,?,?,?,?,?,?,'open',?)",
                          (uuid4().hex, run_id, role_id, finding["key"], finding["severity"],
                           finding.get("entity_type"), finding.get("entity_id"), finding.get("lane"),
                           finding["summary"], _json(finding.get("evidence", {})), now))
            result = {"finding_count": len(findings), **(extra or {})}
            c.execute("UPDATE audit_runs SET status=?,result_json=?,finished_at_utc=? WHERE id=?",
                      (status, _json(result), now, run_id))
            append_audit(c, actor=actor, event_type=f"audit.{role_id.lower()}_scan_{status}",
                         entity_type="audit_run", entity_id=run_id,
                         details_json=_json(result), occurred_at_utc=now)
        return {"run_id": run_id, "status": status, "findings": findings, **(extra or {})}

    def resolve_finding(self, *, finding_id: str, actor: str, resolution: str) -> None:
        actor, resolution, now = _required(actor, "actor"), _required(resolution, "resolution"), self._now()
        with self.db.transaction() as c:
            if c.execute("UPDATE audit_findings SET state='resolved',resolved_at_utc=?,resolved_by=? "
                         "WHERE id=? AND state<>'resolved'", (now, actor, finding_id)).rowcount != 1:
                raise InvalidStateError("open audit finding not found")
            append_audit(c, actor=actor, event_type="audit.finding_resolved",
                         entity_type="audit_finding", entity_id=finding_id,
                         details_json=_json({"resolution": resolution}), occurred_at_utc=now)

    def report(self, *, limit: int = 50) -> dict[str, Any]:
        if not 1 <= limit <= 1000:
            raise ValueError("limit must be 1..1000")
        with self.db.read() as c:
            policies = [dict(r) for r in c.execute("SELECT id,policy_key,version,state,rules_hash,"
                                                   "created_at_utc,activated_at_utc FROM audit_policy_versions "
                                                   "ORDER BY policy_key,version DESC")]
            runs = [dict(r) for r in c.execute("SELECT id,role_id,run_kind,status,started_at_utc,"
                                               "finished_at_utc FROM audit_runs ORDER BY started_at_utc DESC LIMIT ?",
                                               (limit,))]
            findings = [dict(r) for r in c.execute("SELECT id,run_id,role_id,severity,entity_type,entity_id,"
                                                   "lane,summary,state,created_at_utc FROM audit_findings "
                                                   "ORDER BY created_at_utc DESC LIMIT ?", (limit,))]
            guards = [dict(r) for r in c.execute("SELECT id,draft_id,content_hash,policy_id,decision,"
                                                 "checker_state,reviewed_at_utc FROM audit_guard_reviews "
                                                 "ORDER BY reviewed_at_utc DESC LIMIT ?", (limit,))]
            growth = [dict(r) for r in c.execute("SELECT id,window_start_utc,window_end_utc,sample_warning,"
                                                 "report_hash,created_at_utc FROM growth_audit_reports "
                                                 "ORDER BY created_at_utc DESC LIMIT ?", (limit,))]
        return {"policies": policies, "guard_reviews": guards, "runs": runs,
                "open_findings": [f for f in findings if f["state"] == "open"],
                "recent_findings": findings, "growth_reports": growth,
                "rate_health": self.rate_health_report(),
                "pause_scopes": self.pause.active_scopes()}
