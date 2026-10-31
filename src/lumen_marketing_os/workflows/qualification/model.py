"""Conservative normalization and explainable, versioned qualification factors."""
from __future__ import annotations

import re
from urllib.parse import urlsplit


def _field(container: object, key: str) -> tuple[object, dict]:
    if not isinstance(container, dict):
        return None, {}
    item = container.get(key)
    if isinstance(item, dict) and "value" in item:
        return item.get("value"), item.get("metadata") if isinstance(item.get("metadata"), dict) else {}
    return item, item.get("metadata", {}) if isinstance(item, dict) else {}


def _host(value: object) -> str | None:
    if not isinstance(value, str) or not value:
        return None
    target = urlsplit(value if "://" in value else "https://" + value)
    return target.hostname.lower().removeprefix("www.") if target.hostname else None


def _confidence(metadata: dict) -> float | None:
    value = metadata.get("confidence")
    return float(value) if isinstance(value, (int, float)) and 0 <= value <= 1 else None


def _sources(metadata: dict) -> list[str]:
    values = metadata.get("sources")
    if not isinstance(values, list):
        return []
    return [value for value in values[:20] if isinstance(value, str) and
            value.startswith(("https://", "http://"))]


def _last_verified(metadata: dict) -> str | None:
    value = metadata.get("last_verified")
    return value if isinstance(value, str) and len(value) <= 64 else None


def normalize_profile(profile: object, *, requested_name: str,
                      requested_domain: str | None, requested_country: str | None) -> tuple[dict, dict, bool]:
    if not isinstance(profile, dict):
        raise ValueError("profile must be an object")
    business = profile.get("business") if isinstance(profile.get("business"), dict) else {}
    contact = profile.get("contact") if isinstance(profile.get("contact"), dict) else {}
    location = profile.get("location") if isinstance(profile.get("location"), dict) else {}
    name, name_meta = _field(business, "trade_name")
    if not isinstance(name, str) or not name.strip():
        name, name_meta = _field(business, "legal_name")
    website, site_meta = _field(contact, "website")
    hq = location.get("headquarters") if isinstance(location.get("headquarters"), dict) else {}
    country, country_meta = _field(hq, "country_code")
    if isinstance(country, str):
        country = country.upper().strip()
    else:
        country = None
    domain = _host(website)
    confidences = [value for value in (_confidence(name_meta), _confidence(site_meta))
                   if value is not None]
    confidence = min(confidences) if confidences else None
    evidence = {
        "name": {"sources": _sources(name_meta), "confidence": _confidence(name_meta),
                 "last_verified": _last_verified(name_meta)},
        "website": {"sources": _sources(site_meta), "confidence": _confidence(site_meta),
                    "last_verified": _last_verified(site_meta)},
        "country": {"sources": _sources(country_meta), "confidence": _confidence(country_meta),
                    "last_verified": _last_verified(country_meta)},
    }
    emails: list[dict] = []
    for item in (contact.get("emails") or [])[:30] if isinstance(contact.get("emails"), list) else []:
        if not isinstance(item, dict) or not isinstance(item.get("address"), str):
            continue
        address = item["address"].strip().lower()
        if not re.fullmatch(r"[^\s@]+@[^\s@]+\.[^\s@]+", address):
            continue
        meta = item.get("metadata") if isinstance(item.get("metadata"), dict) else {}
        emails.append({"address": address, "purpose": item.get("purpose") if isinstance(item.get("purpose"), str) else None,
                       "sources": _sources(meta), "confidence": _confidence(meta),
                       "last_verified": _last_verified(meta)})
    normalized = {"name": name.strip() if isinstance(name, str) else None,
                  "website": website if isinstance(website, str) else None,
                  "domain": domain, "country_code": country,
                  "profile_confidence": confidence, "emails": emails}
    name_matches = (not requested_name or not normalized["name"] or
                    re.sub(r"\W+", "", requested_name.lower()) ==
                    re.sub(r"\W+", "", normalized["name"].lower()))
    ambiguous = (not normalized["name"] or
                 bool(requested_domain and domain != requested_domain) or
                 bool(requested_country and country and country != requested_country) or
                 bool(not requested_domain and not name_matches))
    return normalized, evidence, ambiguous


def assess_fit(*, segment: str, approved_segments: list[str], rubric_approved: bool,
               use_case_approved: bool, identity_verified: bool, ambiguous: bool,
               normalized: dict, minimum_confidence: float, minimum_score: int) -> tuple[float, list[dict], str]:
    factors: list[dict] = []
    def factor(name: str, points: int, possible: int, reason: str) -> None:
        factors.append({"factor": name, "points": points, "possible": possible, "reason": reason})
    factor("identity", 25 if identity_verified and not ambiguous else 0, 25,
           "reviewed organization identity and matching profile" if identity_verified and not ambiguous
           else "identity or profile match needs review")
    factor("segment", 20 if segment in approved_segments else 0, 20,
           "operator-approved segment" if segment in approved_segments else "segment not approved")
    factor("problem", 35 if use_case_approved else 0, 35,
           "operator-approved evidence-backed use case" if use_case_approved
           else "no approved use case linked to this prospect")
    confidence = normalized.get("profile_confidence")
    strong = isinstance(confidence, (int, float)) and confidence >= minimum_confidence
    factor("profile_evidence", 20 if strong else 0, 20,
           "profile field confidence meets threshold" if strong else "profile confidence missing or low")
    score = float(sum(item["points"] for item in factors))
    decision = "qualified" if (rubric_approved and identity_verified and not ambiguous and
                               segment in approved_segments and use_case_approved and strong and
                               score >= minimum_score) else "review"
    return score, factors, decision
