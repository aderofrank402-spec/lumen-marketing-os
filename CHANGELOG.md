# Changelog

All notable public portfolio changes are documented here.

## [1.0.0] - 2026-11-02

### Added

- Public engineering release of the nine-department orchestration and control system
- 35 logical role definitions with durable department handoffs
- 18 versioned SQLite migrations covering workflow, policy, operations, and support state
- Durable jobs, leases, retries, recovery, review states, and outbox delivery
- Exact-hash approvals, suppression, quotas, pauses, provider-health checks, and audit controls
- 153-test verification suite
- Portfolio architecture, control model, operator walkthrough, and engineering decision records
- Automated public-artifact validation and GitHub Actions CI

### Security

- Development configuration remains dry-run by default
- Credentials and generated runtime state remain excluded from the repository
- External effects require explicit configuration and narrow approval
