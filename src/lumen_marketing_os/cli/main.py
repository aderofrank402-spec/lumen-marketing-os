from __future__ import annotations

import argparse
import json
import os
import sqlite3
import sys
from dataclasses import asdict
from datetime import datetime
from pathlib import Path
from typing import Sequence
from zoneinfo import ZoneInfo

from lumen_marketing_os.config import load_settings
from lumen_marketing_os.core.errors import LmosError
from lumen_marketing_os.db.database import Database
from lumen_marketing_os.engine.outbox import Outbox
from lumen_marketing_os.engine.queue import Job, JobQueue
from lumen_marketing_os.policy.pause import PauseControl
from lumen_marketing_os.workflows.research.runtime import ResearchRuntime, load_local_env
from lumen_marketing_os.workflows.research.handoff import ResearchHandoffRuntime
from lumen_marketing_os.workflows.qualification.runtime import QualificationRuntime
from lumen_marketing_os.workflows.content.runtime import ContentRuntime
from lumen_marketing_os.workflows.outreach.runtime import IncomingMail, OutreachRuntime
from lumen_marketing_os.workflows.operations.scheduler import ScheduleManager, WORKFLOWS
from lumen_marketing_os.workflows.operations.ledger import OperationsLedger
from lumen_marketing_os.workflows.community.runtime import CommunityRuntime
from lumen_marketing_os.workflows.community.indexer import ProspectListIndexer
from lumen_marketing_os.workflows.audit.runtime import AuditRuntime
from lumen_marketing_os.workflows.discovery.runtime import DiscoveryRuntime
from lumen_marketing_os.workflows.support.runtime import SupportRuntime
from lumen_marketing_os.adapters.research import HackerNewsClient, HttpClient, SourceError


def _parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(prog="lmos", description="Lumen Marketing OS local foundation")
    parser.add_argument("--workspace", type=Path, default=Path.cwd())
    parser.add_argument("--environment", choices=("development", "staging", "live"))
    commands = parser.add_subparsers(dest="command", required=True)
    commands.add_parser("init", help="apply versioned database migrations")
    status = commands.add_parser("status", help="show local runtime and pause status")
    status.add_argument("--lane", help="also check a specific lane")
    commands.add_parser("check", help="run SQLite integrity and foreign-key checks")

    backup = commands.add_parser("backup", help="write and verify a consistent SQLite backup")
    backup.add_argument("destination", type=Path)

    for name in ("pause", "resume"):
        cmd = commands.add_parser(name, help=f"{name} global work or one lane")
        cmd.add_argument("--lane")
        cmd.add_argument("--actor", required=True)
        cmd.add_argument("--reason", required=True)

    enqueue = commands.add_parser("enqueue", help="put a durable job in the queue")
    enqueue.add_argument("workflow")
    enqueue.add_argument("--lane", required=True)
    enqueue.add_argument("--key", required=True)
    enqueue.add_argument("--payload", default="{}", help="JSON object")
    enqueue.add_argument("--due-at", help="timezone-aware ISO 8601 timestamp")
    enqueue.add_argument("--priority", type=int, default=0)
    enqueue.add_argument("--max-attempts", type=int)
    enqueue.add_argument("--effect", choices=("internal", "external"), default="internal")

    jobs = commands.add_parser("jobs", help="list recent job metadata")
    jobs.add_argument("--state")
    jobs.add_argument("--limit", type=int, default=50)

    claim = commands.add_parser("claim", help="lease one due job to a worker")
    claim.add_argument("--worker", required=True)
    claim.add_argument("--lane")
    claim.add_argument("--lease-seconds", type=int)

    heartbeat = commands.add_parser("heartbeat", help="renew a live job lease")
    heartbeat.add_argument("job_id")
    heartbeat.add_argument("--worker", required=True)
    heartbeat.add_argument("--lease-seconds", type=int)

    complete = commands.add_parser("complete", help="mark a leased job complete")
    complete.add_argument("job_id")
    complete.add_argument("--worker", required=True)

    fail = commands.add_parser("fail", help="record a leased job failure")
    fail.add_argument("job_id")
    fail.add_argument("--worker", required=True)
    fail.add_argument("--error-class", required=True)
    fail.add_argument("--retryable", action="store_true")
    fail.add_argument("--unknown-effect", action="store_true")

    replay = commands.add_parser("replay", help="requeue a reviewed failed job")
    replay.add_argument("job_id")
    replay.add_argument("--actor", required=True)
    replay.add_argument("--reason", required=True)
    replay.add_argument("--external-effect-ruled-out", action="store_true")

    commands.add_parser("recover", help="recover expired job and outbox leases")
    research = commands.add_parser("research", help="Department 1 research agents")
    research_commands = research.add_subparsers(dest="research_command", required=True)
    research_commands.add_parser("tick", help="enqueue due Department 1 scans")
    research_commands.add_parser("run", help="enqueue due scans and work until the research queue is empty")
    research_commands.add_parser("work", help="work one queued Department 1 job")
    research_commands.add_parser("daemon", help="keep Department 1 scheduled scans running locally")
    report = research_commands.add_parser("report", help="show source health and research findings")
    report.add_argument("--limit", type=int, default=30)
    finding = research_commands.add_parser("finding", help="show one finding with evidence")
    finding.add_argument("finding_id")
    reddit_import = research_commands.add_parser(
        "import-reddit-observation", help="promote one leased 6A Reddit observation to 1B review")
    reddit_import.add_argument("task_id")
    reddit_import.add_argument("observation_id")
    reddit_import.add_argument("--worker", required=True)
    reddit_import.add_argument("--analysis-file", type=Path, required=True)
    review = research_commands.add_parser("review", help="mark a finding reviewed or rejected")
    review.add_argument("finding_id")
    review.add_argument("--reviewer", required=True)
    review.add_argument("--decision", choices=("reviewed", "rejected"), required=True)
    review_dispatch = research_commands.add_parser("review-dispatch", help="assign pending findings to durable Codex role lanes")
    review_dispatch.add_argument("--agent", choices=("1A", "1B", "1C", "1E"))
    review_dispatch.add_argument("--limit", type=int, default=10)
    review_claim = research_commands.add_parser("review-claim", help="lease one finding for a Codex specialist")
    review_claim.add_argument("agent_id", choices=("1A", "1B", "1C", "1E"))
    review_claim.add_argument("--worker", required=True)
    review_claim.add_argument("--lease-seconds", type=int, default=1800)
    review_complete = research_commands.add_parser("review-complete", help="commit a sourced specialist assessment")
    review_complete.add_argument("job_id")
    review_complete.add_argument("--worker", required=True)
    review_complete.add_argument("--reviewer", required=True)
    review_complete.add_argument("--agent-run-ref", required=True)
    review_complete.add_argument("--decision", choices=("reviewed", "rejected"), required=True)
    review_complete.add_argument("--assessment-file", type=Path, required=True)
    review_fail = research_commands.add_parser("review-fail", help="record an unavailable or invalid specialist review")
    review_fail.add_argument("job_id")
    review_fail.add_argument("--worker", required=True)
    review_fail.add_argument("--reason", required=True)
    review_fail.add_argument("--permanent", action="store_true")
    review_report = research_commands.add_parser("review-report", help="show specialist review queue and outcomes")
    review_report.add_argument("--limit", type=int, default=30)
    screen_legacy = research_commands.add_parser("screen-legacy", help="quarantine old irrelevant 1E headlines before specialist review")
    screen_legacy.add_argument("--actor", required=True)
    screen_legacy.add_argument("--limit", type=int, default=100)
    screen_report = research_commands.add_parser("screen-report", help="inspect reversible legacy intake exclusions")
    screen_report.add_argument("--limit", type=int, default=100)
    screen_restore = research_commands.add_parser("screen-restore", help="return one screened finding to specialist review")
    screen_restore.add_argument("finding_id")
    screen_restore.add_argument("--actor", required=True)
    screen_restore.add_argument("--reason", required=True)
    handoff_run = research_commands.add_parser("handoff-run", help="route specialist-reviewed findings to internal destinations")
    handoff_run.add_argument("--limit", type=int, default=20)
    research_commands.add_parser("handoff-report", help="show pending and completed Department 1 handoffs")
    research_commands.add_parser("handoff-reconcile", help="quarantine undated pending community handoff tasks")
    intake_dismiss = research_commands.add_parser("handoff-dismiss", help="dismiss a manual handoff intake")
    intake_dismiss.add_argument("intake_id")
    intake_dismiss.add_argument("--reviewer", required=True)
    intake_dismiss.add_argument("--reason", required=True)
    intake_retry = research_commands.add_parser("handoff-retry", help="retry one manual community handoff after connection repair")
    intake_retry.add_argument("intake_id")
    intake_retry.add_argument("--actor", required=True)
    promote = research_commands.add_parser("promote-organization", help="verify a 1E company identity and hand it to qualification")
    promote.add_argument("finding_id")
    promote.add_argument("--name", required=True)
    promote.add_argument("--domain", required=True)
    promote.add_argument("--reviewer", required=True)
    research_commands.add_parser("credentials", help="show configured credential names, never values")
    qualification = commands.add_parser("qualification", help="Department 2 lead qualification")
    qc = qualification.add_subparsers(dest="qualification_command", required=True)
    qc.add_parser("consume", help="ingest one reviewed Department 1 organization handoff")
    qc.add_parser("tick", help="enqueue due lookup, poll and contact work")
    qc.add_parser("run", help="consume handoffs, enqueue due work, and process available jobs")
    qc.add_parser("daemon", help="run Department 2 locally until stopped")
    qc.add_parser("work", help="process one queued Department 2 job")
    qr = qc.add_parser("report", help="show intake and qualification status")
    qr.add_argument("--limit", type=int, default=30)
    qp = qc.add_parser("prospect", help="show a prospect with attempts, scores, contacts")
    qp.add_argument("prospect_id")
    ar = qc.add_parser("approve-rubric", help="approve current versioned rubric configuration")
    ar.add_argument("--reviewer", required=True)
    uc = qc.add_parser("approve-use-case", help="approve a sourced research use case")
    uc.add_argument("finding_id")
    uc.add_argument("--vertical", required=True)
    uc.add_argument("--problem", required=True)
    uc.add_argument("--buyer", required=True)
    uc.add_argument("--reviewer", required=True)
    admit = qc.add_parser("admit", help="admit reviewed organization to a segment")
    admit.add_argument("intake_id")
    admit.add_argument("--segment", required=True)
    admit.add_argument("--reviewer", required=True)
    admit.add_argument("--country-code")
    admit.add_argument("--location-hint")
    admit.add_argument("--use-case-id")
    admit.add_argument("--pain-evidence-url")
    admit.add_argument("--pain-evidence-note")
    reject = qc.add_parser("reject-intake", help="reject a reviewed organization handoff")
    reject.add_argument("intake_id")
    reject.add_argument("--reviewer", required=True)
    reject.add_argument("--reason", required=True)
    link_case = qc.add_parser("link-use-case", help="attach an approved use case to a prospect")
    link_case.add_argument("prospect_id")
    link_case.add_argument("use_case_id")
    link_case.add_argument("--reviewer", required=True)
    link_case.add_argument("--pain-evidence-url", required=True)
    link_case.add_argument("--pain-evidence-note", required=True)
    rescore = qc.add_parser("rescore", help="reassess a saved profile under the current rubric")
    rescore.add_argument("prospect_id")
    rescore.add_argument("--reviewer", required=True)
    retry_lookup = qc.add_parser("retry-lookup", help="approve a new lookup after provider reconciliation")
    retry_lookup.add_argument("prospect_id")
    retry_lookup.add_argument("--reviewer", required=True)
    retry_lookup.add_argument("--reason", required=True)
    retry_lookup.add_argument("--provider-effect-reconciled", action="store_true")
    add_contact = qc.add_parser("add-contact", help="record a sourced named contact candidate")
    add_contact.add_argument("prospect_id")
    add_contact.add_argument("--name", required=True)
    add_contact.add_argument("--role", required=True)
    add_contact.add_argument("--email", required=True)
    add_contact.add_argument("--source-url", required=True)
    add_contact.add_argument("--reviewer", required=True)
    verify = qc.add_parser("verify-contact", help="review a sourced named contact")
    verify.add_argument("contact_id")
    verify.add_argument("--prospect-id", required=True)
    verify.add_argument("--source-url", required=True)
    verify.add_argument("--reviewer", required=True)

    content = commands.add_parser("content", help="Department 3 evidence-bound drafts, critique and A/B advice")
    cc = content.add_subparsers(dest="content_command", required=True)
    cc.add_parser("consume", help="ingest one verified-contact handoff")
    cc.add_parser("tick", help="enqueue due compose and critique work")
    cc.add_parser("work", help="process one Department 3 job")
    cc.add_parser("run", help="consume handoffs and process available Department 3 work")
    cc.add_parser("daemon", help="run Department 3 locally until stopped")
    cr = cc.add_parser("report", help="show content intakes, drafts, reviews and experiments")
    cr.add_argument("--limit", type=int, default=30)
    cd = cc.add_parser("draft", help="show one immutable draft and critique findings")
    cd.add_argument("draft_id")
    cb = cc.add_parser("brief", help="show bounded approved evidence packet for Codex 3A/3B")
    cb.add_argument("request_id")
    claim = cc.add_parser("register-claim", help="stage a sourced product or permitted customer claim")
    claim.add_argument("--key", required=True)
    claim.add_argument("--type", choices=("product", "customer"), required=True)
    claim.add_argument("--text", required=True)
    claim.add_argument("--evidence-ref", required=True)
    claim.add_argument("--permission-confirmed", action="store_true")
    claim.add_argument("--actor", required=True)
    approve_claim = cc.add_parser("approve-claim", help="approve one staged claim")
    approve_claim.add_argument("claim_id")
    approve_claim.add_argument("--reviewer", required=True)
    template = cc.add_parser("register-template", help="stage an evidence-bound template")
    template.add_argument("--key", required=True)
    template.add_argument("--kind", choices=("personalized", "technical", "case_study"), required=True)
    template.add_argument("--channel", required=True)
    template.add_argument("--subject")
    template.add_argument("--body-file", type=Path, required=True)
    template.add_argument("--actor", required=True)
    approve_template = cc.add_parser("approve-template", help="approve one staged template")
    approve_template.add_argument("template_id")
    approve_template.add_argument("--reviewer", required=True)
    request_content = cc.add_parser("request", help="request a 3A or 3B evidence-bound draft")
    request_content.add_argument("--kind", choices=("personalized", "technical", "case_study"), required=True)
    request_content.add_argument("--template-id", required=True)
    request_content.add_argument("--claim-id", action="append", required=True)
    request_content.add_argument("--intake-id")
    request_content.add_argument("--logical-key")
    request_content.add_argument("--actor", required=True)
    revise = cc.add_parser("revise", help="save a new version; free-form revision stays blocked pending claim review")
    revise.add_argument("draft_id")
    revise.add_argument("--subject")
    revise.add_argument("--body-file", type=Path, required=True)
    revise.add_argument("--actor", required=True)
    audit_revision = cc.add_parser("audit-revision", help="human-check all factual claims in a free-form revision")
    audit_revision.add_argument("draft_id")
    audit_revision.add_argument("--reviewer", required=True)
    audit_revision.add_argument("--rationale", required=True)
    experiment = cc.add_parser("create-experiment", help="start an advisory A/B experiment for an approved campaign")
    experiment.add_argument("campaign_id")
    experiment.add_argument("--metric", required=True)
    experiment.add_argument("--starts-at", required=True, help="timezone-aware ISO 8601 start")
    experiment.add_argument("--ends-at", required=True, help="timezone-aware ISO 8601 end")
    experiment.add_argument("--minimum-exposures", type=int, required=True)
    experiment.add_argument("--minimum-conversions", type=int, required=True)
    experiment.add_argument("--actor", required=True)
    exposure = cc.add_parser("record-exposure", help="record a subject's actual variant exposure")
    exposure.add_argument("experiment_id")
    exposure.add_argument("--variant", required=True)
    exposure.add_argument("--subject-key", required=True)
    exposure.add_argument("--actor", required=True)
    outcome = cc.add_parser("record-outcome", help="record an outcome tied to a prior exposure")
    outcome.add_argument("exposure_id")
    outcome.add_argument("--outcome-key", required=True)
    outcome.add_argument("--actor", required=True)
    analysis = cc.add_parser("analyze", help="produce a non-executing experiment recommendation")
    analysis.add_argument("experiment_id")
    outreach = commands.add_parser("outreach", help="Department 4 inbox and guarded actions")
    oc = outreach.add_subparsers(dest="outreach_command", required=True)
    oa = oc.add_parser("register-account")
    oa.add_argument("--email", required=True)
    oa.add_argument("--policy-version", required=True)
    oa.add_argument("--calendar-id")
    oa.add_argument("--actor", required=True)
    oe = oc.add_parser("enable-account")
    oe.add_argument("account_id")
    oe.add_argument("--actor", required=True)
    oe.add_argument("--confirmed", action="store_true")
    od = oc.add_parser("disable-account")
    od.add_argument("account_id")
    od.add_argument("--actor", required=True)
    od.add_argument("--reason", required=True)
    oq = oc.add_parser("set-quota")
    oq.add_argument("account_id")
    oq.add_argument("--max-units", type=int, required=True)
    oq.add_argument("--period-seconds", type=int, required=True)
    oq.add_argument("--policy-version", required=True)
    oq.add_argument("--actor", required=True)
    ow = oc.add_parser("set-send-window")
    ow.add_argument("account_id")
    ow.add_argument("--timezone", required=True)
    ow.add_argument("--start", required=True, help="HH:MM local")
    ow.add_argument("--end", required=True, help="HH:MM local")
    ow.add_argument("--weekdays", required=True, help="comma-separated 0=Monday..6=Sunday")
    ow.add_argument("--actor", required=True)
    osup = oc.add_parser("suppress")
    osup.add_argument("--type", choices=("address", "domain", "organization", "account"), required=True)
    osup.add_argument("--value", required=True)
    osup.add_argument("--reason", required=True)
    osup.add_argument("--actor", required=True)
    oi = oc.add_parser("ingest-file", help="ingest a local JSON inbox fixture; no provider call")
    oi.add_argument("account_id")
    oi.add_argument("--file", type=Path, required=True)
    oi.add_argument("--cursor")
    orc = oc.add_parser("review-classification")
    orc.add_argument("event_id")
    orc.add_argument("--label", required=True)
    orc.add_argument("--rationale", required=True)
    orc.add_argument("--actor", required=True)
    op = oc.add_parser("propose-response")
    op.add_argument("event_id")
    op.add_argument("account_id")
    op.add_argument("--subject", required=True)
    op.add_argument("--body-file", type=Path, required=True)
    op.add_argument("--slots-file", type=Path)
    op.add_argument("--author", required=True)
    opr = oc.add_parser("review-response")
    opr.add_argument("proposal_id")
    opr.add_argument("--reviewer", required=True)
    ops = oc.add_parser("prepare-send")
    ops.add_argument("draft_id")
    ops.add_argument("account_id")
    ops.add_argument("--recipient", required=True)
    ops.add_argument("--due-at")
    ops.add_argument("--source-message-id")
    ops.add_argument("--recipient-timezone", help="IANA zone for recipient; required for cold email")
    ops.add_argument("--timezone-source", choices=("verified", "approved_fallback"))
    ops.add_argument("--actor", required=True)
    ob = oc.add_parser("prepare-booking")
    ob.add_argument("account_id")
    ob.add_argument("--attendee", required=True)
    ob.add_argument("--title", required=True)
    ob.add_argument("--starts-at", required=True)
    ob.add_argument("--ends-at", required=True)
    ob.add_argument("--source-event-id")
    ob.add_argument("--actor", required=True)
    oap = oc.add_parser("approve")
    oap.add_argument("action_id")
    oap.add_argument("--expires-at", required=True)
    oap.add_argument("--reason", required=True)
    oap.add_argument("--actor", required=True)
    orev = oc.add_parser("revoke")
    orev.add_argument("action_id")
    orev.add_argument("--reason", required=True)
    orev.add_argument("--actor", required=True)
    orecon = oc.add_parser("reconcile")
    orecon.add_argument("action_id")
    orecon.add_argument("--actor", required=True)
    oc.add_parser("dispatch-once")
    recover_outreach = oc.add_parser("recover-dispatch")
    recover_outreach.add_argument("--older-than-seconds", type=int, default=600)
    oc.add_parser("report")
    action_detail = oc.add_parser("action", help="inspect exact action before approval")
    action_detail.add_argument("action_id")
    inbound_detail = oc.add_parser("inbound", help="inspect one inbound message locally")
    inbound_detail.add_argument("event_id")
    support = commands.add_parser("support", help="Department 9 feedback, triage and support")
    sc = support.add_subparsers(dest="support_command", required=True)
    sc.add_parser("tick", help="classify bounded new local tickets; no provider or send call")
    source = sc.add_parser("register-source")
    source.add_argument("--key", required=True)
    source.add_argument("--kind", choices=("gmail", "form", "product_event", "manual"), required=True)
    source.add_argument("--reference", required=True)
    source.add_argument("--account-id")
    source.add_argument("--actor", required=True)
    enabled = sc.add_parser("enable-source")
    enabled.add_argument("source_id")
    enabled.add_argument("--verified", action="store_true")
    enabled.add_argument("--actor", required=True)
    ingest9 = sc.add_parser("ingest-file", help="ingest an authorized connector JSON page")
    ingest9.add_argument("source_id")
    ingest9.add_argument("--file", type=Path, required=True)
    ingest9.add_argument("--next-cursor")
    ingest9.add_argument("--actor", required=True)
    triage9 = sc.add_parser("triage")
    triage9.add_argument("ticket_id")
    triage9.add_argument("--actor", required=True)
    review9 = sc.add_parser("review")
    review9.add_argument("ticket_id")
    review9.add_argument("--category", required=True)
    review9.add_argument("--priority", required=True)
    review9.add_argument("--state", required=True)
    review9.add_argument("--rationale", required=True)
    review9.add_argument("--actor", required=True)
    draft9 = sc.add_parser("draft-response")
    draft9.add_argument("ticket_id")
    draft9.add_argument("--subject", required=True)
    draft9.add_argument("--body-file", type=Path, required=True)
    draft9.add_argument("--author", required=True)
    approve9 = sc.add_parser("approve-response")
    approve9.add_argument("draft_id")
    approve9.add_argument("--hash", required=True)
    approve9.add_argument("--reviewer", required=True)
    handoff9 = sc.add_parser("handoff-email", help="create a Department 4 reply proposal; does not send")
    handoff9.add_argument("draft_id")
    handoff9.add_argument("--actor", required=True)
    project9 = sc.add_parser("sheet-projection")
    project9.add_argument("ticket_id")
    project9.add_argument("--spreadsheet-id", required=True)
    project9.add_argument("--actor", required=True)
    ack9 = sc.add_parser("sheet-ack")
    ack9.add_argument("ticket_id")
    ack9.add_argument("--spreadsheet-id", required=True)
    ack9.add_argument("--version", type=int, required=True)
    ack9.add_argument("--hash", required=True)
    ack9.add_argument("--actor", required=True)
    sc.add_parser("report").add_argument("--limit", type=int, default=50)
    sc.add_parser("ticket").add_argument("ticket_id")

    discovery = commands.add_parser("discovery", help="Department 8 opportunity and prospect escalation")
    dc = discovery.add_subparsers(dest="discovery_command", required=True)
    dc.add_parser("report").add_argument("--limit", type=int, default=50)
    dc.add_parser("cycle").add_argument("--key", required=True)
    finish8 = dc.add_parser("finish-task")
    finish8.add_argument("task_id")
    finish8.add_argument("--outcome", choices=("completed", "blocked"), required=True)
    finish8.add_argument("--reason", required=True)
    finish8.add_argument("--actor", required=True)
    scout = dc.add_parser("scout")
    for flag in ("vertical", "problem", "buyer", "source-url", "excerpt"):
        scout.add_argument("--" + flag, required=True)
    evidence = dc.add_parser("evidence")
    evidence.add_argument("opportunity_id")
    evidence.add_argument("--polarity", choices=("support", "counter"), required=True)
    evidence.add_argument("--source-url", required=True)
    evidence.add_argument("--excerpt", required=True)
    validate = dc.add_parser("validate")
    validate.add_argument("opportunity_id")
    for flag in ("buyer-workflow", "product-fit", "limitations", "reviewer"):
        validate.add_argument("--" + flag, required=True)
    candidate = dc.add_parser("candidate")
    candidate.add_argument("opportunity_id")
    for flag in ("company", "domain", "country", "source-url", "pain-url", "pain-note"):
        candidate.add_argument("--" + flag, required=True)
    review8 = dc.add_parser("review-candidate")
    review8.add_argument("candidate_id")
    review8.add_argument("--decision", choices=("accept", "reject"), required=True)
    review8.add_argument("--reviewer", required=True)
    dispatch8 = dc.add_parser("dispatch")
    dispatch8.add_argument("opportunity_id")
    dispatch8.add_argument("--approver", required=True)

    operations = commands.add_parser("operations", help="Department 5 schedules, usage and recovery")
    op = operations.add_subparsers(dest="operations_command", required=True)
    schedule = op.add_parser("configure-schedule")
    schedule.add_argument("schedule_id")
    schedule.add_argument("workflow", choices=sorted(WORKFLOWS))
    schedule.add_argument("--interval-seconds", type=int, required=True)
    schedule.add_argument("--next-due-at", required=True)
    schedule.add_argument("--timezone", default="UTC")
    schedule.add_argument("--catch-up", choices=("skip", "one", "all"), default="one")
    schedule.add_argument("--owner", required=True)
    schedule.add_argument("--max-overlap", type=int, default=1)
    schedule.add_argument("--window-start")
    schedule.add_argument("--window-end")
    schedule.add_argument("--weekdays", default="0,1,2,3,4,5,6")
    schedule.add_argument("--expected-version")
    activate = op.add_parser("set-schedule-active")
    activate.add_argument("schedule_id")
    activate.add_argument("--active", action="store_true")
    activate.add_argument("--actor", required=True)
    op.add_parser("tick")
    op.add_parser("work")
    op.add_parser("run")
    daemon = op.add_parser("daemon")
    daemon.add_argument("--poll-seconds", type=int, default=30)
    op.add_parser("report")
    op.add_parser("health")
    price = op.add_parser("approve-price")
    price.add_argument("provider")
    price.add_argument("model")
    price.add_argument("currency")
    price.add_argument("--prompt-per-million-minor", type=int, required=True)
    price.add_argument("--completion-per-million-minor", type=int, required=True)
    price.add_argument("--request-minor", type=int, required=True)
    price.add_argument("--effective-at", required=True)
    price.add_argument("--actor", required=True)
    usage = op.add_parser("record-usage")
    usage.add_argument("usage_key")
    usage.add_argument("provider")
    usage.add_argument("model")
    usage.add_argument("--prompt-tokens", type=int)
    usage.add_argument("--completion-tokens", type=int)
    usage.add_argument("--requests", type=int)
    usage.add_argument("--occurred-at", required=True)
    usage.add_argument("--job-id")
    reconciliation = op.add_parser("reconcile-usage")
    reconciliation.add_argument("usage_key")
    reconciliation.add_argument("--actual-minor", type=int, required=True)
    reconciliation.add_argument("--currency", required=True)
    reconciliation.add_argument("--actor", required=True)
    op.add_parser("ledger-report")
    verified_backup = op.add_parser("verified-backup")
    verified_backup.add_argument("destination", type=Path)
    drill = op.add_parser("restore-drill")
    drill.add_argument("backup_id")
    community = commands.add_parser("community", help="Department 6 Codex community handoffs")
    cc6 = community.add_subparsers(dest="community_command", required=True)
    hn_scan = cc6.add_parser("hn-scan", help="read current public HN stories; no login or posting")
    hn_scan.add_argument("--group", choices=("askstories", "showstories", "topstories"),
                         default="askstories")
    hn_scan.add_argument("--limit", type=int, default=10)
    hn_item = cc6.add_parser("hn-item", help="read one public HN item and bounded discussion context")
    hn_item.add_argument("item_id", type=int)
    hn_item.add_argument("--max-comments", type=int, default=3)
    trig = cc6.add_parser("configure-trigger")
    trig.add_argument("trigger_id")
    trig.add_argument("agent_id", choices=("6A", "6B", "6C", "6D", "6E"))
    trig.add_argument("--kind", choices=("interval", "event", "manual"), required=True)
    trig.add_argument("--owner", required=True)
    trig.add_argument("--input-file", type=Path, required=True)
    trig.add_argument("--interval-seconds", type=int)
    trig.add_argument("--next-due-at")
    trig.add_argument("--expected-version")
    active6 = cc6.add_parser("set-trigger-active")
    active6.add_argument("trigger_id")
    active6.add_argument("--active", action="store_true")
    active6.add_argument("--actor", required=True)
    manual6 = cc6.add_parser("trigger")
    manual6.add_argument("agent_id", choices=("6A", "6B", "6C", "6D", "6E"))
    manual6.add_argument("--key", required=True)
    manual6.add_argument("--input-file", type=Path, required=True)
    manual6.add_argument("--trigger-id")
    cc6.add_parser("tick")
    claim6 = cc6.add_parser("claim")
    claim6.add_argument("agent_id", choices=("6A", "6B", "6C", "6D", "6E"))
    claim6.add_argument("--worker", required=True)
    claim6.add_argument("--lease-seconds", type=int, default=900)
    brief6 = cc6.add_parser("brief")
    brief6.add_argument("task_id")
    observe6 = cc6.add_parser("observe")
    observe6.add_argument("task_id")
    observe6.add_argument("--worker", required=True)
    observe6.add_argument("--platform", required=True)
    observe6.add_argument("--url", required=True)
    observe6.add_argument("--title", required=True)
    observe6.add_argument("--excerpt", required=True)
    observe6.add_argument("--observed-at", required=True)
    observe6.add_argument("--relevance", required=True)
    observe6.add_argument("--external-id")
    draft6 = cc6.add_parser("draft")
    draft6.add_argument("task_id")
    draft6.add_argument("--worker", required=True)
    draft6.add_argument("--type", required=True)
    draft6.add_argument("--body-file", type=Path, required=True)
    draft6.add_argument("--evidence-url", action="append", required=True)
    draft6.add_argument("--observation-id")
    draft_detail6 = cc6.add_parser("draft-detail", help="read one reviewable community draft")
    draft_detail6.add_argument("draft_id")
    finish6 = cc6.add_parser("finish")
    finish6.add_argument("task_id")
    finish6.add_argument("--worker", required=True)
    finish6.add_argument("--outcome", choices=("review", "no_match", "blocked"), required=True)
    finish6.add_argument("--reason", required=True)
    review6 = cc6.add_parser("review-draft")
    review6.add_argument("draft_id")
    review6.add_argument("--reviewer", required=True)
    review6.add_argument("--decision", choices=("approved", "rejected"), required=True)
    review6.add_argument("--note", required=True)
    review6.add_argument("--expected-hash", required=True)
    checklist6 = cc6.add_parser("review-launch-item")
    checklist6.add_argument("task_id")
    checklist6.add_argument("item_key")
    checklist6.add_argument("--state", choices=("open", "ready", "reviewed"), required=True)
    checklist6.add_argument("--actor", required=True)
    checklist6.add_argument("--evidence-ref")
    connection6 = cc6.add_parser("connection-status")
    connection6.add_argument("agent_id", choices=("6A", "6B", "6C", "6D", "6E"))
    connection6.add_argument("--platform", required=True)
    connection6.add_argument("--state", choices=("not_connected", "ready", "blocked"), required=True)
    connection6.add_argument("--account-reference")
    connection6.add_argument("--failure-class")
    connection6.add_argument("--verified", action="store_true")
    connection6.add_argument("--actor", required=True)
    import6 = cc6.add_parser("import-prospects")
    import6.add_argument("--file", type=Path, required=True)
    import6.add_argument("--source-name", required=True)
    import6.add_argument("--source-url", required=True)
    import6.add_argument("--source-version", required=True)
    import6.add_argument("--approved-by", required=True)
    import6.add_argument("--task-id")
    import6.add_argument("--worker")
    import_report6 = cc6.add_parser("import-report")
    import_report6.add_argument("--batch-id")
    report6 = cc6.add_parser("report")
    report6.add_argument("--limit", type=int, default=50)

    audit = commands.add_parser("audit", help="Department 7 audit and executive controls")
    ac = audit.add_subparsers(dest="audit_command", required=True)
    init_policy = ac.add_parser("init-policy", help="install the practical default 7A guard policy")
    init_policy.add_argument("--actor", default="system")
    add_policy = ac.add_parser("add-policy", help="create a versioned guard policy")
    add_policy.add_argument("--key", required=True)
    add_policy.add_argument("--rules-file", type=Path, required=True)
    add_policy.add_argument("--actor", required=True)
    add_policy.add_argument("--activate", action="store_true")
    activate_policy = ac.add_parser("activate-policy")
    activate_policy.add_argument("policy_id")
    activate_policy.add_argument("--actor", required=True)
    guard = ac.add_parser("guard", help="run 7A against an immutable draft")
    guard.add_argument("draft_id")
    guard.add_argument("--reviewer", default="7A")
    guard.add_argument("--checker-unavailable", action="store_true")
    health = ac.add_parser("record-health", help="record 7B provider/domain health evidence")
    health.add_argument("--channel", required=True)
    health.add_argument("--account-id", required=True)
    health.add_argument("--provider", required=True)
    health.add_argument("--status", choices=("healthy", "degraded", "unhealthy", "unknown"), required=True)
    health.add_argument("--reason", required=True)
    health.add_argument("--metrics", default="{}", help="JSON object")
    health.add_argument("--expires-at")
    health.add_argument("--actor", required=True)
    ac.add_parser("rate-report")
    integrity = ac.add_parser("integrity-scan")
    integrity.add_argument("--actor", default="7C")
    growth = ac.add_parser("growth-audit")
    growth.add_argument("--starts-at", required=True)
    growth.add_argument("--ends-at", required=True)
    growth.add_argument("--minimum-sample", type=int, default=20)
    growth.add_argument("--actor", default="7D")
    pipeline = ac.add_parser("pipeline-scan")
    pipeline.add_argument("--pending-lag-seconds", type=int, default=3600)
    pipeline.add_argument("--heartbeat-lag-seconds", type=int, default=300)
    pipeline.add_argument("--actor", default="7E")
    suppression = ac.add_parser("suppression-check")
    suppression.add_argument("--type", choices=("address", "domain", "organization", "account"), required=True)
    suppression.add_argument("--value", required=True)
    suppression.add_argument("--actor", default="7F")
    retention = ac.add_parser("retention-review")
    retention.add_argument("--actor", default="7F")
    full = ac.add_parser("full-scan")
    full.add_argument("--actor", default="department-7")
    resolve = ac.add_parser("resolve-finding")
    resolve.add_argument("finding_id")
    resolve.add_argument("--actor", required=True)
    resolve.add_argument("--resolution", required=True)
    audit_report = ac.add_parser("report")
    audit_report.add_argument("--limit", type=int, default=50)
    return parser


def _job_view(job: Job) -> dict:
    return {
        "id": job.id, "workflow": job.workflow, "lane": job.lane,
        "effect": job.effect, "logical_key": job.logical_key,
        "priority": job.priority, "due_at_utc": job.due_at_utc,
        "state": job.state, "lease_owner": job.lease_owner,
        "lease_expires_at_utc": job.lease_expires_at_utc,
        "attempt_count": job.attempt_count, "max_attempts": job.max_attempts,
        "last_error_class": job.last_error_class,
    }


def _print(value: object) -> None:
    # ASCII-safe JSON survives Windows consoles configured for legacy code pages.
    print(json.dumps(value, indent=2, ensure_ascii=True))


def main(argv: Sequence[str] | None = None) -> int:
    args = _parser().parse_args(argv)
    try:
        settings = load_settings(args.workspace, args.environment)
        db = Database(settings.database, busy_timeout_ms=settings.busy_timeout_ms)
        if args.command == "init":
            applied = db.initialize()
            _print({"database": str(db.path), "applied_migrations": applied,
                    "integrity": db.integrity_check()})
            return 0
        if not db.path.is_file():
            if args.command == "status":
                _print({"initialized": False, "database": str(db.path),
                        "pause_file_present": settings.pause_file.is_file()})
                return 0
            raise LmosError("database is not initialized; run 'lmos init' first")
        pause = PauseControl(db, settings.pause_file)
        queue = JobQueue(db, pause, retry_base_seconds=settings.retry_base_seconds,
                         retry_max_seconds=settings.retry_max_seconds,
                         external_actions_enabled=settings.external_actions_enabled)
        outbox = Outbox(db, pause)

        if args.command == "status":
            state = pause.status(args.lane)
            _print({"initialized": True, "environment": settings.environment,
                    "dry_run": settings.dry_run,
                    "external_actions_enabled": settings.external_actions_enabled,
                    "database": str(db.path), "paused": state.paused,
                    "pause_reasons": state.reasons, "active_pause_scopes": pause.active_scopes(),
                    "jobs": queue.counts(),
                    "outbox": outbox.counts()})
        elif args.command == "check":
            result = db.integrity_check()
            _print(result)
            return 0 if result["ok"] else 1
        elif args.command == "backup":
            _print({"backup": str(db.backup(args.destination))})
        elif args.command in {"pause", "resume"}:
            state = (pause.pause if args.command == "pause" else pause.resume)(
                actor=args.actor, reason=args.reason, lane=args.lane)
            _print({"paused": state.paused, "reasons": state.reasons,
                    "file_present": state.file_present})
        elif args.command == "enqueue":
            payload = json.loads(args.payload)
            if not isinstance(payload, dict):
                raise ValueError("--payload must be a JSON object")
            due = datetime.fromisoformat(args.due_at) if args.due_at else None
            job = queue.enqueue(workflow=args.workflow, lane=args.lane, payload=payload,
                                logical_key=args.key, due_at=due, priority=args.priority,
                                max_attempts=args.max_attempts or settings.default_max_attempts,
                                effect=args.effect)
            _print(_job_view(job))
        elif args.command == "jobs":
            _print([_job_view(job) for job in queue.list_jobs(state=args.state, limit=args.limit)])
        elif args.command == "claim":
            job = queue.claim(worker_id=args.worker,
                              lease_seconds=args.lease_seconds or settings.default_lease_seconds,
                              lane=args.lane)
            _print(_job_view(job) if job else None)
        elif args.command == "heartbeat":
            _print(_job_view(queue.heartbeat(job_id=args.job_id, worker_id=args.worker,
                                             lease_seconds=args.lease_seconds or settings.default_lease_seconds)))
        elif args.command == "complete":
            _print(_job_view(queue.complete(job_id=args.job_id, worker_id=args.worker)))
        elif args.command == "fail":
            _print(_job_view(queue.fail(job_id=args.job_id, worker_id=args.worker,
                                        error_class=args.error_class, retryable=args.retryable,
                                        unknown_effect=args.unknown_effect)))
        elif args.command == "replay":
            _print(_job_view(queue.replay(job_id=args.job_id, actor=args.actor,
                                          reason=args.reason,
                                          external_effect_ruled_out=args.external_effect_ruled_out)))
        elif args.command == "recover":
            _print({"jobs": queue.recover_expired(), "outbox": outbox.recover_expired()})
        elif args.command == "research":
            runtime = ResearchRuntime(settings=settings, database=db, pause=pause, queue=queue)
            if args.research_command == "tick":
                _print({"enqueued": [_job_view(job) for job in runtime.tick()]})
            elif args.research_command == "run":
                enqueued = runtime.tick()
                completed = []
                while (result := runtime.work_once()) is not None:
                    completed.append(result)
                _print({"enqueued": len(enqueued), "completed": completed})
            elif args.research_command == "work":
                _print(runtime.work_once())
            elif args.research_command == "daemon":
                runtime.run_loop()
            elif args.research_command == "report":
                if not 1 <= args.limit <= 1000:
                    raise ValueError("--limit must be 1..1000")
                _print(runtime.report(limit=args.limit))
            elif args.research_command == "finding":
                _print(runtime.finding(args.finding_id))
            elif args.research_command == "import-reddit-observation":
                _print(runtime.import_reddit_observation(
                    task_id=args.task_id, observation_id=args.observation_id,
                    worker_id=args.worker,
                    analysis=json.loads(args.analysis_file.read_text(encoding="utf-8"))))
            elif args.research_command == "review":
                runtime.review(finding_id=args.finding_id, reviewer=args.reviewer,
                               decision=args.decision)
                _print({"finding_id": args.finding_id, "decision": args.decision})
            elif args.research_command == "review-dispatch":
                _print({"assignments": runtime.dispatch_reviews(agent_id=args.agent,
                                                                  limit=args.limit)})
            elif args.research_command == "review-claim":
                _print(runtime.claim_review(agent_id=args.agent_id, worker_id=args.worker,
                                            lease_seconds=args.lease_seconds))
            elif args.research_command == "review-complete":
                _print(runtime.complete_review(job_id=args.job_id, worker_id=args.worker,
                    reviewer=args.reviewer, agent_run_ref=args.agent_run_ref,
                    decision=args.decision,
                    assessment=json.loads(args.assessment_file.read_text(encoding="utf-8"))))
            elif args.research_command == "review-fail":
                _print(runtime.fail_review(job_id=args.job_id, worker_id=args.worker,
                                           reason=args.reason, retryable=not args.permanent))
            elif args.research_command == "review-report":
                _print(runtime.review_report(limit=args.limit))
            elif args.research_command == "screen-legacy":
                _print({"screened": runtime.screen_legacy_directory(actor=args.actor, limit=args.limit)})
            elif args.research_command == "screen-report":
                _print(runtime.screening_report(limit=args.limit))
            elif args.research_command == "screen-restore":
                runtime.restore_screened_finding(finding_id=args.finding_id, actor=args.actor,
                                                 reason=args.reason)
                _print({"finding_id": args.finding_id, "state": "restored"})
            elif args.research_command in {"handoff-run", "handoff-report", "handoff-reconcile", "handoff-dismiss", "handoff-retry"}:
                handoffs = ResearchHandoffRuntime(db, pause,
                    settings.workspace / "DEPARTMENT_6_AGENT_GUIDES")
                if args.research_command == "handoff-run":
                    _print({"results": handoffs.run(limit=args.limit)})
                elif args.research_command == "handoff-report":
                    _print(handoffs.report())
                elif args.research_command == "handoff-reconcile":
                    _print({"results": handoffs.reconcile_undated_community_tasks()})
                elif args.research_command == "handoff-retry":
                    _print(handoffs.retry_community_intake(intake_id=args.intake_id,
                        actor=args.actor))
                else:
                    handoffs.dismiss_intake(intake_id=args.intake_id,
                        reviewer=args.reviewer, reason=args.reason)
                    _print({"intake_id": args.intake_id, "state": "dismissed"})
            elif args.research_command == "promote-organization":
                organization_id = runtime.promote_organization(
                    finding_id=args.finding_id, name=args.name,
                    domain=args.domain, reviewer=args.reviewer)
                _print({"finding_id": args.finding_id, "organization_id": organization_id})
            elif args.research_command == "credentials":
                values = load_local_env(settings.workspace)
                _print({"configured": [name for name in (
                    "BRAVE_SEARCH_API_KEY_1", "BRAVE_SEARCH_API_KEY_2",
                    "BRAVE_SEARCH_API_KEY_3")
                    if values.get(name)]})
        elif args.command == "qualification":
            runtime = QualificationRuntime(settings=settings, database=db, pause=pause,
                                           queue=queue, outbox=outbox)
            if args.qualification_command == "consume":
                _print(runtime.consume_discovery_once())
            elif args.qualification_command == "tick":
                _print({"enqueued": [_job_view(job) for job in runtime.tick()]})
            elif args.qualification_command == "run":
                consumed = []
                while (item := runtime.consume_discovery_once()) is not None:
                    consumed.append(item)
                while runtime.consume_internal_once() is not None:
                    pass
                enqueued = runtime.tick()
                completed = []
                while (result := runtime.work_once()) is not None:
                    completed.append(result)
                while runtime.consume_internal_once() is not None:
                    pass
                _print({"intakes": consumed, "enqueued": len(enqueued), "completed": completed})
            elif args.qualification_command == "daemon":
                runtime.run_loop()
            elif args.qualification_command == "work":
                _print(runtime.work_once())
            elif args.qualification_command == "report":
                _print(runtime.report(limit=args.limit))
            elif args.qualification_command == "prospect":
                _print(runtime.prospect(args.prospect_id))
            elif args.qualification_command == "approve-rubric":
                _print({"rubric_version": runtime.approve_rubric(reviewer=args.reviewer)})
            elif args.qualification_command == "approve-use-case":
                _print({"use_case_id": runtime.approve_use_case(
                    finding_id=args.finding_id, vertical=args.vertical, problem=args.problem,
                    buyer=args.buyer, reviewer=args.reviewer)})
            elif args.qualification_command == "admit":
                _print({"prospect_id": runtime.admit(
                    intake_id=args.intake_id, segment=args.segment, reviewer=args.reviewer,
                    country_code=args.country_code, location_hint=args.location_hint,
                    use_case_id=args.use_case_id, pain_evidence_url=args.pain_evidence_url,
                    pain_evidence_note=args.pain_evidence_note)})
            elif args.qualification_command == "reject-intake":
                runtime.reject_intake(intake_id=args.intake_id, reviewer=args.reviewer,
                                      reason=args.reason)
                _print({"intake_id": args.intake_id, "status": "rejected"})
            elif args.qualification_command == "link-use-case":
                runtime.link_use_case(prospect_id=args.prospect_id, use_case_id=args.use_case_id,
                                      reviewer=args.reviewer, pain_evidence_url=args.pain_evidence_url,
                                      pain_evidence_note=args.pain_evidence_note)
                _print({"prospect_id": args.prospect_id, "use_case_id": args.use_case_id})
            elif args.qualification_command == "rescore":
                _print(runtime.rescore(prospect_id=args.prospect_id, reviewer=args.reviewer))
            elif args.qualification_command == "retry-lookup":
                runtime.retry_lookup(prospect_id=args.prospect_id, reviewer=args.reviewer,
                                     reason=args.reason,
                                     provider_effect_reconciled=args.provider_effect_reconciled)
                _print({"prospect_id": args.prospect_id, "status": "new_lookup_approved"})
            elif args.qualification_command == "add-contact":
                _print({"contact_id": runtime.add_contact(
                    prospect_id=args.prospect_id, name=args.name, role=args.role,
                    email=args.email, source_url=args.source_url, reviewer=args.reviewer)})
            elif args.qualification_command == "verify-contact":
                runtime.verify_contact(contact_id=args.contact_id, prospect_id=args.prospect_id,
                                       source_url=args.source_url, reviewer=args.reviewer)
                _print({"contact_id": args.contact_id, "status": "verified"})
        elif args.command == "content":
            runtime = ContentRuntime(database=db, pause=pause, queue=queue, outbox=outbox)
            command = args.content_command
            if command == "consume":
                _print(runtime.consume_contact_once())
            elif command == "tick":
                _print({"enqueued": [_job_view(job) for job in runtime.tick()]})
            elif command == "work":
                _print(runtime.work_once())
            elif command == "run":
                intakes = []
                while (item := runtime.consume_contact_once()) is not None:
                    intakes.append(item)
                while runtime.consume_critic_once() is not None:
                    pass
                enqueued = runtime.tick()
                completed = []
                while (item := runtime.work_once()) is not None:
                    completed.append(item)
                while runtime.consume_critic_once() is not None:
                    pass
                enqueued += runtime.tick()
                while (item := runtime.work_once()) is not None:
                    completed.append(item)
                _print({"intakes": intakes, "enqueued": len(enqueued), "completed": completed})
            elif command == "daemon":
                runtime.run_loop()
            elif command == "report":
                _print(runtime.report(limit=args.limit))
            elif command == "draft":
                _print(runtime.draft(args.draft_id))
            elif command == "brief":
                _print(runtime.brief(args.request_id))
            elif command == "register-claim":
                _print({"claim_id": runtime.register_claim(
                    claim_key=args.key, claim_type=args.type, claim_text=args.text,
                    evidence_ref=args.evidence_ref, actor=args.actor,
                    permission_confirmed=args.permission_confirmed)})
            elif command == "approve-claim":
                runtime.approve_claim(claim_id=args.claim_id, reviewer=args.reviewer)
                _print({"claim_id": args.claim_id, "status": "approved"})
            elif command == "register-template":
                _print({"template_id": runtime.register_template(
                    template_key=args.key, kind=args.kind, channel=args.channel,
                    subject_template=args.subject, body_template=args.body_file.read_text(encoding="utf-8"),
                    actor=args.actor)})
            elif command == "approve-template":
                runtime.approve_template(template_id=args.template_id, reviewer=args.reviewer)
                _print({"template_id": args.template_id, "status": "approved"})
            elif command == "request":
                _print({"request_id": runtime.request(
                    kind=args.kind, template_id=args.template_id, claim_ids=args.claim_id,
                    actor=args.actor, intake_id=args.intake_id, logical_key=args.logical_key)})
            elif command == "revise":
                _print({"draft_id": runtime.revise(
                    draft_id=args.draft_id, subject=args.subject,
                    body=args.body_file.read_text(encoding="utf-8"), actor=args.actor)})
            elif command == "audit-revision":
                _print({"claim_audit_id": runtime.audit_revision(
                    draft_id=args.draft_id, reviewer=args.reviewer, rationale=args.rationale)})
            elif command == "create-experiment":
                _print({"experiment_id": runtime.create_experiment(
                    campaign_id=args.campaign_id, metric_key=args.metric,
                    window_start_at_utc=args.starts_at, window_end_at_utc=args.ends_at,
                    minimum_exposures=args.minimum_exposures,
                    minimum_conversions=args.minimum_conversions, actor=args.actor)})
            elif command == "record-exposure":
                _print({"exposure_id": runtime.record_exposure(
                    experiment_id=args.experiment_id, variant_key=args.variant,
                    subject_key=args.subject_key, actor=args.actor)})
            elif command == "record-outcome":
                _print({"outcome_id": runtime.record_outcome(
                    exposure_id=args.exposure_id, outcome_key=args.outcome_key, actor=args.actor)})
            elif command == "analyze":
                _print(runtime.analyze_experiment(experiment_id=args.experiment_id))
        elif args.command == "discovery":
            runtime = DiscoveryRuntime(db, pause)
            command = args.discovery_command
            if command == "report":
                _print(runtime.report(limit=args.limit))
            elif command == "cycle":
                _print(runtime.cycle(cycle_key=args.key))
            elif command == "finish-task":
                _print(runtime.finish_task(task_id=args.task_id, outcome=args.outcome,
                                           reason=args.reason, actor=args.actor))
            elif command == "scout":
                _print(runtime.scout(vertical=args.vertical, problem=args.problem,
                                     buyer=args.buyer, source_url=args.source_url, excerpt=args.excerpt))
            elif command == "evidence":
                _print(runtime.evidence(opportunity_id=args.opportunity_id, polarity=args.polarity,
                                        source_url=args.source_url, excerpt=args.excerpt))
            elif command == "validate":
                _print(runtime.validate(opportunity_id=args.opportunity_id, buyer_workflow=args.buyer_workflow,
                                        product_fit=args.product_fit, limitations=args.limitations,
                                        reviewer=args.reviewer))
            elif command == "candidate":
                _print(runtime.candidate(opportunity_id=args.opportunity_id, company=args.company,
                                         domain=args.domain, country=args.country, source_url=args.source_url,
                                         pain_url=args.pain_url, pain_note=args.pain_note))
            elif command == "review-candidate":
                _print(runtime.review_candidate(candidate_id=args.candidate_id,
                                                accept=args.decision == "accept", reviewer=args.reviewer))
            elif command == "dispatch":
                _print(runtime.dispatch(opportunity_id=args.opportunity_id, approver=args.approver))
        elif args.command == "operations":
            schedules = ScheduleManager(db, pause, queue)
            ledger = OperationsLedger(db)
            command = args.operations_command
            if command == "configure-schedule":
                version = schedules.configure(
                    schedule_id=args.schedule_id, workflow=args.workflow,
                    interval_seconds=args.interval_seconds,
                    next_due_at=datetime.fromisoformat(args.next_due_at),
                    timezone_name=args.timezone, catch_up=args.catch_up,
                    owner_role=args.owner, max_overlap=args.max_overlap,
                    window_start=args.window_start, window_end=args.window_end,
                    weekdays=tuple(int(day) for day in args.weekdays.split(",")),
                    expected_version=args.expected_version)
                _print({"schedule_id": args.schedule_id, "config_version": version, "active": False})
            elif command == "set-schedule-active":
                schedules.set_active(args.schedule_id, active=args.active, actor=args.actor)
                _print({"schedule_id": args.schedule_id, "active": args.active})
            elif command == "tick":
                _print(schedules.tick())
            elif command in {"work", "run", "daemon"}:
                handlers = {
                    "research.tick": lambda: len(ResearchRuntime(settings=settings, database=db,
                        pause=pause, queue=queue).tick()),
                    "qualification.tick": lambda: len(QualificationRuntime(settings=settings,
                        database=db, pause=pause, queue=queue, outbox=outbox).tick()),
                    "content.tick": lambda: len(ContentRuntime(database=db, pause=pause,
                        queue=queue, outbox=outbox).tick()),
                    "community.tick": lambda: len(CommunityRuntime(
                        db, pause, settings.workspace / "DEPARTMENT_6_AGENT_GUIDES").tick()),
                    "audit.tick": lambda: AuditRuntime(db, pause).full_scan(actor="scheduled-7"),
                    "discovery.tick": lambda: DiscoveryRuntime(db, pause).cycle(
                        cycle_key=(lambda local: f"{local.date()}-{local.hour // 8}")(
                            datetime.now(ZoneInfo("Africa/Nairobi")))),
                    "support.tick": lambda: SupportRuntime(db, pause).tick(),
                    "operations.backup": lambda: ledger.scheduled_backup(
                        settings.workspace / "runtime" / "backups"),
                    "operations.restore_drill": ledger.restore_latest,
                    "operations.integrity_check": db.integrity_check,
                }
                if command == "work":
                    _print(schedules.work_once(handlers))
                elif command == "run":
                    decisions = schedules.tick()
                    results = []
                    while (item := schedules.work_once(handlers)) is not None:
                        results.append(item)
                    _print({"decisions": decisions, "worked": results})
                else:
                    schedules.run_loop(handlers, poll_seconds=args.poll_seconds)
            elif command == "report":
                _print({"health": schedules.health(), "scheduler": schedules.report(), "ledger": ledger.report(),
                        "database": db.integrity_check(), "jobs": queue.counts()})
            elif command == "health":
                result = schedules.health()
                _print(result)
                return 0 if result["ok"] else 1
            elif command == "approve-price":
                _print({"price_id": ledger.approve_price(
                    provider=args.provider, model=args.model, currency=args.currency,
                    prompt_per_million_minor=args.prompt_per_million_minor,
                    completion_per_million_minor=args.completion_per_million_minor,
                    request_minor=args.request_minor,
                    effective_at=datetime.fromisoformat(args.effective_at), actor=args.actor)})
            elif command == "record-usage":
                _print(ledger.record_usage(usage_key=args.usage_key, provider=args.provider,
                    model=args.model, prompt_tokens=args.prompt_tokens,
                    completion_tokens=args.completion_tokens, requests=args.requests,
                    occurred_at=datetime.fromisoformat(args.occurred_at), job_id=args.job_id))
            elif command == "reconcile-usage":
                _print(ledger.reconcile(usage_key=args.usage_key, actual_minor=args.actual_minor,
                                        currency=args.currency, actor=args.actor))
            elif command == "ledger-report":
                _print(ledger.report())
            elif command == "verified-backup":
                _print(ledger.backup(args.destination))
            elif command == "restore-drill":
                _print(ledger.restore_drill(backup_id=args.backup_id))
        elif args.command == "community":
            runtime = CommunityRuntime(db, pause, settings.workspace / "DEPARTMENT_6_AGENT_GUIDES")
            indexer = ProspectListIndexer(db, pause=pause)
            command = args.community_command
            if command in {"hn-scan", "hn-item"}:
                hn = HackerNewsClient(HttpClient(user_agent="LumenMarketingOS/0.1 public HN research"))
                if command == "hn-scan":
                    _print({"group": args.group, "items": [asdict(item) for item in
                            hn.stories(args.group, limit=args.limit)]})
                else:
                    _print(hn.item_context(args.item_id, max_comments=args.max_comments))
            elif command == "configure-trigger":
                payload = json.loads(args.input_file.read_text(encoding="utf-8"))
                _print({"trigger_id": args.trigger_id, "config_version": runtime.configure_trigger(
                    trigger_id=args.trigger_id, agent_id=args.agent_id, trigger_kind=args.kind,
                    owner=args.owner, input_payload=payload,
                    interval_seconds=args.interval_seconds,
                    next_due_at=datetime.fromisoformat(args.next_due_at) if args.next_due_at else None,
                    expected_version=args.expected_version), "active": False})
            elif command == "set-trigger-active":
                runtime.set_trigger_active(args.trigger_id, active=args.active, actor=args.actor)
                _print({"trigger_id": args.trigger_id, "active": args.active})
            elif command == "trigger":
                payload = json.loads(args.input_file.read_text(encoding="utf-8"))
                _print({"task_id": runtime.trigger(agent_id=args.agent_id, trigger_key=args.key,
                    input_payload=payload, trigger_id=args.trigger_id)})
            elif command == "tick":
                _print(runtime.tick())
            elif command == "claim":
                _print(runtime.claim(agent_id=args.agent_id, worker_id=args.worker,
                                     lease_seconds=args.lease_seconds))
            elif command == "brief":
                _print(runtime.brief(args.task_id))
            elif command == "observe":
                _print({"observation_id": runtime.observe(task_id=args.task_id,
                    worker_id=args.worker, platform=args.platform, canonical_url=args.url,
                    title=args.title, excerpt=args.excerpt,
                    observed_at=datetime.fromisoformat(args.observed_at),
                    relevance_note=args.relevance, external_id=args.external_id)})
            elif command == "draft":
                _print({"draft_id": runtime.draft(task_id=args.task_id, worker_id=args.worker,
                    artifact_type=args.type, body=args.body_file.read_text(encoding="utf-8"),
                    evidence_urls=args.evidence_url, observation_id=args.observation_id)})
            elif command == "draft-detail":
                _print(runtime.draft_detail(args.draft_id))
            elif command == "finish":
                _print({"task_id": args.task_id, "state": runtime.finish(task_id=args.task_id,
                    worker_id=args.worker, outcome=args.outcome, reason=args.reason)})
            elif command == "review-draft":
                runtime.review_draft(draft_id=args.draft_id, reviewer=args.reviewer,
                    decision=args.decision, note=args.note, expected_hash=args.expected_hash)
                _print({"draft_id": args.draft_id, "state": args.decision})
            elif command == "review-launch-item":
                runtime.review_launch_item(task_id=args.task_id, item_key=args.item_key,
                    state=args.state, actor=args.actor, evidence_ref=args.evidence_ref)
                _print({"task_id": args.task_id, "item_key": args.item_key, "state": args.state})
            elif command == "connection-status":
                runtime.connection(agent_id=args.agent_id, platform=args.platform,
                    state=args.state, actor=args.actor, account_reference=args.account_reference,
                    failure_class=args.failure_class, verified=args.verified)
                _print({"agent_id": args.agent_id, "platform": args.platform, "state": args.state})
            elif command == "import-prospects":
                rows = json.loads(args.file.read_text(encoding="utf-8"))
                _print(indexer.import_rows(source_name=args.source_name, source_url=args.source_url,
                    source_version=args.source_version, approved_by=args.approved_by, rows=rows,
                    task_id=args.task_id, worker_id=args.worker))
            elif command == "import-report":
                _print(indexer.report(batch_id=args.batch_id))
            elif command == "report":
                _print(runtime.report(limit=args.limit))
        elif args.command == "audit":
            env = load_local_env(settings.workspace)
            secret = os.environ.get("LMOS_SUPPRESSION_HMAC_KEY_HEX") or env.get(
                "LMOS_SUPPRESSION_HMAC_KEY_HEX", "")
            try:
                key = bytes.fromhex(secret) if secret else None
            except ValueError as exc:
                raise ValueError("LMOS_SUPPRESSION_HMAC_KEY_HEX must be hex-encoded") from exc
            runtime = AuditRuntime(db, pause, suppression_key=key)
            command = args.audit_command
            if command == "init-policy":
                _print({"policy_id": runtime.ensure_default_policy(actor=args.actor)})
            elif command == "add-policy":
                rules = json.loads(args.rules_file.read_text(encoding="utf-8"))
                _print({"policy_id": runtime.create_policy(policy_key=args.key, rules=rules,
                        actor=args.actor, activate=args.activate)})
            elif command == "activate-policy":
                runtime.activate_policy(policy_id=args.policy_id, actor=args.actor)
                _print({"policy_id": args.policy_id, "state": "active"})
            elif command == "guard":
                _print(runtime.guard_draft(draft_id=args.draft_id, reviewer=args.reviewer,
                                           checker_available=not args.checker_unavailable))
            elif command == "record-health":
                metrics = json.loads(args.metrics)
                if not isinstance(metrics, dict):
                    raise ValueError("metrics must be a JSON object")
                _print({"snapshot_id": runtime.record_provider_health(channel=args.channel,
                       account_id=args.account_id, provider=args.provider, status=args.status,
                       reason=args.reason, actor=args.actor, metrics=metrics,
                       expires_at_utc=args.expires_at)})
            elif command == "rate-report":
                _print(runtime.rate_health_report())
            elif command == "integrity-scan":
                _print(runtime.integrity_scan(actor=args.actor))
            elif command == "growth-audit":
                _print(runtime.growth_audit(window_start_utc=args.starts_at,
                       window_end_utc=args.ends_at, minimum_sample=args.minimum_sample,
                       actor=args.actor))
            elif command == "pipeline-scan":
                _print(runtime.pipeline_scan(actor=args.actor,
                       pending_lag_seconds=args.pending_lag_seconds,
                       heartbeat_lag_seconds=args.heartbeat_lag_seconds))
            elif command == "suppression-check":
                _print(runtime.suppression_check(subject_type=args.type, value=args.value,
                                                  actor=args.actor))
            elif command == "retention-review":
                _print(runtime.retention_review(actor=args.actor))
            elif command == "full-scan":
                _print(runtime.full_scan(actor=args.actor))
            elif command == "resolve-finding":
                runtime.resolve_finding(finding_id=args.finding_id, actor=args.actor,
                                        resolution=args.resolution)
                _print({"finding_id": args.finding_id, "state": "resolved"})
            elif command == "report":
                _print(runtime.report(limit=args.limit))
        elif args.command == "support":
            runtime = SupportRuntime(db, pause)
            command = args.support_command
            if command == "register-source":
                _print({"source_id": runtime.register_source(key=args.key, kind=args.kind,
                        external_reference=args.reference, actor=args.actor, account_id=args.account_id)})
            elif command == "tick":
                _print(runtime.tick())
            elif command == "enable-source":
                runtime.enable_source(source_id=args.source_id, actor=args.actor, verified=args.verified)
                _print({"source_id": args.source_id, "enabled": True})
            elif command == "ingest-file":
                _print(runtime.ingest_batch(source_id=args.source_id, actor=args.actor,
                        items=json.loads(args.file.read_text(encoding="utf-8")), next_cursor=args.next_cursor))
            elif command == "triage":
                _print(runtime.triage(ticket_id=args.ticket_id, actor=args.actor))
            elif command == "review":
                _print(runtime.review(ticket_id=args.ticket_id, category=args.category,
                        priority=args.priority, state=args.state, actor=args.actor,
                        rationale=args.rationale))
            elif command == "draft-response":
                _print({"draft_id": runtime.draft_response(ticket_id=args.ticket_id,
                        subject=args.subject, body=args.body_file.read_text(encoding="utf-8"),
                        author=args.author)})
            elif command == "approve-response":
                runtime.approve_response(draft_id=args.draft_id, reviewer=args.reviewer,
                                         expected_hash=args.hash)
                _print({"draft_id": args.draft_id, "state": "approved"})
            elif command == "handoff-email":
                env = load_local_env(settings.workspace)
                secret = os.environ.get("LMOS_SUPPRESSION_HMAC_KEY_HEX") or env.get(
                    "LMOS_SUPPRESSION_HMAC_KEY_HEX", "")
                try:
                    key = bytes.fromhex(secret)
                except ValueError as exc:
                    raise ValueError("LMOS_SUPPRESSION_HMAC_KEY_HEX must be hex-encoded") from exc
                outreach = OutreachRuntime(database=db, pause=pause, suppression_key=key,
                                          external_actions_enabled=settings.external_actions_enabled)
                _print(runtime.handoff_email(draft_id=args.draft_id, outreach=outreach,
                                             actor=args.actor))
            elif command == "sheet-projection":
                _print(runtime.sheet_projection(ticket_id=args.ticket_id,
                       spreadsheet_id=args.spreadsheet_id, actor=args.actor))
            elif command == "sheet-ack":
                runtime.acknowledge_sheet(ticket_id=args.ticket_id, spreadsheet_id=args.spreadsheet_id,
                                          version=args.version, digest=args.hash, actor=args.actor)
                _print({"ticket_id": args.ticket_id, "state": "synced"})
            elif command == "report":
                _print(runtime.report(limit=args.limit))
            elif command == "ticket":
                _print(runtime.ticket(args.ticket_id))
        elif args.command == "outreach":
            env = load_local_env(settings.workspace)
            secret = os.environ.get("LMOS_SUPPRESSION_HMAC_KEY_HEX") or env.get("LMOS_SUPPRESSION_HMAC_KEY_HEX", "")
            try:
                key = bytes.fromhex(secret)
            except ValueError as exc:
                raise ValueError("LMOS_SUPPRESSION_HMAC_KEY_HEX must be hex-encoded") from exc
            runtime = OutreachRuntime(database=db, pause=pause, suppression_key=key,
                                      external_actions_enabled=settings.external_actions_enabled)
            command = args.outreach_command
            if command == "register-account":
                _print({"account_id": runtime.register_account(email=args.email,
                       policy_version=args.policy_version, calendar_id=args.calendar_id,
                       actor=args.actor)})
            elif command == "enable-account":
                runtime.enable_account(account_id=args.account_id, actor=args.actor,
                                       confirmed=args.confirmed)
                _print({"account_id": args.account_id, "enabled": True})
            elif command == "disable-account":
                runtime.disable_account(account_id=args.account_id, actor=args.actor,
                                        reason=args.reason)
                _print({"account_id": args.account_id, "enabled": False})
            elif command == "set-quota":
                _print({"quota_id": runtime.set_quota(account_id=args.account_id,
                       max_units=args.max_units, period_seconds=args.period_seconds,
                       policy_version=args.policy_version, actor=args.actor)})
            elif command == "set-send-window":
                runtime.set_send_window(account_id=args.account_id, timezone_name=args.timezone,
                                        start_local=args.start, end_local=args.end,
                                        weekdays=[int(x) for x in args.weekdays.split(",")],
                                        actor=args.actor)
                _print({"account_id": args.account_id, "window_configured": True})
            elif command == "suppress":
                _print({"suppression_id": runtime.suppress(subject_type=args.type,
                       value=args.value, reason=args.reason, actor=args.actor)})
            elif command == "ingest-file":
                payload = json.loads(args.file.read_text(encoding="utf-8"))
                if not isinstance(payload, list):
                    raise ValueError("inbox fixture must be a JSON array")
                _print(runtime.ingest_page(account_id=args.account_id,
                       messages=[IncomingMail(**item) for item in payload], next_cursor=args.cursor))
            elif command == "review-classification":
                runtime.review_classification(event_id=args.event_id, label=args.label,
                                              actor=args.actor, rationale=args.rationale)
                _print({"event_id": args.event_id, "label": args.label})
            elif command == "propose-response":
                slots = json.loads(args.slots_file.read_text(encoding="utf-8")) if args.slots_file else []
                _print({"proposal_id": runtime.propose_response(event_id=args.event_id,
                       account_id=args.account_id, subject=args.subject,
                       body=args.body_file.read_text(encoding="utf-8"),
                       slots=slots, author=args.author)})
            elif command == "review-response":
                runtime.review_response(proposal_id=args.proposal_id, reviewer=args.reviewer)
                _print({"proposal_id": args.proposal_id, "state": "reviewed"})
            elif command == "prepare-send":
                _print({"action_id": runtime.prepare_send(draft_id=args.draft_id,
                       account_id=args.account_id, recipient=args.recipient,
                       actor=args.actor, due_at_utc=args.due_at,
                       source_message_id=args.source_message_id,
                       recipient_timezone=args.recipient_timezone,
                       timezone_source=args.timezone_source)})
            elif command == "prepare-booking":
                _print({"action_id": runtime.prepare_booking(account_id=args.account_id,
                       attendee=args.attendee, title=args.title,
                       start_at_utc=args.starts_at, end_at_utc=args.ends_at,
                       source_event_id=args.source_event_id, actor=args.actor)})
            elif command == "approve":
                _print({"approval_id": runtime.approve_action(action_id=args.action_id,
                       actor=args.actor, expires_at_utc=args.expires_at, reason=args.reason)})
            elif command == "revoke":
                runtime.revoke_action(action_id=args.action_id, actor=args.actor,
                                      reason=args.reason)
                _print({"action_id": args.action_id, "state": "cancelled"})
            elif command == "reconcile":
                _print(runtime.reconcile_unknown(action_id=args.action_id, actor=args.actor))
            elif command == "dispatch-once":
                _print(runtime.dispatch_once())
            elif command == "recover-dispatch":
                _print({"unknown_actions": runtime.recover_stale_dispatches(
                    older_than_seconds=args.older_than_seconds)})
            elif command == "report":
                _print(runtime.report())
            elif command == "action":
                _print(runtime.action_detail(args.action_id))
            elif command == "inbound":
                _print(runtime.inbound_detail(args.event_id))
        return 0
    except (LmosError, SourceError, ValueError, OSError, sqlite3.Error) as exc:
        print(f"error: {exc}", file=sys.stderr)
        return 2
