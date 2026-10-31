"""Conservative, side-effect-free analysis of recorded campaign variants.

This module proposes a candidate for human review; it never changes variant
allocation, campaign policy, or an external channel.  The caller is responsible
for supplying deduplicated, attributed exposure and conversion counts.
"""

from __future__ import annotations

from math import isfinite, sqrt


def _wilson_interval(successes: int, trials: int, z: float) -> tuple[float, float]:
    if trials == 0:
        return (0.0, 1.0)
    rate = successes / trials
    squared = z * z
    denominator = 1.0 + squared / trials
    centre = (rate + squared / (2.0 * trials)) / denominator
    margin = (z / denominator) * sqrt(
        rate * (1.0 - rate) / trials + squared / (4.0 * trials * trials)
    )
    return (max(0.0, centre - margin), min(1.0, centre + margin))


def analyze_variants(
    rows: list[dict], *, minimum_exposures: int, minimum_conversions: int,
    confidence_z: float = 1.96,
) -> dict:
    """Compare conversion rates and recommend only clearly separated variants.

    Each input row contains a unique ``variant_key``, integer ``exposures``, and
    integer ``conversions``. A recommendation is only a review proposal, not an
    instruction to change campaign weights or declare a causal winner.
    """
    if not isinstance(rows, list):
        raise ValueError("rows must be a list")
    if type(minimum_exposures) is not int or minimum_exposures < 1:
        raise ValueError("minimum_exposures must be a positive integer")
    if type(minimum_conversions) is not int or minimum_conversions < 1:
        raise ValueError("minimum_conversions must be a positive integer")
    if (isinstance(confidence_z, bool) or not isinstance(confidence_z, (int, float))
            or not isfinite(confidence_z) or confidence_z <= 0
            or not isfinite(confidence_z * confidence_z)):
        raise ValueError("confidence_z must be a finite positive number")

    seen: set[str] = set()
    variants: list[dict] = []
    for row in rows:
        if not isinstance(row, dict):
            raise ValueError("each variant must be an object")
        key = row.get("variant_key")
        exposures = row.get("exposures")
        conversions = row.get("conversions")
        if not isinstance(key, str) or not key.strip() or key != key.strip():
            raise ValueError("variant_key must be a nonempty trimmed string")
        if key in seen:
            raise ValueError(f"duplicate variant_key: {key}")
        if (type(exposures) is not int or type(conversions) is not int
                or exposures < 0 or conversions < 0 or conversions > exposures):
            raise ValueError("counts must be nonnegative integers with conversions <= exposures")
        seen.add(key)
        lower, upper = _wilson_interval(conversions, exposures, float(confidence_z))
        variants.append({
            "variant_key": key,
            "exposures": exposures,
            "conversions": conversions,
            "conversion_rate": conversions / exposures if exposures else None,
            "wilson_lower": lower,
            "wilson_upper": upper,
        })

    # Stable order makes audit records and deterministic fixture tests easier to inspect.
    variants.sort(key=lambda item: item["variant_key"])
    reasons: list[str] = []
    if len(variants) < 2:
        reasons.append("fewer_than_two_variants")
    if any(item["exposures"] < minimum_exposures for item in variants):
        reasons.append("minimum_exposures_not_met")
    if sum(item["conversions"] for item in variants) < minimum_conversions:
        reasons.append("minimum_conversions_not_met")

    ranked = sorted(
        variants,
        key=lambda item: (-(item["conversion_rate"] or 0.0), item["variant_key"]),
    )
    if len(ranked) >= 2 and not reasons:
        best = ranked[0]
        # A runner-up comparison alone can miss an uncertain lower-rate arm
        # with a wider interval, so require separation from every other arm.
        if any(best["wilson_lower"] <= other["wilson_upper"] for other in ranked[1:]):
            reasons.append("wilson_intervals_overlap")

    return {
        "status": "recommendation" if not reasons else "insufficient_evidence",
        "recommended_variant": ranked[0]["variant_key"] if not reasons else None,
        "reasons": reasons,
        "variants": variants,
        "confidence_z": float(confidence_z),
        "method": "unadjusted_wilson_intervals",
        "requires_human_approval": True,
    }
