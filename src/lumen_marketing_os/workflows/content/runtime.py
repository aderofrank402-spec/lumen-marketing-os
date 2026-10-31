"""Department 3: reviewed evidence -> immutable drafts -> critique; experiment advice only."""
from __future__ import annotations

import json
import sqlite3
import time
from datetime import datetime, timezone
from typing import Callable
from uuid import uuid4

from lumen_marketing_os.core.time import utc_now, utc_text
from lumen_marketing_os.core.errors import PausedError
from lumen_marketing_os.db.database import Database, append_audit
from lumen_marketing_os.engine.outbox import Outbox
from lumen_marketing_os.engine.queue import Job, JobQueue, OutgoingEvent
from lumen_marketing_os.policy.pause import PauseControl
from lumen_marketing_os.workflows.content.experiments import analyze_variants
from lumen_marketing_os.workflows.content.model import (RULE_VERSION, canonical, content_hash,
                                                         critique, digest, render_template,
                                                         validate_template)


class ContentRuntime:
    def __init__(self, *, database: Database, pause: PauseControl, queue: JobQueue,
                 outbox: Outbox, clock: Callable = utc_now) -> None:
        self.db, self.pause, self.queue, self.outbox, self.clock = database, pause, queue, outbox, clock

    def _now(self) -> str:
        return utc_text(self.clock())

    def register_claim(self, *, claim_key: str, claim_type: str, claim_text: str,
                       evidence_ref: str, actor: str, permission_confirmed: bool = False) -> str:
        if claim_type not in {"product", "customer"} or not all(
                isinstance(x, str) and x.strip() for x in (claim_key, claim_text, evidence_ref, actor)):
            raise ValueError("claim key, type, text, evidence and actor are required")
        if len(claim_text) > 1000 or len(evidence_ref) > 1000:
            raise ValueError("claim text or evidence reference too long")
        if claim_type == "customer" and not permission_confirmed:
            raise ValueError("customer result claims require confirmed permission")
        now, claim_id = self._now(), uuid4().hex
        with self.db.transaction() as c:
            version = c.execute("SELECT COALESCE(MAX(version),0)+1 FROM content_claims WHERE claim_key=?",
                                (claim_key,)).fetchone()[0]
            c.execute("INSERT INTO content_claims(id,claim_key,version,claim_type,claim_text,evidence_ref,"
                      "permission_confirmed,status,created_at_utc) VALUES (?,?,?,?,?,?,?,'draft',?)",
                      (claim_id, claim_key, version, claim_type, claim_text, evidence_ref,
                       int(permission_confirmed), now))
            append_audit(c, actor=actor, event_type="content.claim_registered", entity_type="content_claim",
                         entity_id=claim_id, occurred_at_utc=now)
        return claim_id

    def approve_claim(self, *, claim_id: str, reviewer: str) -> None:
        if not reviewer.strip():
            raise ValueError("reviewer required")
        now = self._now()
        with self.db.transaction() as c:
            row = c.execute("SELECT * FROM content_claims WHERE id=?", (claim_id,)).fetchone()
            if not row or row["status"] != "draft":
                raise ValueError("only a draft claim can be approved")
            c.execute("UPDATE content_claims SET status='retired' WHERE claim_key=? AND status='approved'",
                      (row["claim_key"],))
            c.execute("UPDATE content_claims SET status='approved',approved_by=?,approved_at_utc=? WHERE id=?",
                      (reviewer, now, claim_id))
            append_audit(c, actor=reviewer, event_type="content.claim_approved",
                         entity_type="content_claim", entity_id=claim_id, occurred_at_utc=now)

    def register_template(self, *, template_key: str, kind: str, channel: str,
                          subject_template: str | None, body_template: str, actor: str) -> str:
        if kind not in {"personalized", "technical", "case_study"} or not all(
                isinstance(x, str) and x.strip() for x in (template_key, channel, actor)):
            raise ValueError("template key, kind, channel and actor are required")
        fields = validate_template(body_template) | validate_template(subject_template)
        if kind == "personalized" and not {"company", "product_claim"}.issubset(fields):
            raise ValueError("personalized template needs company and product_claim fields")
        if kind == "case_study" and "customer_claim" not in fields:
            raise ValueError("case study template needs customer_claim field")
        if kind == "technical" and "product_claim" not in fields:
            raise ValueError("technical template needs product_claim field")
        now, template_id = self._now(), uuid4().hex
        with self.db.transaction() as c:
            version = c.execute("SELECT COALESCE(MAX(version),0)+1 FROM content_templates WHERE template_key=?",
                                (template_key,)).fetchone()[0]
            c.execute("INSERT INTO content_templates(id,template_key,version,kind,channel,subject_template,"
                      "body_template,status,created_at_utc) VALUES (?,?,?,?,?,?,?,'draft',?)",
                      (template_id, template_key, version, kind, channel, subject_template, body_template, now))
            append_audit(c, actor=actor, event_type="content.template_registered",
                         entity_type="content_template", entity_id=template_id, occurred_at_utc=now)
        return template_id

    def approve_template(self, *, template_id: str, reviewer: str) -> None:
        if not reviewer.strip():
            raise ValueError("reviewer required")
        now = self._now()
        with self.db.transaction() as c:
            row = c.execute("SELECT * FROM content_templates WHERE id=?", (template_id,)).fetchone()
            if not row or row["status"] != "draft":
                raise ValueError("only a draft template can be approved")
            c.execute("UPDATE content_templates SET status='retired' WHERE template_key=? AND status='approved'",
                      (row["template_key"],))
            c.execute("UPDATE content_templates SET status='approved',approved_by=?,approved_at_utc=? WHERE id=?",
                      (reviewer, now, template_id))
            append_audit(c, actor=reviewer, event_type="content.template_approved",
                         entity_type="content_template", entity_id=template_id, occurred_at_utc=now)

    @staticmethod
    def _eligible_contact(c: sqlite3.Connection, prospect_id: str, contact_id: str) -> sqlite3.Row:
        row = c.execute(
            "SELECT p.id AS prospect_id,p.lifecycle_state,p.pain_evidence_url,p.pain_evidence_note,p.use_case_id,"
            "o.display_name AS company,o.identity_status,ct.display_name AS contact_name,"
            "ct.role_title,ct.verification_status,ct.organization_id FROM prospects p "
            "JOIN organizations o ON o.id=p.organization_id "
            "JOIN contacts ct ON ct.organization_id=o.id WHERE p.id=? AND ct.id=?",
            (prospect_id, contact_id)).fetchone()
        if not row or row["identity_status"] != "verified" or row["verification_status"] != "verified" or not (
                row["contact_name"] and row["role_title"]):
            raise ValueError("verified organization and named, role-bearing contact required")
        fit = c.execute("SELECT decision FROM fit_assessments WHERE prospect_id=? "
                        "ORDER BY assessed_at_utc DESC,rowid DESC LIMIT 1", (prospect_id,)).fetchone()
        if not fit or fit["decision"] != "qualified":
            raise ValueError("current qualified fit assessment required")
        provenance = c.execute("SELECT 1 FROM contact_provenance WHERE contact_id=? AND source_url IS NOT NULL "
                               "LIMIT 1", (contact_id,)).fetchone()
        if not provenance:
            raise ValueError("contact needs source provenance")
        return row

    def consume_contact_once(self, *, worker_id: str = "content-intake") -> dict | None:
        item = self.outbox.claim(worker_id=worker_id, destination="content.review")
        if item is None:
            return None
        try:
            if item.event_type != "contact.verified" or item.schema_version != 1:
                raise ValueError("unsupported contact handoff")
            prospect_id, contact_id = item.payload.get("prospect_id"), item.payload.get("contact_id")
            with self.db.transaction() as c:
                self._eligible_contact(c, prospect_id, contact_id)
                c.execute("INSERT OR IGNORE INTO content_intakes(id,prospect_id,contact_id,source_event_id,"
                          "state,created_at_utc) VALUES (?,?,?,?,'needs_review',?)",
                          (uuid4().hex, prospect_id, contact_id, item.id, self._now()))
            self.outbox.acknowledge(item_id=item.id, worker_id=worker_id)
            return {"event_id": item.id, "status": "needs_review"}
        except (ValueError, sqlite3.Error):
            self.outbox.fail(item_id=item.id, worker_id=worker_id,
                             error_class="invalid_content_handoff")
            return {"event_id": item.id, "status": "needs_review"}

    def request(self, *, kind: str, template_id: str, claim_ids: list[str], actor: str,
                intake_id: str | None = None, logical_key: str | None = None) -> str:
        if kind not in {"personalized", "technical", "case_study"} or not actor.strip() or not claim_ids:
            raise ValueError("kind, actor and at least one approved claim required")
        if len(set(claim_ids)) != len(claim_ids):
            raise ValueError("duplicate claim IDs")
        now, request_id = self._now(), uuid4().hex
        key = logical_key or f"content:{kind}:{intake_id or uuid4().hex}:{template_id}:{digest(sorted(claim_ids))}"
        with self.db.transaction() as c:
            existing = c.execute("SELECT id,kind,template_id,intake_id FROM content_requests WHERE logical_key=?",
                                 (key,)).fetchone()
            if existing:
                prior_claims = {row[0] for row in c.execute(
                    "SELECT claim_id FROM content_request_claims WHERE request_id=?", (existing["id"],))}
                if (existing["kind"], existing["template_id"], existing["intake_id"], prior_claims) != (
                        kind, template_id, intake_id, set(claim_ids)):
                    raise ValueError("logical content request key conflicts")
                return existing["id"]
            template = c.execute("SELECT * FROM content_templates WHERE id=?", (template_id,)).fetchone()
            if not template or template["status"] != "approved" or template["kind"] != kind:
                raise ValueError("approved template of matching kind required")
            claims = [c.execute("SELECT * FROM content_claims WHERE id=?", (claim_id,)).fetchone()
                      for claim_id in claim_ids]
            if any(not row or row["status"] != "approved" for row in claims):
                raise ValueError("all claims must be approved")
            if kind in {"personalized", "technical"} and not any(
                    row["claim_type"] == "product" for row in claims):
                raise ValueError("approved product claim required")
            if kind == "case_study" and not any(
                    row["claim_type"] == "customer" and row["permission_confirmed"] for row in claims):
                raise ValueError("case study needs a permitted approved customer result")
            prospect_id = contact_id = None
            if kind == "personalized":
                intake = c.execute("SELECT * FROM content_intakes WHERE id=? AND state='needs_review'",
                                   (intake_id,)).fetchone()
                if not intake:
                    raise ValueError("reviewable Department 2 intake required")
                self._eligible_contact(c, intake["prospect_id"], intake["contact_id"])
                prospect_id, contact_id = intake["prospect_id"], intake["contact_id"]
            elif intake_id:
                raise ValueError("generic technical/case-study requests do not use contact intake")
            c.execute("INSERT INTO content_requests(id,intake_id,prospect_id,contact_id,kind,template_id,"
                      "status,logical_key,requested_by,requested_at_utc,updated_at_utc) "
                      "VALUES (?,?,?,?,?,?,'pending',?,?,?,?)",
                      (request_id, intake_id, prospect_id, contact_id, kind, template_id,
                       key, actor, now, now))
            for claim_id in claim_ids:
                c.execute("INSERT INTO content_request_claims(request_id,claim_id) VALUES (?,?)",
                          (request_id, claim_id))
            if intake_id:
                c.execute("UPDATE content_intakes SET state='ready',reviewer=?,reviewed_at_utc=? WHERE id=?",
                          (actor, now, intake_id))
            append_audit(c, actor=actor, event_type="content.requested", entity_type="content_request",
                         entity_id=request_id, occurred_at_utc=now)
        return request_id

    def _context(self, c: sqlite3.Connection, request_id: str) -> tuple[sqlite3.Row, dict, dict, str]:
        request = c.execute("SELECT * FROM content_requests WHERE id=?", (request_id,)).fetchone()
        if not request:
            raise ValueError("content request not found")
        template = c.execute("SELECT * FROM content_templates WHERE id=?", (request["template_id"],)).fetchone()
        if not template or template["status"] != "approved" or template["kind"] != request["kind"]:
            raise ValueError("template retired or unavailable")
        claims = c.execute("SELECT cl.* FROM content_claims cl JOIN content_request_claims rc "
                           "ON rc.claim_id=cl.id WHERE rc.request_id=? ORDER BY cl.claim_key,cl.version",
                           (request_id,)).fetchall()
        if not claims or any(row["status"] != "approved" for row in claims):
            raise ValueError("claim retired or unavailable")
        product = [r["claim_text"] for r in claims if r["claim_type"] == "product"]
        customer = [r["claim_text"] for r in claims if r["claim_type"] == "customer" and r["permission_confirmed"]]
        if request["kind"] in {"personalized", "technical"} and not product:
            raise ValueError("approved product claim missing")
        if request["kind"] == "case_study" and not customer:
            raise ValueError("approved permitted customer claim missing")
        values = {"company": "", "contact_first_name": "", "role": "", "problem": "",
                  "product_claim": " ".join(product), "customer_claim": " ".join(customer)}
        fit_id = None
        contact_source = None
        pain_evidence_url = pain_evidence_note = None
        if request["kind"] == "personalized":
            row = self._eligible_contact(c, request["prospect_id"], request["contact_id"])
            case = c.execute("SELECT problem_statement,status FROM use_cases WHERE id=?",
                             (row["use_case_id"],)).fetchone() if row["use_case_id"] else None
            if not case or case["status"] != "approved" or not row["pain_evidence_url"]:
                raise ValueError("approved organization-specific problem evidence required")
            fit_id = c.execute("SELECT id FROM fit_assessments WHERE prospect_id=? ORDER BY "
                               "assessed_at_utc DESC,rowid DESC LIMIT 1",
                               (request["prospect_id"],)).fetchone()["id"]
            source = c.execute("SELECT id,source_url,observed_at_utc FROM contact_provenance "
                               "WHERE contact_id=? ORDER BY observed_at_utc DESC,rowid DESC LIMIT 1",
                               (request["contact_id"],)).fetchone()
            contact_source = dict(source) if source else None
            pain_evidence_url, pain_evidence_note = row["pain_evidence_url"], row["pain_evidence_note"]
            values.update(company=row["company"], contact_first_name=row["contact_name"].split()[0],
                          role=row["role_title"], problem=case["problem_statement"])
        evidence = {"template_id": template["id"], "template_version": template["version"],
                    "claims": [{"id": r["id"], "version": r["version"], "evidence_ref": r["evidence_ref"]}
                               for r in claims], "fit_assessment_id": fit_id,
                    "prospect_id": request["prospect_id"], "contact_id": request["contact_id"],
                    "contact_source": contact_source, "pain_evidence_url": pain_evidence_url,
                    "pain_evidence_note": pain_evidence_note}
        input_hash = digest({"evidence": evidence, "values": values,
                             "subject_template": template["subject_template"],
                             "body_template": template["body_template"]})
        return template, values, evidence, input_hash

    def tick(self) -> list[Job]:
        self.pause.guard()
        jobs: list[Job] = []
        with self.db.read() as c:
            requests = c.execute("SELECT id,kind FROM content_requests WHERE status='pending'").fetchall()
            drafts = c.execute("SELECT id FROM drafts WHERE request_id IS NOT NULL AND status='review_pending'").fetchall()
        for row in requests:
            lane = "content.3A" if row["kind"] == "personalized" else "content.3B"
            jobs.append(self.queue.enqueue(workflow="content.compose", lane=lane,
                                           payload={"request_id": row["id"]},
                                           logical_key=f"content:compose:{row['id']}"))
        for row in drafts:
            jobs.append(self.queue.enqueue(workflow="content.critique", lane="content.3D",
                                           payload={"draft_id": row["id"]},
                                           logical_key=f"content:critique:{row['id']}"))
        return jobs

    def work_once(self, *, worker_id: str = "content-worker") -> dict | None:
        for lane in ("content.3A", "content.3B", "content.3D"):
            job = self.queue.claim(worker_id=worker_id, lane=lane)
            if job is None:
                continue
            try:
                if job.workflow == "content.compose":
                    return self._compose(job, worker_id)
                if job.workflow == "content.critique":
                    return self._critique(job, worker_id)
                self.queue.fail(job_id=job.id, worker_id=worker_id,
                                error_class="unsupported_content_job", retryable=False)
                return {"job_id": job.id, "status": "needs_review"}
            except (KeyError, TypeError, ValueError, sqlite3.Error) as exc:
                self.queue.fail(job_id=job.id, worker_id=worker_id,
                                error_class=type(exc).__name__, retryable=False)
                return {"job_id": job.id, "status": "needs_review", "error": str(exc)}
        return None

    def _compose(self, job: Job, worker: str) -> dict:
        request_id = job.payload["request_id"]
        with self.db.read() as c:
            request = c.execute("SELECT status,kind,prospect_id,contact_id FROM content_requests WHERE id=?",
                                (request_id,)).fetchone()
            if not request or request["status"] != "pending":
                raise ValueError("content request no longer pending")
            template, values, evidence, input_hash = self._context(c, request_id)
            subject = render_template(template["subject_template"], values)
            body = render_template(template["body_template"], values)
        if not body:
            raise ValueError("empty rendered body")
        draft_id, now = uuid4().hex, self._now()
        def write(c: sqlite3.Connection) -> None:
            current = c.execute("SELECT status FROM content_requests WHERE id=?", (request_id,)).fetchone()
            if not current or current["status"] != "pending":
                raise ValueError("content request changed")
            # Re-evaluate under the write transaction so changed approvals never enter a draft.
            _, _, current_evidence, current_hash = self._context(c, request_id)
            if current_hash != input_hash or current_evidence != evidence:
                raise ValueError("content evidence changed before draft commit")
            c.execute("INSERT INTO drafts(id,prospect_id,contact_id,channel,subject,body,content_hash,version,"
                      "evidence_json,author_role,status,created_at_utc,request_id,input_hash,template_id) "
                      "VALUES (?,?,?,?,?,?,?,?,?,?,'review_pending',?,?,?,?)",
                      (draft_id, request["prospect_id"], request["contact_id"], template["channel"],
                       subject, body, content_hash(subject, body), 1, canonical(evidence),
                       "3A" if request["kind"] == "personalized" else "3B", now, request_id,
                       input_hash, template["id"]))
            c.execute("UPDATE content_requests SET status='drafted',updated_at_utc=? WHERE id=?", (now, request_id))
            append_audit(c, actor=worker, event_type="content.draft_created", entity_type="draft",
                         entity_id=draft_id, occurred_at_utc=now)
        self.queue.complete(job_id=job.id, worker_id=worker, mutate=write,
                            events=[OutgoingEvent("draft.ready", "draft", draft_id, "content.critic",
                                                  {"draft_id": draft_id}, f"content:draft:{draft_id}:ready")])
        return {"job_id": job.id, "draft_id": draft_id, "status": "review_pending"}

    def revise(self, *, draft_id: str, subject: str | None, body: str, actor: str) -> str:
        if not actor.strip() or not isinstance(body, str) or not body.strip() or len(body) > 12000:
            raise ValueError("actor and nonempty bounded body required")
        if subject is not None and len(subject) > 300:
            raise ValueError("subject too long")
        now, new_id = self._now(), uuid4().hex
        with self.db.transaction() as c:
            old = c.execute("SELECT * FROM drafts WHERE id=? AND request_id IS NOT NULL", (draft_id,)).fetchone()
            if not old or old["status"] == "retired":
                raise ValueError("current Department 3 draft required")
            latest = c.execute("SELECT MAX(version) FROM drafts WHERE request_id=?", (old["request_id"],)).fetchone()[0]
            if old["version"] != latest:
                raise ValueError("only the latest draft can be revised")
            c.execute("UPDATE drafts SET status='retired' WHERE id=?", (draft_id,))
            c.execute("UPDATE approvals SET decision='revoked',reason='draft revised' WHERE draft_id=? "
                      "AND decision='approved'", (draft_id,))
            c.execute("INSERT INTO drafts(id,prospect_id,contact_id,campaign_id,channel,subject,body,content_hash,"
                      "version,evidence_json,author_role,status,created_at_utc,request_id,parent_draft_id,input_hash,"
                      "template_id) VALUES (?,?,?,?,?,?,?,?,?,?,?,'review_pending',?,?,?,?,?)",
                      (new_id, old["prospect_id"], old["contact_id"], old["campaign_id"], old["channel"],
                       subject, body, content_hash(subject, body), old["version"] + 1, old["evidence_json"],
                       "3A-revision" if old["author_role"].startswith("3A") else "3B-revision", now,
                       old["request_id"], draft_id, old["input_hash"], old["template_id"]))
            append_audit(c, actor=actor, event_type="content.draft_revised", entity_type="draft",
                         entity_id=new_id, details_json=canonical({"previous_draft_id": draft_id}),
                         occurred_at_utc=now)
        return new_id

    def audit_revision(self, *, draft_id: str, reviewer: str, rationale: str) -> str:
        """Human confirms every factual assertion in a free-form revision against its evidence packet."""
        if not reviewer.strip() or len(rationale.strip()) < 20:
            raise ValueError("reviewer and substantive claim-audit rationale required")
        now, audit_id = self._now(), uuid4().hex
        with self.db.transaction() as c:
            draft = c.execute("SELECT * FROM drafts WHERE id=? AND request_id IS NOT NULL",
                              (draft_id,)).fetchone()
            if not draft or draft["status"] not in {"blocked", "review_pending"} or not draft["author_role"].endswith("-revision"):
                raise ValueError("a current free-form revision is required")
            _, _, _, current_hash = self._context(c, draft["request_id"])
            if current_hash != draft["input_hash"] or content_hash(draft["subject"], draft["body"]) != draft["content_hash"]:
                raise ValueError("draft evidence or content changed; create a new version")
            existing = c.execute("SELECT id FROM content_claim_audits WHERE draft_id=? AND content_hash=? "
                                 "AND input_hash=?", (draft_id, draft["content_hash"], draft["input_hash"])).fetchone()
            if existing:
                return existing["id"]
            c.execute("INSERT INTO content_claim_audits(id,draft_id,content_hash,input_hash,reviewer,rationale,"
                      "reviewed_at_utc) VALUES (?,?,?,?,?,?,?)",
                      (audit_id, draft_id, draft["content_hash"], draft["input_hash"], reviewer,
                       rationale.strip(), now))
            c.execute("UPDATE drafts SET status='review_pending' WHERE id=?", (draft_id,))
            append_audit(c, actor=reviewer, event_type="content.revision_claims_audited",
                         entity_type="draft", entity_id=draft_id, occurred_at_utc=now)
        self.queue.enqueue(workflow="content.critique", lane="content.3D",
                           payload={"draft_id": draft_id},
                           logical_key=f"content:critique:{draft_id}:audit:{audit_id}")
        return audit_id

    def _critique(self, job: Job, worker: str) -> dict:
        draft_id = job.payload["draft_id"]
        with self.db.read() as c:
            draft = c.execute("SELECT * FROM drafts WHERE id=?", (draft_id,)).fetchone()
            if not draft or draft["status"] != "review_pending" or not draft["request_id"]:
                raise ValueError("reviewable Department 3 draft required")
            audit = c.execute("SELECT id FROM content_claim_audits WHERE draft_id=? AND content_hash=? "
                              "AND input_hash=?", (draft_id, draft["content_hash"],
                                                    draft["input_hash"])).fetchone()
            try:
                template, values, _, current_hash = self._context(c, draft["request_id"])
                expected_subject = render_template(template["subject_template"], values)
                expected_body = render_template(template["body_template"], values)
                evidence_ok = current_hash == draft["input_hash"]
            except ValueError:
                expected_subject, expected_body, evidence_ok = None, "", False
            findings = critique(subject=draft["subject"], body=draft["body"],
                                expected_subject=expected_subject, expected_body=expected_body or "",
                                evidence_ok=evidence_ok,
                                trusted_template=draft["author_role"] in {"3A", "3B"},
                                human_claim_audit=bool(audit))
            if content_hash(draft["subject"], draft["body"]) != draft["content_hash"]:
                findings.append({"code": "content_hash_mismatch", "severity": "blocking",
                                 "detail": "Stored content hash does not match content."})
        decision = "blocked" if findings else "passed"
        now, review_id = self._now(), uuid4().hex
        def write(c: sqlite3.Connection) -> None:
            current = c.execute("SELECT status,content_hash,input_hash FROM drafts WHERE id=?", (draft_id,)).fetchone()
            if not current or current["status"] != "review_pending" or current["content_hash"] != draft["content_hash"]:
                raise ValueError("draft changed before review commit")
            # A concurrent claim/template change must not allow a stale pass.
            if decision == "passed":
                _, _, _, latest_hash = self._context(c, draft["request_id"])
                if latest_hash != current["input_hash"]:
                    raise ValueError("evidence changed before review commit")
            review_round = c.execute("SELECT COALESCE(MAX(review_round),0)+1 FROM content_reviews "
                                     "WHERE draft_id=?", (draft_id,)).fetchone()[0]
            c.execute("INSERT INTO content_reviews(id,draft_id,content_hash,input_hash,rule_version,"
                      "review_round,claim_audit_id,decision,reviewer_role,reviewed_at_utc) "
                      "VALUES (?,?,?,?,?,?,?,?,?,?)",
                      (review_id, draft_id, draft["content_hash"], draft["input_hash"],
                       RULE_VERSION, review_round, audit["id"] if audit else None,
                       decision, "3D", now))
            for finding in findings:
                c.execute("INSERT INTO review_findings(id,draft_id,reviewer_role,rule_version,severity,"
                          "finding,disposition,created_at_utc,content_review_id) "
                          "VALUES (?,?,'3D',?,?,?,?,?,?)",
                          (uuid4().hex, draft_id, RULE_VERSION, finding["severity"],
                           finding["code"] + ": " + finding["detail"], "blocked", now, review_id))
            c.execute("UPDATE drafts SET status=? WHERE id=?",
                      ("blocked" if findings else "review_passed", draft_id))
            append_audit(c, actor=worker, event_type="content.critique_completed", entity_type="draft",
                         entity_id=draft_id, details_json=canonical({"decision": decision}), occurred_at_utc=now)
        destination = "policy.review" if decision == "passed" else "content.blocked"
        self.queue.complete(job_id=job.id, worker_id=worker, mutate=write,
                            events=[OutgoingEvent("draft.review_passed" if decision == "passed" else "draft.blocked",
                                                  "draft", draft_id, destination,
                                                  {"draft_id": draft_id, "content_hash": draft["content_hash"],
                                                   "input_hash": draft["input_hash"], "review_id": review_id},
                                                  f"content:review:{draft_id}:{RULE_VERSION}:{review_id}")])
        return {"job_id": job.id, "draft_id": draft_id, "status": decision,
                "findings": findings}

    def consume_critic_once(self, *, worker_id: str = "content-critic-handoff") -> dict | None:
        item = self.outbox.claim(worker_id=worker_id, destination="content.critic")
        if item is None:
            return None
        try:
            if item.event_type != "draft.ready" or item.schema_version != 1:
                raise ValueError("invalid critic handoff")
            draft_id = item.payload["draft_id"]
            with self.db.read() as c:
                if not c.execute("SELECT 1 FROM drafts WHERE id=? AND status='review_pending'",
                                 (draft_id,)).fetchone():
                    raise ValueError("draft not ready")
            self.queue.enqueue(workflow="content.critique", lane="content.3D",
                               payload={"draft_id": draft_id}, logical_key=f"content:critique:{draft_id}")
            self.outbox.acknowledge(item_id=item.id, worker_id=worker_id)
            return {"draft_id": draft_id, "status": "queued"}
        except (KeyError, ValueError, sqlite3.Error):
            self.outbox.fail(item_id=item.id, worker_id=worker_id, error_class="invalid_critic_handoff")
            return {"event_id": item.id, "status": "needs_review"}

    def create_experiment(self, *, campaign_id: str, metric_key: str, minimum_exposures: int,
                          minimum_conversions: int, window_start_at_utc: str,
                          window_end_at_utc: str, actor: str) -> str:
        if not all(x.strip() for x in (campaign_id, metric_key, actor)) or minimum_exposures < 1 or minimum_conversions < 1:
            raise ValueError("campaign, metric, actor and positive thresholds required")
        try:
            start = datetime.fromisoformat(window_start_at_utc.replace("Z", "+00:00"))
            end = datetime.fromisoformat(window_end_at_utc.replace("Z", "+00:00"))
            if start.tzinfo is None or end.tzinfo is None or start >= end:
                raise ValueError("invalid experiment window")
        except (TypeError, ValueError) as exc:
            raise ValueError("timezone-aware ordered experiment window required") from exc
        start_text, end_text = utc_text(start.astimezone(timezone.utc)), utc_text(end.astimezone(timezone.utc))
        now, experiment_id = self._now(), uuid4().hex
        with self.db.transaction() as c:
            campaign = c.execute("SELECT status FROM campaigns WHERE id=?", (campaign_id,)).fetchone()
            count = c.execute("SELECT COUNT(*) FROM campaign_variants WHERE campaign_id=?", (campaign_id,)).fetchone()[0]
            if not campaign or campaign["status"] not in {"approved", "active"} or count < 2:
                raise ValueError("approved/active campaign with at least two variants required")
            c.execute("INSERT INTO content_experiments(id,campaign_id,metric_key,window_start_at_utc,"
                      "window_end_at_utc,minimum_exposures,minimum_conversions,status,created_by,created_at_utc) "
                      "VALUES (?,?,?,?,?,?,?,'active',?,?)",
                      (experiment_id, campaign_id, metric_key, start_text, end_text,
                       minimum_exposures, minimum_conversions, actor, now))
            append_audit(c, actor=actor, event_type="content.experiment_created",
                         entity_type="content_experiment", entity_id=experiment_id, occurred_at_utc=now)
        return experiment_id

    def record_exposure(self, *, experiment_id: str, variant_key: str, subject_key: str,
                        actor: str) -> str:
        if not all(x.strip() for x in (experiment_id, variant_key, subject_key, actor)):
            raise ValueError("experiment, variant, subject and actor required")
        now, exposure_id = self._now(), uuid4().hex
        with self.db.transaction() as c:
            exp = c.execute("SELECT campaign_id,status,window_start_at_utc,window_end_at_utc "
                            "FROM content_experiments WHERE id=?",
                            (experiment_id,)).fetchone()
            if not exp or exp["status"] != "active":
                raise ValueError("active experiment required")
            if not exp["window_start_at_utc"] <= now <= exp["window_end_at_utc"]:
                raise ValueError("exposure is outside predeclared experiment window")
            variant = c.execute("SELECT id FROM campaign_variants WHERE campaign_id=? AND variant_key=?",
                                (exp["campaign_id"], variant_key)).fetchone()
            if not variant:
                raise ValueError("variant not in experiment campaign")
            existing = c.execute("SELECT id,variant_id FROM content_exposures WHERE experiment_id=? AND subject_key=?",
                                 (experiment_id, subject_key)).fetchone()
            if existing:
                if existing["variant_id"] != variant["id"]:
                    raise ValueError("subject already exposed to a different variant")
                return existing["id"]
            c.execute("INSERT INTO content_exposures(id,experiment_id,variant_id,subject_key,observed_at_utc) "
                      "VALUES (?,?,?,?,?)", (exposure_id, experiment_id, variant["id"], subject_key, now))
            append_audit(c, actor=actor, event_type="content.exposure_recorded",
                         entity_type="content_exposure", entity_id=exposure_id, occurred_at_utc=now)
        return exposure_id

    def record_outcome(self, *, exposure_id: str, outcome_key: str, actor: str) -> str:
        if not all(x.strip() for x in (exposure_id, outcome_key, actor)):
            raise ValueError("exposure, outcome key and actor required")
        now, outcome_id = self._now(), uuid4().hex
        with self.db.transaction() as c:
            exposure = c.execute("SELECT e.metric_key,e.window_end_at_utc,x.id AS exposure_id "
                                 "FROM content_experiments e "
                                 "JOIN content_exposures x ON x.experiment_id=e.id WHERE x.id=?",
                                 (exposure_id,)).fetchone()
            if not exposure:
                raise ValueError("recorded exposure required before outcome")
            if now > exposure["window_end_at_utc"]:
                raise ValueError("outcome is outside predeclared experiment window")
            existing = c.execute("SELECT id,outcome_key FROM content_outcomes WHERE exposure_id=? AND metric_key=?",
                                 (exposure_id, exposure["metric_key"])).fetchone()
            if existing:
                if existing["outcome_key"] != outcome_key:
                    raise ValueError("exposure already has an outcome")
                return existing["id"]
            c.execute("INSERT INTO content_outcomes(id,exposure_id,metric_key,outcome_key,observed_at_utc) "
                      "VALUES (?,?,?,?,?)", (outcome_id, exposure_id, exposure["metric_key"], outcome_key, now))
            append_audit(c, actor=actor, event_type="content.outcome_recorded",
                         entity_type="content_outcome", entity_id=outcome_id, occurred_at_utc=now)
        return outcome_id

    def analyze_experiment(self, *, experiment_id: str) -> dict:
        with self.db.read() as c:
            exp = c.execute("SELECT * FROM content_experiments WHERE id=?", (experiment_id,)).fetchone()
            if not exp:
                raise ValueError("experiment not found")
            rows = [dict(r) for r in c.execute(
                "SELECT v.variant_key,COUNT(x.id) AS exposures,COUNT(o.id) AS conversions "
                "FROM campaign_variants v LEFT JOIN content_exposures x ON x.variant_id=v.id "
                "AND x.experiment_id=? LEFT JOIN content_outcomes o ON o.exposure_id=x.id "
                "AND o.metric_key=? WHERE v.campaign_id=? GROUP BY v.id,v.variant_key ORDER BY v.variant_key",
                (experiment_id, exp["metric_key"], exp["campaign_id"])).fetchall()]
        result = analyze_variants(rows, minimum_exposures=exp["minimum_exposures"],
                                  minimum_conversions=exp["minimum_conversions"])
        analysis_hash, now = digest({"experiment_id": experiment_id, "rows": rows,
                                     "thresholds": [exp["minimum_exposures"], exp["minimum_conversions"]]}), self._now()
        with self.db.transaction() as c:
            c.execute("INSERT OR IGNORE INTO content_recommendations(id,experiment_id,analysis_hash,result_json,"
                      "state,created_at_utc) VALUES (?,?,?,?,?,?)",
                      (uuid4().hex, experiment_id, analysis_hash, canonical(result),
                       "review_required" if result["status"] == "recommendation" else "insufficient_evidence", now))
        return {"experiment_id": experiment_id, "analysis_hash": analysis_hash, **result}

    def report(self, *, limit: int = 30) -> dict:
        if not 1 <= limit <= 1000:
            raise ValueError("limit must be 1..1000")
        with self.db.read() as c:
            result = {}
            for key, query in {
                "intakes": "SELECT * FROM content_intakes ORDER BY created_at_utc DESC LIMIT ?",
                "requests": "SELECT * FROM content_requests ORDER BY requested_at_utc DESC LIMIT ?",
                "drafts": "SELECT id,request_id,parent_draft_id,version,channel,content_hash,status,created_at_utc FROM drafts WHERE request_id IS NOT NULL ORDER BY created_at_utc DESC LIMIT ?",
                "reviews": "SELECT * FROM content_reviews ORDER BY reviewed_at_utc DESC LIMIT ?",
                "experiments": "SELECT * FROM content_experiments ORDER BY created_at_utc DESC LIMIT ?",
                "recommendations": "SELECT * FROM content_recommendations ORDER BY created_at_utc DESC LIMIT ?",
            }.items():
                result[key] = [dict(r) for r in c.execute(query, (limit,)).fetchall()]
        return result

    def brief(self, request_id: str) -> dict:
        """Bounded, approved evidence packet for a Codex 3A/3B role; never a send instruction."""
        with self.db.read() as c:
            request = c.execute("SELECT kind,prospect_id,contact_id FROM content_requests WHERE id=?",
                                (request_id,)).fetchone()
            if not request:
                raise ValueError("content request not found")
            template, values, evidence, input_hash = self._context(c, request_id)
        return {"request_id": request_id, "role": "3A" if request["kind"] == "personalized" else "3B",
                "kind": request["kind"], "channel": template["channel"],
                "subject_template": template["subject_template"], "body_template": template["body_template"],
                "approved_values": values, "evidence": evidence, "input_hash": input_hash,
                "boundary": "Draft only. Do not send. Use only approved facts; free-form wording needs human claim audit and 3D critique."}

    def draft(self, draft_id: str) -> dict:
        with self.db.read() as c:
            row = c.execute("SELECT * FROM drafts WHERE id=? AND request_id IS NOT NULL", (draft_id,)).fetchone()
            if not row:
                raise ValueError("Department 3 draft not found")
            findings = [dict(r) for r in c.execute("SELECT * FROM review_findings WHERE draft_id=?",
                                                  (draft_id,)).fetchall()]
            reviews = [dict(r) for r in c.execute("SELECT * FROM content_reviews WHERE draft_id=?",
                                                 (draft_id,)).fetchall()]
            audits = [dict(r) for r in c.execute("SELECT * FROM content_claim_audits WHERE draft_id=?",
                                                (draft_id,)).fetchall()]
        draft = dict(row)
        draft["evidence"] = json.loads(draft.pop("evidence_json"))
        latest_review = reviews[-1] if reviews else None
        return {"draft": draft, "findings": findings, "reviews": reviews, "claim_audits": audits,
                "current_findings": [f for f in findings if latest_review and
                                     f["content_review_id"] == latest_review["id"]]}

    def run_loop(self, *, interval_seconds: int = 15) -> None:
        if interval_seconds <= 0:
            raise ValueError("positive interval required")
        while True:
            try:
                while self.consume_contact_once() is not None:
                    pass
                while self.consume_critic_once() is not None:
                    pass
                self.tick()
                while self.work_once() is not None:
                    pass
            except PausedError:
                pass
            time.sleep(interval_seconds)
