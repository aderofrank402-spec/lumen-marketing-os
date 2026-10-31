# Configuration and secrets

## Configuration ownership

Configuration answers what may run, with which approved limits and accounts, and where local state lives. Keep four layers distinct: versioned non-secret defaults (`config/base.yaml`), environment profile (`development`, `staging`, or `live`), operator-approved strategy/objectives (`STRATEGY.md`, `DAILY_OBJECTIVES.md`, `CONTENT_PLAYBOOK.md` plus versioned policy records), and secret values supplied through protected local storage or process environment. The runtime validates the merged configuration before starting any worker. A malformed or missing required value stops the affected lane with a clear diagnostic.

The workspace root is discovered from the installed project/configuration, not from a path copied from a planning PDF. This workspace is `C:\Users\USER\Desktop\Lumen-Marketing-OS`; generated runtime data can live under its ignored `runtime/` directory or another explicitly configured local path. No OS component is configured to run on Vultr.

## Non-secret settings

| Setting group | Example fields | Rule |
| --- | --- | --- |
| Environment | `environment`, `workspace_root`, `database_path`, `log_level`, `dry_run`, `external_actions_enabled`. | Development/staging default to dry run and disabled external actions; live flag alone is insufficient without policy approval. |
| Scheduling | Per-role schedule, source timezone, catch-up window, lease duration, worker concurrency, poll interval. | Validate time zone and positive ranges; three-minute Lumen lookup requires an adequate lease/deadline margin. |
| Sources | Allowed feed/source IDs, query terms, provider account reference, permitted capability, page/scan limits. | A role cannot add a new source or action capability by changing an unreviewed prompt. |
| Campaign policy | Active segment, approved claims/template versions, channels, sender identity, action windows, follow-up policy. | Operator-approved version stored and referenced by each action. |
| Limits | Daily/periodic channel quotas, per-provider request/token limits, estimated spend ceilings, concurrency caps. | No copied numeric quota becomes active automatically; absence of an approved limit disables that external lane. |
| Reliability | Request deadlines, retry/backoff policy, dead-letter threshold, cursor lag threshold, backup interval and retention. | External-effect retries have stricter rules than read retries. |
| Dashboards | Spreadsheet ID, exact tab names and ID-column mapping, report generation schedule. | `Lumen_Target_Leads`, `Campaign_Analytics`, and `Lumen_Support_Tickets` are canonical projections. |

## Secret inventory and storage

| Secret | Used by | Handling |
| --- | --- | --- |
| Lumen API credential, if the real service requires one | Enrichment adapter | Inject at runtime; never copy the example bearer token from a PDF. |
| Email account OAuth token/app credential | Send and inbox adapters | Scope to the intended mailbox and actions; rotate/revoke independently. |
| Google Workspace service account/OAuth material | Sheets/Drive projection | Grant access only to the required files/scopes; store JSON outside the repository with restricted Windows ACLs. |
| Reddit/Product Hunt/other platform token | Corresponding authorized adapter | Keep per-platform scopes distinct; a read token is not a posting token. |
| Contact provider and model API keys | Contact and model adapters | Separate keys and spend controls; redact from provider errors. |
| Suppression lookup HMAC key | Suppression vault | Protect and back up separately; losing it can break historical suppression matching. |
| Webhook verification secret, if used | Inbound event adapter | Verify signature/replay window before writing a trusted event. |

Use Windows Credential Manager, a protected local secret store, or environment injection appropriate to the chosen process manager. A `.env` file may be used only for local development if excluded from Git, protected by file permissions, and never printed. `.env.example` contains variable names and harmless placeholders, not usable secrets. Logs, exception strings, model prompts, screenshots, generated Markdown, and Sheet rows must not contain secret values.

## Suggested configuration names

These are names for the future implementation, not values to fill from the drafts:

```text
LMOS_ENVIRONMENT
LMOS_WORKSPACE_ROOT
LMOS_DATABASE_PATH
LMOS_DRY_RUN
LMOS_EXTERNAL_ACTIONS_ENABLED
LMOS_PAUSE_FILE
LUMEN_API_BASE_URL
LUMEN_API_TOKEN_REF
EMAIL_SENDER_ACCOUNT_REF
EMAIL_INBOX_ACCOUNT_REF
GOOGLE_WORKSPACE_CREDENTIAL_REF
GOOGLE_SPREADSHEET_ID
REDDIT_CREDENTIAL_REF
PRODUCT_HUNT_CREDENTIAL_REF
CONTACT_PROVIDER_CREDENTIAL_REF
MODEL_PROVIDER_CREDENTIAL_REF
SUPPRESSION_HMAC_KEY_REF
```

`*_REF` identifies an entry in the protected store; it is not a key string. Provider-specific URLs, OAuth scopes, model names, and pricing are verified during adapter implementation. The two different sample Lumen API paths in the PDFs are not a configuration decision.

## Startup validation

At startup the OS checks: config schema/version, valid local paths and writable database, migration level, pause signal, protected secret references needed by each enabled lane, authorized account capability, approved quota/budget policy, and no conflicting live/dry-run settings. A connector without credentials may remain disabled while unrelated local work runs. An outbound lane without exact account authorization or approved quota cannot start. Diagnostic output names the missing capability without exposing a credential.

## Change and rotation procedure

Changing strategy, budget, quota, channel permission, sender account, or live action mode creates a versioned operator command and audit event. Workers load the new policy at a safe boundary, and queued external actions are rechecked before dispatch. Key rotation uses overlap only where the provider supports it; a failed rotation pauses the affected adapter and preserves pending jobs. Revoking an account stops only its lane. Backup documentation records how to restore key references and the suppression key without putting key material in the backup manifest.

## Privacy and retention

Store only fields required for qualification, contact, support, or measurement. Keep source evidence long enough to substantiate claims, subject to legal/provider requirements and an operator-approved retention schedule. Remove or anonymize expired personal data while retaining minimal no-contact markers when needed to honor suppression. Exports and backups receive the same access controls and deletion schedule as the database. Before live use, confirm applicable sender/privacy obligations for the actual markets and channels; the OS must be able to enforce the resulting rules as configuration/policy.
