"""Server-side, Postgres-persisted import jobs - replaces the earlier
in-memory staging dict so a job (and its uploaded file bytes + validation
result) survives page navigation and outlives a single request, per this
stage's explicit requirement. See app/api/routes_import.py for the state
machine (QUEUED -> VALIDATING -> IMPORTING -> READY / FAILED) and the
bulk-insert step that actually creates rows in the existing
app.models.financial tables.
"""
from sqlalchemy import (
    Column,
    DateTime,
    ForeignKey,
    Integer,
    JSON,
    LargeBinary,
    String,
    Text,
    func,
)

from app.db.session import Base
from app.db.types import BIGINT_PK


class ImportJob(Base):
    __tablename__ = "import_jobs"

    id = Column(BIGINT_PK, primary_key=True, autoincrement=True)
    job_id = Column(String(64), unique=True, nullable=False, index=True)
    user_id = Column(BIGINT_PK, ForeignKey("users.id"), nullable=True, index=True)
    status = Column(String(32), nullable=False, default="QUEUED", server_default="QUEUED", index=True)
    dataset_version = Column(String(64), nullable=True)
    batch_id = Column(String(64), nullable=True)
    error_message = Column(Text, nullable=True)
    files_total = Column(Integer, nullable=False, default=0, server_default="0")
    rows_total = Column(Integer, nullable=False, default=0, server_default="0")
    rows_inserted = Column(Integer, nullable=False, default=0, server_default="0")
    # Human-readable stage within IMPORTING (e.g. "inserting payments
    # (250,000 / 500,000 rows so far)") - updated and committed by
    # _run_import after each record-type's bulk insert actually lands, so
    # a large import shows real, incrementally-advancing progress instead
    # of sitting at "IMPORTING" with no signal for the whole run.
    current_stage = Column(String(128), nullable=True)
    created_at = Column(DateTime(timezone=True), nullable=False, server_default=func.now())
    updated_at = Column(DateTime(timezone=True), nullable=False, server_default=func.now(), onupdate=func.now())


class ImportJobFile(Base):
    """One uploaded file within a job. raw_bytes is kept so the actual
    bulk-insert step (IMPORTING) can re-parse deterministically from the
    original upload rather than trying to round-trip parsed Python
    values (datetimes, ints) through JSON - app.api.import_detect's
    parse_csv/validate_rows are pure functions, so re-running them here
    against the same bytes always reproduces the same validation result
    shown at detect time."""

    __tablename__ = "import_job_files"

    id = Column(BIGINT_PK, primary_key=True, autoincrement=True)
    job_id = Column(String(64), ForeignKey("import_jobs.job_id"), nullable=False, index=True)
    filename = Column(String(255), nullable=False)
    detected_type = Column(String(32), nullable=False)
    raw_bytes = Column(LargeBinary, nullable=False)
    columns_found = Column(JSON, nullable=False, default=list)
    row_count = Column(Integer, nullable=False, default=0)
    valid_row_count = Column(Integer, nullable=False, default=0)
    invalid_row_count = Column(Integer, nullable=False, default=0)
    duplicate_count = Column(Integer, nullable=False, default=0)
    missing_field_count = Column(Integer, nullable=False, default=0)
    missing_required_columns = Column(JSON, nullable=False, default=list)
    sample_errors = Column(JSON, nullable=False, default=list)
    preview_rows = Column(JSON, nullable=False, default=list)
