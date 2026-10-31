"""Deterministic, evidence-bound content assembly and first-line critique."""
from __future__ import annotations

import hashlib
import json
import re
from string import Formatter

ALLOWED_FIELDS = frozenset({"company", "contact_first_name", "role", "problem", "product_claim", "customer_claim"})
RULE_VERSION = "content-critic-v1"


def canonical(value: object) -> str:
    return json.dumps(value, sort_keys=True, separators=(",", ":"), ensure_ascii=False, allow_nan=False)


def digest(value: object) -> str:
    return hashlib.sha256(canonical(value).encode("utf-8")).hexdigest()


def content_hash(subject: str | None, body: str) -> str:
    return digest({"subject": subject, "body": body})


def validate_template(text: str | None) -> set[str]:
    if text is None:
        return set()
    if not isinstance(text, str) or not text.strip() or len(text) > 12000:
        raise ValueError("template must contain 1..12000 characters")
    fields = set()
    for _, field, format_spec, conversion in Formatter().parse(text):
        if field is None:
            continue
        if field not in ALLOWED_FIELDS or format_spec or conversion:
            raise ValueError("template contains an unsupported field or format directive")
        fields.add(field)
    return fields


def render_template(text: str | None, values: dict[str, str]) -> str | None:
    if text is None:
        return None
    fields = validate_template(text)
    if any(not values.get(field) for field in fields):
        raise ValueError("template requires missing approved evidence")
    return text.format_map(values)


def critique(*, subject: str | None, body: str, expected_subject: str | None,
             expected_body: str, evidence_ok: bool, trusted_template: bool,
             human_claim_audit: bool = False) -> list[dict]:
    findings: list[dict] = []
    def block(code: str, detail: str) -> None:
        findings.append({"code": code, "severity": "blocking", "detail": detail})
    if not evidence_ok:
        block("stale_or_missing_evidence", "A referenced claim, template, identity or contact is no longer approved/current.")
    if not body.strip() or len(body) > 12000 or (subject is not None and len(subject) > 300):
        block("invalid_length", "Body or subject is empty or outside the configured limits.")
    if not human_claim_audit and (not trusted_template or
                                  (subject, body) != (expected_subject, expected_body)):
        block("unreviewed_freeform_claims", "Content differs from approved evidence-bound template; human claim review is required.")
    if re.search(r"\b(ignore (all |previous )?instructions|reveal (the )?(secret|api key)|system prompt)\b", body, re.I):
        block("instruction_like_content", "Draft contains instruction-like or sensitive-content language.")
    if "{" in body or "}" in body:
        block("unrendered_placeholder", "Draft has a possible unrendered placeholder.")
    return findings
