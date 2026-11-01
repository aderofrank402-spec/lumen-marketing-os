# Control model

Lumen Marketing OS is designed around one principle: generating an artifact is not authorization to act on it.

## Final external-action sequence

Before a configured adapter may perform an external effect, the runtime must:

1. Read the global and lane-level pause state.
2. Load the exact action, draft, target, channel, and policy version.
3. Check suppression and recipient eligibility.
4. Verify the evidence and contact constraints required by that workflow.
5. Find an unexpired, named approval matching the exact content hash, target, and channel.
6. Reserve quota atomically.
7. Verify the selected adapter supports the requested capability and is healthy.
8. Claim a unique action key so retries cannot duplicate the same effect.
9. Record the attempt in the audit trail.
10. Recheck pause and suppression immediately before the network boundary.
11. Persist the outcome, or hold an uncertain outcome for human review.

Any missing or ambiguous prerequisite blocks the action.

## Controls and failure behavior

| Control | Failure behavior |
|---|---|
| Global or lane pause | New work is not dispatched; the more restrictive state wins |
| Exact-hash approval | Changed content invalidates the approval |
| Suppression lookup | Missing or unreadable suppression state fails closed |
| Quota reservation | No reservation means no dispatch |
| Provider health | Unhealthy or unsupported adapters are not used |
| Idempotency key | A previously claimed effect is not repeated |
| Unknown external outcome | Work enters review instead of automatic replay |
| Expired worker lease | Recovery classifies and safely requeues or reviews the job |

## Approval is narrow

An approval is not “approve this campaign.” It binds a named reviewer to a specific content hash, recipient or target, channel, expiration, and policy context. If any protected field changes, the action must be reviewed again.

## HMAC suppression identifiers

Suppression records use keyed identifiers so the control plane can compare identities without treating raw contact values as durable operational keys. If the key or suppression data is unavailable, the safe response is to stop.

## Human responsibility

The system prepares evidence, drafts, classifications, and action records. Human operators remain responsible for provider credentials, policy configuration, final approvals, production supervision, and compliance with the rules governing their recipients and channels.

The detailed normative specification is in [`PERMISSIONS_AND_APPROVALS.md`](../PERMISSIONS_AND_APPROVALS.md).
