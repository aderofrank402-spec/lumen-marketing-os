from __future__ import annotations

import sqlite3

import pytest

from lumen_marketing_os.db.database import Database
from lumen_marketing_os.core.errors import MigrationError


def test_migrations_are_repeatable_and_cover_domain_and_runtime(tmp_path):
    database = Database(tmp_path / "lumen_growth.db")
    first = database.initialize()
    assert first == ["0001_domain.sql", "0002_runtime.sql", "0003_research.sql",
                     "0004_qualification.sql", "0005_contact_freshness.sql",
                         "0006_fit_input_version.sql", "0007_content.sql", "0008_outreach.sql",
                         "0009_outreach_timezone.sql", "0010_operations.sql",
                         "0011_community.sql", "0012_community_import_handoff.sql",
                         "0013_audit_control.sql", "0014_discovery_escalation.sql",
                         "0015_support.sql", "0016_research_reviews.sql",
                         "0017_research_handoffs.sql", "0018_research_intake_screening.sql"]
    assert database.initialize() == []
    status = database.integrity_check()
    assert status["ok"] is True
    with database.read() as connection:
        names = {row[0] for row in connection.execute(
            "SELECT name FROM sqlite_master WHERE type='table'")}
        assert {"organizations", "prospects", "enrichment_attempts", "drafts", "approvals",
                "actions", "suppression_rules", "paid_conversions", "jobs", "job_attempts",
                "outbox_events", "system_controls", "audit_events", "research_findings",
                "research_source_health", "research_runs", "qualification_intakes",
                "qualification_rubrics", "contact_research", "contact_provenance",
                "qualification_provider_calls", "outreach_accounts", "outreach_action_bindings",
                "inbox_cursors", "inbound_classifications", "response_proposals",
                "calendar_booking_bindings", "schedule_occurrences", "usage_ledger",
                "provider_prices", "backup_records", "restore_drills",
                "community_tasks", "community_trigger_definitions", "community_observations",
                "community_drafts", "community_import_batches", "community_import_rows",
                "community_task_import_links", "support_sources", "support_tickets",
                "support_ticket_events", "support_classifications", "support_response_drafts",
                "support_sheet_sync", "support_response_handoffs",
                "research_review_results", "research_handoff_receipts",
                "research_handoff_intakes", "research_intake_screenings"} <= names
        assert connection.execute("PRAGMA foreign_keys").fetchone()[0] == 1
        assert connection.execute("PRAGMA journal_mode").fetchone()[0].lower() == "wal"


def test_foreign_keys_and_unique_payment_transaction(tmp_path):
    database = Database(tmp_path / "lumen_growth.db")
    database.initialize()
    with pytest.raises(sqlite3.IntegrityError):
        with database.transaction() as connection:
            connection.execute(
                "INSERT INTO paid_conversions(id,product_account_id,provider,transaction_id,"
                "verification_reference,paid_at_utc,verified_at_utc) VALUES "
                "('one','account','billing','tx-1','receipt','2026-09-30T12:00:00+00:00',"
                "'2026-09-30T12:00:00+00:00')"
            )
            connection.execute(
                "INSERT INTO paid_conversions(id,product_account_id,provider,transaction_id,"
                "verification_reference,paid_at_utc,verified_at_utc) VALUES "
                "('two','account','billing','tx-1','receipt','2026-09-30T12:00:00+00:00',"
                "'2026-09-30T12:00:00+00:00')"
            )
    with database.read() as connection:
        assert connection.execute("SELECT COUNT(*) FROM paid_conversions").fetchone()[0] == 0
        with pytest.raises(sqlite3.IntegrityError):
            connection.execute(
                "INSERT INTO prospects(id,organization_id,segment,lifecycle_state,created_at_utc,"
                "updated_at_utc) VALUES ('p','missing','segment','discovered','now','now')"
            )


def test_consistent_backup_and_restore(tmp_path):
    database = Database(tmp_path / "runtime" / "lumen_growth.db")
    database.initialize()
    with database.transaction() as connection:
        connection.execute(
            "INSERT INTO organizations(id,display_name,normalized_name,identity_status,"
            "created_at_utc,updated_at_utc) VALUES ('org','Example','example','verified','now','now')"
        )
    backup = database.backup(tmp_path / "backups" / "snapshot.db")
    restored = Database(backup)
    assert restored.integrity_check()["ok"] is True
    with restored.read() as connection:
        assert connection.execute("SELECT display_name FROM organizations WHERE id='org'").fetchone()[0] == "Example"
    with pytest.raises(FileExistsError):
        database.backup(backup)


def test_migration_checksum_change_is_detected(tmp_path):
    database = Database(tmp_path / "lumen_growth.db")
    database.initialize()
    with database.transaction() as connection:
        connection.execute(
            "UPDATE schema_migrations SET checksum='wrong' WHERE version='0001_domain.sql'"
        )
    with pytest.raises(MigrationError):
        database.initialize()
