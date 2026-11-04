"""Shared, namespace-scoped storage for Activity Monitor.

This metadata is intentionally separate from ``NamespaceBase``. Normal
namespace provisioning must not create or change Activity Monitor tables;
those are added only through the explicit namespace CLI operation.
"""

from sqlalchemy import (
    BigInteger,
    Boolean,
    Column,
    Date,
    DateTime,
    ForeignKey,
    Index,
    Integer,
    JSON,
    String,
    Text,
    UniqueConstraint,
)
from sqlalchemy.ext.declarative import declarative_base


ActivityMonitorBase = declarative_base()


class ActivityMonitorRider(ActivityMonitorBase):
    """Stable monitor identity and latest CDB roster snapshot for a rider."""

    __tablename__ = 'activity_monitor_riders'

    id = Column(Integer, primary_key=True)
    identity_key = Column(String(160), nullable=False, unique=True)
    source_worlddb_rider_id = Column(BigInteger)
    identity_generation = Column(Integer, nullable=False, default=0)
    first_cycling_id = Column(BigInteger, index=True)
    first_name = Column(String(120), nullable=False, default='')
    last_name = Column(String(160), nullable=False, default='')
    birth_date = Column(Date)
    nationality = Column(String(100))
    nationality_code = Column(String(10))
    team_id = Column(Integer)
    team_name = Column(String(255), nullable=False, default='')
    pcs_rider_path = Column(String(500))
    identity_status = Column(String(30), nullable=False, default='unverified')
    identity_hold = Column(Boolean, nullable=False, default=False)
    identity_hold_reason = Column(Text)
    eligible = Column(Boolean, nullable=False, default=True)
    present_in_cdb = Column(Boolean, nullable=False, default=True)
    eligibility_reason = Column(Text)
    last_seen_cdb_at = Column(DateTime)
    created_at = Column(DateTime, nullable=False)
    updated_at = Column(DateTime, nullable=False)

    __table_args__ = (
        UniqueConstraint(
            'source_worlddb_rider_id',
            'identity_generation',
            name='uq_activity_monitor_rider_source_generation',
        ),
        Index('idx_activity_monitor_rider_name', 'last_name', 'first_name'),
    )


class ActivityMonitorCDBImport(ActivityMonitorBase):
    """Auditable roster synchronization; deliberately stores no local path."""

    __tablename__ = 'activity_monitor_cdb_imports'

    id = Column(Integer, primary_key=True)
    source_sha256 = Column(String(64), nullable=False, index=True)
    reference_date = Column(Date, nullable=False)
    imported_at = Column(DateTime, nullable=False)
    imported_by_user_id = Column(Integer)
    rider_count = Column(Integer, nullable=False)
    summary = Column(JSON, nullable=False, default=dict)

    __table_args__ = (
        UniqueConstraint(
            'source_sha256', 'reference_date',
            name='uq_activity_monitor_cdb_import_source_day',
        ),
    )


class ActivityMonitorRosterSnapshot(ActivityMonitorBase):
    """Rider data as it appeared in a particular CDB synchronization."""

    __tablename__ = 'activity_monitor_roster_snapshots'

    id = Column(Integer, primary_key=True)
    cdb_import_id = Column(
        Integer,
        ForeignKey('activity_monitor_cdb_imports.id', ondelete='CASCADE'),
        nullable=False,
    )
    rider_id = Column(
        Integer,
        ForeignKey('activity_monitor_riders.id', ondelete='CASCADE'),
        nullable=False,
    )
    source_worlddb_rider_id = Column(BigInteger)
    identity_generation = Column(Integer, nullable=False)
    roster_data = Column(JSON, nullable=False)

    __table_args__ = (
        UniqueConstraint('cdb_import_id', 'rider_id', name='uq_activity_monitor_roster_snapshot'),
        Index('idx_activity_monitor_roster_snapshot_rider', 'rider_id'),
    )


class ActivityMonitorRun(ActivityMonitorBase):
    """A manual, batch, recovery, or scheduled source-check run."""

    __tablename__ = 'activity_monitor_runs'

    id = Column(Integer, primary_key=True)
    source_record_key = Column(String(240), unique=True)
    run_type = Column(String(30), nullable=False)
    status = Column(String(30), nullable=False, default='queued', index=True)
    initiated_by_user_id = Column(Integer)
    reference_date = Column(Date, nullable=False)
    started_at = Column(DateTime)
    finished_at = Column(DateTime)
    requested_count = Column(Integer, nullable=False, default=0)
    attempted_count = Column(Integer, nullable=False, default=0)
    successful_count = Column(Integer, nullable=False, default=0)
    matched_count = Column(Integer, nullable=False, default=0)
    needs_review_count = Column(Integer, nullable=False, default=0)
    heartbeat_at = Column(DateTime)
    stop_reason = Column(Text)
    settings = Column(JSON, nullable=False, default=dict)
    error_message = Column(Text)
    created_at = Column(DateTime, nullable=False)


class ActivityMonitorSourceCheck(ActivityMonitorBase):
    """Append-only outcome and coverage record for one rider/source check."""

    __tablename__ = 'activity_monitor_source_checks'

    id = Column(Integer, primary_key=True)
    source_record_key = Column(String(240), unique=True)
    run_id = Column(Integer, ForeignKey('activity_monitor_runs.id', ondelete='SET NULL'))
    rider_id = Column(
        Integer,
        ForeignKey('activity_monitor_riders.id', ondelete='SET NULL'),
        index=True,
    )
    source_worlddb_rider_id = Column(BigInteger)
    identity_generation = Column(Integer, nullable=False, default=0)
    first_cycling_id = Column(BigInteger)
    season = Column(Integer)
    source = Column(String(20), nullable=False, index=True)
    source_status = Column(String(40), nullable=False)
    coverage_status = Column(String(40), nullable=False, default='unverified')
    identity_result = Column(String(40), nullable=False, default='not_checked')
    checked_at = Column(DateTime, nullable=False, index=True)
    http_status = Column(Integer)
    response_sha256 = Column(String(64))
    request_path = Column(String(500))
    parser_version = Column(Integer)
    message = Column(Text)

    __table_args__ = (
        Index('idx_activity_monitor_check_source_status', 'source', 'source_status'),
        Index('idx_activity_monitor_check_rider_source', 'rider_id', 'source', 'checked_at'),
        Index(
            'idx_activity_monitor_check_rider_generation_time',
            'rider_id', 'identity_generation', 'checked_at', 'id',
        ),
    )


class ActivityMonitorResult(ActivityMonitorBase):
    """Race or stage evidence used to determine a rider's recent activity."""

    __tablename__ = 'activity_monitor_results'

    id = Column(Integer, primary_key=True)
    source_result_key = Column(String(200), nullable=False, unique=True)
    rider_id = Column(
        Integer,
        ForeignKey('activity_monitor_riders.id', ondelete='SET NULL'),
        index=True,
    )
    source_worlddb_rider_id = Column(BigInteger)
    identity_generation = Column(Integer, nullable=False, default=0)
    first_cycling_id = Column(BigInteger, index=True)
    season = Column(Integer, nullable=False, index=True)
    race_id = Column(BigInteger)
    edition_id = Column(BigInteger)
    race_name = Column(String(255), nullable=False, default='')
    race_category = Column(String(100), nullable=False, default='')
    race_url = Column(String(1000), nullable=False, default='')
    result_date = Column(Date, index=True)
    stage_id = Column(BigInteger)
    stage_number = Column(Integer)
    stage_name = Column(String(255), nullable=False, default='')
    row_type = Column(String(40), nullable=False)
    result_position = Column(String(40), nullable=False, default='')
    gc_position = Column(String(40), nullable=False, default='')
    participation_status = Column(String(40), nullable=False)
    source = Column(String(20), nullable=False, index=True)
    source_check_id = Column(
        Integer,
        ForeignKey('activity_monitor_source_checks.id', ondelete='SET NULL'),
    )
    identity_verified = Column(Boolean, nullable=False, default=False)
    imported_at = Column(DateTime, nullable=False)
    source_payload = Column(JSON)

    __table_args__ = (
        Index('idx_activity_monitor_result_rider_date', 'rider_id', 'result_date'),
        Index('idx_activity_monitor_result_status_date', 'participation_status', 'result_date'),
        Index(
            'idx_activity_monitor_result_rider_generation_date',
            'rider_id', 'identity_generation', 'result_date',
            postgresql_where=identity_verified.is_(True),
        ),
        Index(
            'idx_activity_monitor_result_rider_latest_verified',
            'rider_id',
            result_date.desc().nulls_last(),
            id.desc(),
            postgresql_include=[
                'participation_status', 'result_position', 'source',
                'source_check_id', 'race_name',
            ],
            postgresql_where=identity_verified.is_(True) & rider_id.is_not(None),
        ),
        Index(
            'idx_activity_monitor_result_source_check',
            'source_check_id',
            postgresql_where=source_check_id.is_not(None),
        ),
    )


class ActivityMonitorProfileLink(ActivityMonitorBase):
    """Current or historical PCS profile link, including its provenance."""

    __tablename__ = 'activity_monitor_profile_links'

    id = Column(Integer, primary_key=True)
    source_record_key = Column(String(240), unique=True)
    rider_id = Column(
        Integer,
        ForeignKey('activity_monitor_riders.id', ondelete='CASCADE'),
        nullable=False,
        index=True,
    )
    identity_generation = Column(Integer, nullable=False)
    profile_path = Column(String(500))
    link_source = Column(String(30), nullable=False)
    link_status = Column(String(30), nullable=False, default='candidate')
    is_current = Column(Boolean, nullable=False, default=False)
    override_url = Column(String(500))
    entered_value = Column(String(1000))
    override_updated_at = Column(DateTime)
    override_error_at = Column(DateTime)
    exported_path = Column(String(500))
    exported_override = Column(String(500))
    approved_profile_path = Column(String(500))
    cdb_profile_path = Column(String(500))
    profile_slug = Column(String(255))
    cdb_link_updated_at = Column(DateTime)
    checked_at = Column(DateTime)
    checked_url = Column(String(1000))
    http_status = Column(Integer)
    check_result = Column(String(40), nullable=False, default='not_checked')
    check_result_detail = Column(Text)
    profile_name = Column(String(255))
    profile_birth_date = Column(Date)
    profile_nationality = Column(String(100))
    identity_reason = Column(Text)
    override_error = Column(Text)
    message = Column(Text)
    notes = Column(Text)
    approved_pcs_birth_date = Column(Date)
    approved_worlddb_birth_date = Column(Date)
    approved_at = Column(DateTime)
    approved_by_user_id = Column(Integer)
    parser_version = Column(Integer)
    created_at = Column(DateTime, nullable=False)
    verified_at = Column(DateTime)

    __table_args__ = (
        Index('idx_activity_monitor_profile_current', 'rider_id', 'identity_generation', 'is_current'),
        Index('idx_activity_monitor_profile_path', 'profile_path'),
    )


class ActivityMonitorReview(ActivityMonitorBase):
    """Current shared review state; actor is recorded, ownership is namespace-wide."""

    __tablename__ = 'activity_monitor_reviews'

    id = Column(Integer, primary_key=True)
    rider_id = Column(
        Integer,
        ForeignKey('activity_monitor_riders.id', ondelete='CASCADE'),
        nullable=False,
    )
    identity_generation = Column(Integer, nullable=False)
    review_kind = Column(String(30), nullable=False, default='retirement')
    review_status = Column(String(30), nullable=False, default='not_reviewed')
    notes = Column(Text, nullable=False, default='')
    supporting_links = Column(JSON, nullable=False, default=list)
    last_reviewed_at = Column(DateTime)
    reviewed_by_user_id = Column(Integer)
    updated_at = Column(DateTime, nullable=False)

    __table_args__ = (
        UniqueConstraint(
            'rider_id', 'identity_generation', 'review_kind',
            name='uq_activity_monitor_review_scope',
        ),
        Index('idx_activity_monitor_review_status', 'review_kind', 'review_status'),
    )


class ActivityMonitorReviewEvent(ActivityMonitorBase):
    """Immutable history of review decisions and identity resolutions."""

    __tablename__ = 'activity_monitor_review_events'

    id = Column(Integer, primary_key=True)
    source_record_key = Column(String(240), unique=True)
    rider_id = Column(
        Integer,
        ForeignKey('activity_monitor_riders.id', ondelete='CASCADE'),
        nullable=False,
        index=True,
    )
    identity_generation = Column(Integer, nullable=False)
    review_kind = Column(String(30), nullable=False)
    old_status = Column(String(30))
    new_status = Column(String(30), nullable=False)
    notes = Column(Text, nullable=False, default='')
    supporting_links = Column(JSON, nullable=False, default=list)
    actor_user_id = Column(Integer)
    legacy_actor = Column(String(120))
    occurred_at = Column(DateTime, nullable=False, index=True)


class ActivityMonitorSchedule(ActivityMonitorBase):
    """One shared namespace schedule, executed by PCM Manager's worker."""

    __tablename__ = 'activity_monitor_schedules'

    id = Column(Integer, primary_key=True)
    schedule_key = Column(String(40), nullable=False, unique=True, default='activity-monitor')
    enabled = Column(Boolean, nullable=False, default=False)
    checks_per_hour = Column(Integer, nullable=False, default=1)
    window_start = Column(String(5), nullable=False, default='08:00')
    window_end = Column(String(5), nullable=False, default='22:00')
    delay_min_seconds = Column(Integer, nullable=False, default=59)
    delay_max_seconds = Column(Integer, nullable=False, default=61)
    next_run_at = Column(DateTime)
    last_run_at = Column(DateTime)
    updated_by_user_id = Column(Integer)
    updated_at = Column(DateTime, nullable=False)


class ActivityMonitorRiderSchedule(ActivityMonitorBase):
    """A point-in-time record of a rider's announced race schedule."""

    __tablename__ = 'activity_monitor_rider_schedules'

    id = Column(Integer, primary_key=True)
    source_record_key = Column(String(240), unique=True)
    rider_id = Column(
        Integer,
        ForeignKey('activity_monitor_riders.id', ondelete='SET NULL'),
        index=True,
    )
    source_worlddb_rider_id = Column(BigInteger)
    identity_generation = Column(Integer, nullable=False)
    reference_date = Column(Date, nullable=False)
    recorded_at = Column(DateTime, nullable=False, index=True)
    schedule_data = Column(JSON, nullable=False)

    __table_args__ = (
        Index(
            'idx_activity_monitor_schedule_rider_latest',
            'rider_id', 'reference_date', 'recorded_at', 'id',
        ),
    )


class ActivityMonitorEligibilityEvent(ActivityMonitorBase):
    """History explaining why a rider entered or left the monitored roster."""

    __tablename__ = 'activity_monitor_eligibility_events'

    id = Column(Integer, primary_key=True)
    source_record_key = Column(String(240), unique=True)
    rider_id = Column(
        Integer,
        ForeignKey('activity_monitor_riders.id', ondelete='SET NULL'),
        index=True,
    )
    source_worlddb_rider_id = Column(BigInteger, nullable=False)
    cdb_import_id = Column(Integer, ForeignKey('activity_monitor_cdb_imports.id', ondelete='SET NULL'))
    reference_date = Column(Date, nullable=False)
    recorded_at = Column(DateTime, nullable=False, index=True)
    old_eligible = Column(Boolean)
    new_eligible = Column(Boolean, nullable=False)
    reason = Column(Text, nullable=False)
    legacy_origin = Column(String(80))


class ActivityMonitorTask(ActivityMonitorBase):
    """Resumable background work item for online source checks and research."""

    __tablename__ = 'activity_monitor_tasks'

    id = Column(Integer, primary_key=True)
    source_record_key = Column(String(240), unique=True)
    rider_id = Column(
        Integer,
        ForeignKey('activity_monitor_riders.id', ondelete='SET NULL'),
        index=True,
    )
    source_worlddb_rider_id = Column(BigInteger)
    identity_generation = Column(Integer, nullable=False)
    run_id = Column(Integer, ForeignKey('activity_monitor_runs.id', ondelete='SET NULL'))
    state = Column(String(30), nullable=False, index=True)
    source_scope = Column(JSON, nullable=False, default=list)
    reason = Column(Text, nullable=False)
    reference_date = Column(Date, nullable=False)
    assigned_to_user_id = Column(Integer)
    legacy_assignee = Column(String(120))
    created_at = Column(DateTime, nullable=False)
    updated_at = Column(DateTime, nullable=False)
    next_retry_at = Column(DateTime)
    outcome = Column(Text, nullable=False, default='')


class ActivityMonitorTaskEvent(ActivityMonitorBase):
    """Audit trail for background task state transitions."""

    __tablename__ = 'activity_monitor_task_events'

    id = Column(Integer, primary_key=True)
    source_record_key = Column(String(240), unique=True)
    task_id = Column(
        Integer,
        ForeignKey('activity_monitor_tasks.id', ondelete='CASCADE'),
        nullable=False,
        index=True,
    )
    old_state = Column(String(30))
    new_state = Column(String(30), nullable=False)
    actor_user_id = Column(Integer)
    legacy_actor = Column(String(120))
    occurred_at = Column(DateTime, nullable=False, index=True)
    notes = Column(Text, nullable=False, default='')


class ActivityMonitorIdentityAlert(ActivityMonitorBase):
    """Open or resolved identity discrepancy shown in Problems."""

    __tablename__ = 'activity_monitor_identity_alerts'

    id = Column(Integer, primary_key=True)
    source_record_key = Column(String(240), unique=True)
    rider_id = Column(
        Integer,
        ForeignKey('activity_monitor_riders.id', ondelete='SET NULL'),
        index=True,
    )
    source_worlddb_rider_id = Column(BigInteger, nullable=False)
    cdb_import_id = Column(Integer, ForeignKey('activity_monitor_cdb_imports.id', ondelete='SET NULL'))
    created_at = Column(DateTime, nullable=False, index=True)
    old_identity = Column(JSON, nullable=False)
    new_identity = Column(JSON, nullable=False)
    reason = Column(Text, nullable=False)
    status = Column(String(30), nullable=False, default='open', index=True)

    __table_args__ = (
        Index('idx_activity_monitor_identity_alert_rider_status', 'rider_id', 'status'),
    )
    resolved_at = Column(DateTime)
    resolved_by_user_id = Column(Integer)
    legacy_resolved_by = Column(String(120))
    resolution = Column(Text)


class ActivityMonitorHistoryImport(ActivityMonitorBase):
    """Auditable, fingerprinted batch for the one-time history migration."""

    __tablename__ = 'activity_monitor_history_imports'

    id = Column(Integer, primary_key=True)
    source_sha256 = Column(String(64), nullable=False, unique=True)
    source_schema_fingerprint = Column(String(64), nullable=False)
    status = Column(String(20), nullable=False, default='running', index=True)
    initiated_by_user_id = Column(Integer)
    started_at = Column(DateTime, nullable=False)
    completed_at = Column(DateTime)
    source_row_counts = Column(JSON, nullable=False, default=dict)
    imported_row_counts = Column(JSON, nullable=False, default=dict)
    unlinked_record_count = Column(Integer, nullable=False, default=0)
    error_summary = Column(Text)


class ActivityMonitorLegacyRecord(ActivityMonitorBase):
    """Idempotent holding area for durable legacy records not yet normalized."""

    __tablename__ = 'activity_monitor_legacy_records'

    id = Column(Integer, primary_key=True)
    record_type = Column(String(80), nullable=False)
    source_record_key = Column(String(200), nullable=False)
    rider_id = Column(
        Integer,
        ForeignKey('activity_monitor_riders.id', ondelete='SET NULL'),
        index=True,
    )
    occurred_at = Column(DateTime, index=True)
    record_data = Column(JSON, nullable=False)
    imported_at = Column(DateTime, nullable=False)

    __table_args__ = (
        UniqueConstraint(
            'record_type', 'source_record_key',
            name='uq_activity_monitor_legacy_source_record',
        ),
        Index(
            'idx_activity_monitor_legacy_rider_type_time',
            'rider_id', 'record_type', 'occurred_at', 'id',
        ),
    )


ACTIVITY_MONITOR_TABLES = tuple(ActivityMonitorBase.metadata.tables.values())
