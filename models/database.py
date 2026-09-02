"""
Database models for the Autopilot.
Defines PostgreSQL table schemas using SQLAlchemy ORM with async support.
"""

import uuid
from datetime import datetime
from enum import Enum, unique
from typing import Any, Optional
from zoneinfo import ZoneInfo

from sqlalchemy import (
    BigInteger,
    Boolean,
    CheckConstraint,
    DateTime,
    Float,
    ForeignKey,
    ForeignKeyConstraint,
    Index,
    Integer,
    String,
    Text,
    UniqueConstraint,
    text,
)
from sqlalchemy.dialects.postgresql import JSONB, UUID
from sqlalchemy.orm import (
    DeclarativeBase,
    Mapped,
    mapped_column,
    relationship,
)


IST = ZoneInfo("Asia/Kolkata")


def ist_now() -> datetime:
    """Return the current timezone-aware IST time for ORM-managed timestamps."""
    return datetime.now(IST)


# =============================================================================
# ENUMS
# =============================================================================


class AuthMethod(str, Enum):
    """Authentication method types."""

    LOCAL = "local"
    GOOGLE = "google"


@unique
class ApplicationStatus(str, Enum):
    """Job application status types."""

    DRAFT = "draft"
    PENDING = "pending"
    PROCESSING = "processing"
    COMPLETED = "completed"
    FAILED = "failed"
    APPLIED = "applied"
    INTERVIEW = "interview"
    REJECTED = "rejected"
    ACCEPTED = "accepted"
    DISCOVERED = "discovered"
    QUEUED = "queued"
    PREPARING = "preparing"
    APPLYING = "applying"
    BLOCKED = "blocked"
    SKIPPED = "skipped"
    RETRYING = "retrying"


@unique
class WorkflowStatusEnum(str, Enum):
    """Workflow status types for database."""

    INITIALIZED = "initialized"
    IN_PROGRESS = "in_progress"
    AWAITING_CONFIRMATION = "awaiting_confirmation"
    ANALYSIS_COMPLETE = "analysis_complete"
    COMPLETED = "completed"
    FAILED = "failed"
    CANCELLED = "cancelled"


# =============================================================================
# BASE MODEL
# =============================================================================


class Base(DeclarativeBase):
    """Base class for all SQLAlchemy models."""

    pass


# =============================================================================
# MODELS
# =============================================================================


class User(Base):
    """
    User account model - Authentication and basic identity only.

    Stores authentication credentials and basic user information.
    Extended profile data is stored in the UserProfile table.

    Indexes:
        - email (unique): Fast user lookup by email for authentication
    """

    __tablename__ = "users"

    # Primary Key
    id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True), primary_key=True, default=uuid.uuid4
    )

    # Authentication Fields
    email: Mapped[str] = mapped_column(
        String(255), unique=True, nullable=False, index=True
    )
    password_hash: Mapped[str | None] = mapped_column(String(255), nullable=True)
    auth_method: Mapped[str] = mapped_column(
        String(50), nullable=False, default=AuthMethod.LOCAL.value
    )

    # User Information
    full_name: Mapped[str] = mapped_column(String(255), nullable=False)
    profile_completed: Mapped[bool] = mapped_column(Boolean, default=False)
    profile_completion_percentage: Mapped[int] = mapped_column(Integer, default=0)

    # Admin Role
    is_admin: Mapped[bool] = mapped_column(Boolean, default=False)

    # Email Verification
    email_verified: Mapped[bool] = mapped_column(Boolean, default=False)
    email_verified_at: Mapped[datetime | None] = mapped_column(
        DateTime(timezone=True), nullable=True
    )

    # OAuth Fields
    google_id: Mapped[str | None] = mapped_column(
        String(255), nullable=True, unique=True, index=True
    )

    # API Keys (encrypted)
    gemini_api_key_encrypted: Mapped[str | None] = mapped_column(
        Text, nullable=True, default=None
    )

    # Timestamps
    last_login: Mapped[datetime | None] = mapped_column(
        DateTime(timezone=True), nullable=True
    )
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), default=ist_now
    )
    updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), default=ist_now, onupdate=ist_now
    )

    __table_args__ = (
        CheckConstraint(
            "(auth_method != 'local') OR (password_hash IS NOT NULL)",
            name="ck_users_local_auth_has_password",
        ),
    )

    # Relationships with proper cascade
    # Use lazy="noload" for collections to prevent N+1 queries - load explicitly when needed
    profile: Mapped[Optional["UserProfile"]] = relationship(
        "UserProfile",
        back_populates="user",
        uselist=False,
        lazy="selectin",
        cascade="all, delete-orphan",
    )
    applications: Mapped[list["JobApplication"]] = relationship(
        "JobApplication",
        back_populates="user",
        lazy="noload",
        cascade="all, delete-orphan",
    )
    workflow_sessions: Mapped[list["WorkflowSession"]] = relationship(
        "WorkflowSession",
        back_populates="user",
        lazy="noload",
        cascade="all, delete-orphan",
    )
    workflow_preferences: Mapped[Optional["UserWorkflowPreferences"]] = relationship(
        "UserWorkflowPreferences",
        back_populates="user",
        uselist=False,
        lazy="noload",
        cascade="all, delete-orphan",
    )
    resume_asset: Mapped[Optional["UserResumeAsset"]] = relationship(
        "UserResumeAsset",
        back_populates="user",
        uselist=False,
        lazy="noload",
        cascade="all, delete-orphan",
    )
    resume_versions: Mapped[list["ResumeVersion"]] = relationship(
        "ResumeVersion",
        back_populates="user",
        lazy="noload",
        cascade="all, delete-orphan",
    )
    job_form_answers: Mapped[list["JobFormAnswer"]] = relationship(
        "JobFormAnswer",
        back_populates="user",
        lazy="noload",
        cascade="all, delete-orphan",
    )
    portal_sessions: Mapped[list["PortalSession"]] = relationship(
        "PortalSession",
        back_populates="user",
        lazy="noload",
        cascade="all, delete-orphan",
    )
    job_search_preferences: Mapped[Optional["UserJobSearchPreference"]] = relationship(
        "UserJobSearchPreference",
        back_populates="user",
        uselist=False,
        lazy="noload",
        cascade="all, delete-orphan",
    )
    job_discoveries: Mapped[list["JobDiscovery"]] = relationship(
        "JobDiscovery",
        back_populates="user",
        lazy="noload",
        cascade="all, delete-orphan",
    )
    automation_batches: Mapped[list["ApplicationAutomationBatch"]] = relationship(
        "ApplicationAutomationBatch",
        back_populates="user",
        lazy="noload",
        cascade="all, delete-orphan",
    )
    application_holds: Mapped[list["ApplicationHold"]] = relationship(
        "ApplicationHold",
        back_populates="user",
        lazy="noload",
        cascade="all, delete-orphan",
    )
    application_draft_answers: Mapped[list["ApplicationDraftAnswer"]] = relationship(
        "ApplicationDraftAnswer",
        back_populates="user",
        lazy="noload",
        cascade="all, delete-orphan",
    )
    automation_worker_devices: Mapped[list["AutomationWorkerDevice"]] = relationship(
        "AutomationWorkerDevice",
        back_populates="user",
        lazy="noload",
        cascade="all, delete-orphan",
    )

    def to_dict(self) -> dict[str, Any]:
        """Convert user to dictionary for API responses."""
        return {
            "id": str(self.id),
            "email": self.email,
            "auth_method": self.auth_method,
            "full_name": self.full_name,
            "is_admin": self.is_admin,
            "email_verified": self.email_verified,
            "profile_completed": self.profile_completed,
            "profile_completion_percentage": self.profile_completion_percentage,
            "has_gemini_api_key": self.gemini_api_key_encrypted is not None,
            "has_google_linked": self.google_id is not None,
            "last_login": self.last_login.isoformat() if self.last_login else None,
            "created_at": self.created_at.isoformat() if self.created_at else None,
            "updated_at": self.updated_at.isoformat() if self.updated_at else None,
        }


class UserProfile(Base):
    """
    Extended user profile information.

    Stores detailed profile data including work experience, skills,
    and job preferences. Uses JSONB for flexible nested data.

    Indexes:
        - user_id (unique): One-to-one relationship with User
    """

    __tablename__ = "user_profiles"

    # Primary Key
    id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True), primary_key=True, default=uuid.uuid4
    )

    # Foreign Key to User
    user_id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True),
        ForeignKey("users.id", ondelete="CASCADE"),
        unique=True,
        index=True,
    )

    # Basic Information
    city: Mapped[str | None] = mapped_column(String(100), nullable=True)
    state: Mapped[str | None] = mapped_column(String(100), nullable=True)
    country: Mapped[str | None] = mapped_column(String(100), nullable=True)
    country_phone_code: Mapped[str | None] = mapped_column(String(8), nullable=True)
    postal_code: Mapped[str | None] = mapped_column(String(32), nullable=True)
    nationality: Mapped[str | None] = mapped_column(String(100), nullable=True)
    citizenship: Mapped[str | None] = mapped_column(String(100), nullable=True)
    # Explicitly opt-in portal details. Values are Fernet-encrypted; never expose
    # them from `to_dict()` or include them in LLM prompts.
    date_of_birth_encrypted: Mapped[str | None] = mapped_column(Text, nullable=True)
    pan_encrypted: Mapped[str | None] = mapped_column(Text, nullable=True)
    gender_encrypted: Mapped[str | None] = mapped_column(Text, nullable=True)
    sensitive_portal_autofill_enabled: Mapped[bool] = mapped_column(
        Boolean, nullable=False, default=False
    )
    professional_title: Mapped[str | None] = mapped_column(String(200), nullable=True)
    years_experience: Mapped[float | None] = mapped_column(Float, nullable=True)
    summary: Mapped[str | None] = mapped_column(Text, nullable=True)
    is_student: Mapped[bool] = mapped_column(Boolean, default=False)

    # Contact & application links (autofill, employer forms)
    phone: Mapped[str | None] = mapped_column(String(40), nullable=True)
    linkedin_url: Mapped[str | None] = mapped_column(String(500), nullable=True)
    github_url: Mapped[str | None] = mapped_column(String(500), nullable=True)
    portfolio_url: Mapped[str | None] = mapped_column(String(500), nullable=True)

    # Profile Sections (JSONB for flexibility) - use None as default, not mutable objects
    work_experience: Mapped[list[dict[str, Any]] | None] = mapped_column(
        JSONB, nullable=True, default=None
    )
    education: Mapped[list[dict[str, Any]] | None] = mapped_column(
        JSONB, nullable=True, default=None
    )
    skills: Mapped[list[str] | None] = mapped_column(JSONB, nullable=True, default=None)

    # Job Preferences (JSONB for flexibility)
    desired_salary_range: Mapped[dict[str, Any] | None] = mapped_column(
        JSONB, nullable=True, default=None
    )
    desired_company_sizes: Mapped[list[str] | None] = mapped_column(
        JSONB, nullable=True, default=None
    )
    job_types: Mapped[list[str] | None] = mapped_column(
        JSONB, nullable=True, default=None
    )
    work_arrangements: Mapped[list[str] | None] = mapped_column(
        JSONB, nullable=True, default=None
    )
    willing_to_relocate: Mapped[bool] = mapped_column(Boolean, default=False)
    requires_visa_sponsorship: Mapped[bool] = mapped_column(Boolean, default=False)
    work_authorization: Mapped[str | None] = mapped_column(
        String(40), nullable=True, default=None
    )
    has_security_clearance: Mapped[bool] = mapped_column(Boolean, default=False)
    max_travel_preference: Mapped[str | None] = mapped_column(String(50), nullable=True)

    # Timestamps
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), default=ist_now
    )
    updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), default=ist_now, onupdate=ist_now
    )

    # Relationships
    user: Mapped["User"] = relationship("User", back_populates="profile")

    def to_dict(self) -> dict[str, Any]:
        """Convert profile to dictionary for API responses."""
        return {
            "id": str(self.id),
            "user_id": str(self.user_id),
            "city": self.city,
            "state": self.state,
            "country": self.country,
            "country_phone_code": self.country_phone_code,
            "postal_code": self.postal_code,
            "nationality": self.nationality,
            "citizenship": self.citizenship,
            "has_sensitive_portal_details": bool(
                self.date_of_birth_encrypted
                or self.pan_encrypted
                or self.gender_encrypted
            ),
            "has_saved_gender": bool(self.gender_encrypted),
            "sensitive_portal_autofill_enabled": self.sensitive_portal_autofill_enabled,
            "professional_title": self.professional_title,
            "years_experience": self.years_experience,
            "summary": self.summary,
            "is_student": self.is_student,
            "phone": self.phone,
            "linkedin_url": self.linkedin_url,
            "github_url": self.github_url,
            "portfolio_url": self.portfolio_url,
            "work_experience": self.work_experience or [],
            "education": self.education or [],
            "skills": self.skills or [],
            "desired_salary_range": self.desired_salary_range or {},
            "desired_company_sizes": self.desired_company_sizes or [],
            "job_types": self.job_types or [],
            "work_arrangements": self.work_arrangements or [],
            "willing_to_relocate": self.willing_to_relocate,
            "requires_visa_sponsorship": self.requires_visa_sponsorship,
            "work_authorization": self.work_authorization,
            "has_security_clearance": self.has_security_clearance,
            "max_travel_preference": self.max_travel_preference,
            "created_at": self.created_at.isoformat() if self.created_at else None,
            "updated_at": self.updated_at.isoformat() if self.updated_at else None,
        }


class UserResumeAsset(Base):
    """
    Stored resume file metadata (binary on disk or object storage path).

    One row per user (replaced on re-upload).
    """

    __tablename__ = "user_resume_assets"

    id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True), primary_key=True, default=uuid.uuid4
    )
    user_id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True),
        ForeignKey("users.id", ondelete="CASCADE"),
        unique=True,
        nullable=False,
        index=True,
    )
    storage_relative_path: Mapped[str] = mapped_column(String(512), nullable=False)
    original_filename: Mapped[str] = mapped_column(String(255), nullable=False)
    mime_type: Mapped[str] = mapped_column(String(100), nullable=False)
    byte_size: Mapped[int] = mapped_column(BigInteger, nullable=False)
    sha256_hex: Mapped[str | None] = mapped_column(String(64), nullable=True)

    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), default=ist_now
    )
    updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), default=ist_now, onupdate=ist_now
    )

    user: Mapped["User"] = relationship("User", back_populates="resume_asset")

    def to_dict(self) -> dict[str, Any]:
        return {
            "id": str(self.id),
            "user_id": str(self.user_id),
            "original_filename": self.original_filename,
            "mime_type": self.mime_type,
            "byte_size": self.byte_size,
            "sha256_hex": self.sha256_hex,
            "created_at": self.created_at.isoformat() if self.created_at else None,
            "updated_at": self.updated_at.isoformat() if self.updated_at else None,
        }


class UserWorkflowPreferences(Base):
    """
    Per-user workflow behaviour preferences.

    1-to-1 with the User table (unique user_id). Rows are created on first
    PATCH; if no row exists the application falls back to column defaults.

    Indexes:
        - user_id (unique): direct lookup by user
    """

    __tablename__ = "user_workflow_preferences"

    id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True), primary_key=True, default=uuid.uuid4
    )
    user_id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True),
        ForeignKey("users.id", ondelete="CASCADE"),
        unique=True,
        index=True,
        nullable=False,
    )

    # Match-score threshold (0.0–1.0) below which the workflow pauses for
    # user confirmation before continuing with document generation.
    workflow_gate_threshold: Mapped[float] = mapped_column(
        Float, nullable=False, default=0.5
    )

    # When True, resume advice + cover letter are generated automatically
    # after company research.  When False (default), they are generated
    # on demand via POST /workflow/{session_id}/generate-documents.
    auto_generate_documents: Mapped[bool] = mapped_column(
        Boolean, nullable=False, default=False
    )

    # Tone used by the cover letter writer agent.
    # Values: 'professional' | 'conversational' | 'enthusiastic'
    cover_letter_tone: Mapped[str] = mapped_column(
        String(32), nullable=False, default="professional"
    )

    # How detailed the resume advice should be.
    # Values: 'concise' | 'detailed'
    resume_length: Mapped[str] = mapped_column(
        String(16), nullable=False, default="concise"
    )

    # Provider selected for resume advice and cover-letter generation.
    # The local endpoint remains instance-owned; users may select only its
    # model name, never an arbitrary endpoint.
    ai_provider: Mapped[str] = mapped_column(
        String(16), nullable=False, default="cloud"
    )

    # Preferred cloud model when user is in BYOK mode.
    # NULL means "use the system default".
    preferred_model: Mapped[str | None] = mapped_column(
        String(64), nullable=True, default=None
    )

    # Optional user-selected model exposed by the configured local endpoint.
    local_model: Mapped[str | None] = mapped_column(
        String(128), nullable=True, default=None
    )

    # Ollama reasoning level for gpt-oss models. Ignored by other local models.
    local_reasoning_effort: Mapped[str] = mapped_column(
        String(16), nullable=False, default="medium"
    )

    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), default=ist_now
    )
    updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), default=ist_now, onupdate=ist_now
    )

    # Relationship
    user: Mapped["User"] = relationship("User", back_populates="workflow_preferences")

    def to_dict(self) -> dict[str, Any]:
        """Convert to dictionary for API responses and workflow injection."""
        return {
            "workflow_gate_threshold": self.workflow_gate_threshold,
            "auto_generate_documents": self.auto_generate_documents,
            "cover_letter_tone": self.cover_letter_tone,
            "resume_length": self.resume_length,
            "ai_provider": self.ai_provider,
            "preferred_model": self.preferred_model,
            "local_model": self.local_model,
            "local_reasoning_effort": self.local_reasoning_effort,
        }


class WorkflowSession(Base):
    """
    Workflow processing session.

    Stores the state and results of a job application workflow,
    including all agent outputs. Uses JSONB for complex nested data.

    Indexes:
        - session_id (unique): Fast lookup by session identifier
        - user_id: Filter sessions by user
        - ix_workflow_user_status: Composite for filtering user's sessions by status
        - ix_workflow_user_created: Composite for listing user's recent sessions
    """

    __tablename__ = "workflow_sessions"

    # Primary Key - using session_id as the main identifier
    id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True), primary_key=True, default=uuid.uuid4
    )
    session_id: Mapped[str] = mapped_column(
        String(36), unique=True, nullable=False, index=True
    )

    # Foreign Key to User
    user_id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True), ForeignKey("users.id", ondelete="CASCADE"), index=True
    )

    # Workflow Control and Status
    workflow_status: Mapped[str] = mapped_column(
        String(50), default=WorkflowStatusEnum.INITIALIZED.value
    )
    current_phase: Mapped[str] = mapped_column(String(50), default="initialization")
    current_agent: Mapped[str | None] = mapped_column(String(100), nullable=True)

    # Agent Status Tracking (JSONB) - use None as default
    agent_status: Mapped[dict[str, str] | None] = mapped_column(
        JSONB, nullable=True, default=None
    )
    completed_agents: Mapped[list[str] | None] = mapped_column(
        JSONB, nullable=True, default=None
    )
    failed_agents: Mapped[list[str] | None] = mapped_column(
        JSONB, nullable=True, default=None
    )

    # Error Handling (JSONB)
    error_messages: Mapped[list[str] | None] = mapped_column(
        JSONB, nullable=True, default=None
    )
    warning_messages: Mapped[list[str] | None] = mapped_column(
        JSONB, nullable=True, default=None
    )

    # Input Data (JSONB)
    job_input_data: Mapped[dict[str, Any] | None] = mapped_column(
        JSONB, nullable=True, default=None
    )
    user_data: Mapped[dict[str, Any] | None] = mapped_column(
        JSONB, nullable=True, default=None
    )

    # Agent Processing Results (JSONB for complex nested data)
    job_analysis: Mapped[dict[str, Any] | None] = mapped_column(
        JSONB, nullable=True, default=None
    )
    company_research: Mapped[dict[str, Any] | None] = mapped_column(
        JSONB, nullable=True, default=None
    )
    profile_matching: Mapped[dict[str, Any] | None] = mapped_column(
        JSONB, nullable=True, default=None
    )
    resume_recommendations: Mapped[dict[str, Any] | None] = mapped_column(
        JSONB, nullable=True, default=None
    )
    cover_letter: Mapped[dict[str, Any] | None] = mapped_column(
        JSONB, nullable=True, default=None
    )

    # Interview Prep (generated on-demand after workflow completion)
    interview_prep: Mapped[dict[str, Any] | None] = mapped_column(
        JSONB, nullable=True, default=None
    )

    # Timing - Use proper DateTime instead of String for time-based queries
    processing_start_time: Mapped[datetime | None] = mapped_column(
        DateTime(timezone=True), nullable=True
    )
    processing_end_time: Mapped[datetime | None] = mapped_column(
        DateTime(timezone=True), nullable=True
    )
    agent_start_times: Mapped[dict[str, str] | None] = mapped_column(
        JSONB, nullable=True, default=None
    )
    agent_durations: Mapped[dict[str, float] | None] = mapped_column(
        JSONB, nullable=True, default=None
    )

    # Timestamps
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), default=ist_now
    )
    updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), default=ist_now, onupdate=ist_now
    )

    # Relationships
    user: Mapped["User"] = relationship("User", back_populates="workflow_sessions")
    application: Mapped[Optional["JobApplication"]] = relationship(
        "JobApplication", back_populates="workflow_session", uselist=False
    )

    # Composite Indexes for common query patterns
    __table_args__ = (
        # For filtering user's sessions by status (e.g., "show my in-progress workflows")
        Index("ix_workflow_user_status", "user_id", "workflow_status"),
        # For listing user's recent sessions ordered by time
        Index("ix_workflow_user_created", "user_id", "created_at"),
    )

    def to_dict(self) -> dict[str, Any]:
        """Convert workflow session to dictionary for API responses."""
        return {
            "id": str(self.id),
            "session_id": self.session_id,
            "user_id": str(self.user_id),
            "workflow_status": self.workflow_status,
            "current_phase": self.current_phase,
            "current_agent": self.current_agent,
            "agent_status": self.agent_status or {},
            "completed_agents": self.completed_agents or [],
            "failed_agents": self.failed_agents or [],
            "error_messages": self.error_messages or [],
            "warning_messages": self.warning_messages or [],
            "job_input_data": self.job_input_data or {},
            "user_data": self.user_data or {},
            "job_analysis": self.job_analysis or {},
            "company_research": self.company_research or {},
            "profile_matching": self.profile_matching or {},
            "resume_recommendations": self.resume_recommendations or {},
            "cover_letter": self.cover_letter or {},
            "interview_prep": self.interview_prep or {},
            "processing_start_time": (
                self.processing_start_time.isoformat()
                if self.processing_start_time
                else None
            ),
            "processing_end_time": (
                self.processing_end_time.isoformat()
                if self.processing_end_time
                else None
            ),
            "created_at": self.created_at.isoformat() if self.created_at else None,
            "updated_at": self.updated_at.isoformat() if self.updated_at else None,
        }


class JobApplication(Base):
    """
    Job application document.

    Stores essential job tracking information and references the workflow session
    for AI-generated content and analysis. This design reduces data duplication
    and ensures a single source of truth for workflow results.

    Indexes:
        - user_id: Filter applications by user
        - session_id: Link to workflow session
        - status: Filter by application status
        - created_at: Sort by creation date
        - ix_job_applications_user_status: Composite for user's applications by status
        - ix_job_applications_user_created: Composite for user's recent applications
    """

    __tablename__ = "job_applications"

    # Primary Key
    id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True), primary_key=True, default=uuid.uuid4
    )

    # Foreign Keys
    user_id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True), ForeignKey("users.id", ondelete="CASCADE"), index=True
    )
    session_id: Mapped[str | None] = mapped_column(
        String(36),
        ForeignKey("workflow_sessions.session_id", ondelete="SET NULL"),
        nullable=True,
        index=True,
    )

    # Job Information
    job_title: Mapped[str | None] = mapped_column(String(500), nullable=True)
    company_name: Mapped[str | None] = mapped_column(String(500), nullable=True)
    job_url: Mapped[str | None] = mapped_column(
        Text, nullable=True
    )  # NEW: Original job posting URL
    portal: Mapped[str | None] = mapped_column(String(50), nullable=True, index=True)
    external_job_id: Mapped[str | None] = mapped_column(String(255), nullable=True)
    external_ats_url: Mapped[str | None] = mapped_column(Text, nullable=True)
    job_description: Mapped[str | None] = mapped_column(Text, nullable=True)
    job_description_hash: Mapped[str | None] = mapped_column(String(64), nullable=True)
    job_description_captured_at: Mapped[datetime | None] = mapped_column(
        DateTime(timezone=True), nullable=True
    )
    automation_batch_id: Mapped[uuid.UUID | None] = mapped_column(
        UUID(as_uuid=True),
        ForeignKey("application_automation_batches.id", ondelete="SET NULL"),
        nullable=True,
        index=True,
    )
    workday_account_gate_id: Mapped[uuid.UUID | None] = mapped_column(
        UUID(as_uuid=True),
        ForeignKey("workday_account_gates.id", ondelete="SET NULL"),
        nullable=True,
        index=True,
    )
    automation_lease_id: Mapped[uuid.UUID | None] = mapped_column(
        UUID(as_uuid=True), nullable=True, index=True
    )
    automation_lease_expires_at: Mapped[datetime | None] = mapped_column(
        DateTime(timezone=True), nullable=True, index=True
    )

    # Match Score - store for quick access without loading full workflow
    match_score: Mapped[float | None] = mapped_column(
        Float, nullable=True
    )  # NEW: 0.0-1.0

    # Application Status Tracking
    status: Mapped[str] = mapped_column(
        String(50), default=ApplicationStatus.DRAFT.value, index=True
    )
    applied_date: Mapped[datetime | None] = mapped_column(
        DateTime(timezone=True), nullable=True
    )
    response_date: Mapped[datetime | None] = mapped_column(
        DateTime(timezone=True), nullable=True
    )

    # User Notes - personal notes about this application
    notes: Mapped[str | None] = mapped_column(Text, nullable=True)  # NEW: User's notes

    # Soft delete — set instead of hard DELETE to preserve audit history
    deleted_at: Mapped[datetime | None] = mapped_column(
        DateTime(timezone=True), nullable=True, default=None, index=True
    )

    # Timestamps
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), default=ist_now, index=True
    )
    updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), default=ist_now, onupdate=ist_now
    )

    # Relationships
    user: Mapped["User"] = relationship("User", back_populates="applications")
    workflow_session: Mapped[Optional["WorkflowSession"]] = relationship(
        "WorkflowSession", back_populates="application"
    )
    automation_batch: Mapped[Optional["ApplicationAutomationBatch"]] = relationship(
        "ApplicationAutomationBatch", back_populates="applications"
    )
    holds: Mapped[list["ApplicationHold"]] = relationship(
        "ApplicationHold",
        back_populates="application",
        lazy="noload",
        cascade="all, delete-orphan",
    )
    automation_events: Mapped[list["ApplicationAutomationEvent"]] = relationship(
        "ApplicationAutomationEvent",
        back_populates="application",
        lazy="noload",
        cascade="all, delete-orphan",
    )
    draft_answers: Mapped[list["ApplicationDraftAnswer"]] = relationship(
        "ApplicationDraftAnswer",
        back_populates="application",
        lazy="noload",
        cascade="all, delete-orphan",
    )

    # Constraints and Indexes
    __table_args__ = (
        # Partial unique index — only active (non-deleted) applications are constrained,
        # so a soft-deleted slot can be reused (migration 015 swaps the old full constraint).
        UniqueConstraint(
            "user_id", "job_title", "company_name", name="uq_user_job_company"
        ),
        # For filtering user's applications by status (e.g., "show my interviews")
        Index("ix_job_applications_user_status", "user_id", "status"),
        # For listing user's recent applications
        Index("ix_job_applications_user_created", "user_id", "created_at"),
        # For filtering by match score (e.g., "show my best matches")
        Index("ix_job_applications_user_score", "user_id", "match_score"),
    )

    def to_dict(self) -> dict[str, Any]:
        """Convert application to dictionary for API responses."""
        return {
            "id": str(self.id),
            "user_id": str(self.user_id),
            "session_id": self.session_id,
            "job_title": self.job_title,
            "company_name": self.company_name,
            "job_url": self.job_url,
            "portal": self.portal,
            "external_job_id": self.external_job_id,
            "external_ats_url": self.external_ats_url,
            "job_description": self.job_description,
            "job_description_hash": self.job_description_hash,
            "job_description_captured_at": (
                self.job_description_captured_at.isoformat()
                if self.job_description_captured_at
                else None
            ),
            "match_score": self.match_score,
            "status": self.status,
            "applied_date": (
                self.applied_date.isoformat() if self.applied_date else None
            ),
            "response_date": (
                self.response_date.isoformat() if self.response_date else None
            ),
            "notes": self.notes,
            "created_at": self.created_at.isoformat() if self.created_at else None,
            "updated_at": self.updated_at.isoformat() if self.updated_at else None,
        }


class ResumeVersion(Base):
    __tablename__ = "resume_versions"

    id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True), primary_key=True, default=uuid.uuid4
    )
    user_id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True), ForeignKey("users.id", ondelete="CASCADE"), index=True
    )
    company_name: Mapped[str] = mapped_column(String(100))
    job_title: Mapped[str] = mapped_column(String(100), index=True)
    source_resume: Mapped[str] = mapped_column(String(200))
    docx_path: Mapped[str | None] = mapped_column(
        String(200), nullable=True, default=None
    )
    pdf_path: Mapped[str | None] = mapped_column(
        String(200), nullable=True, default=None
    )
    ats_score: Mapped[float] = mapped_column(
        Float, nullable=False, default=0.0, server_default="0"
    )
    keywords_added: Mapped[list[str] | None] = mapped_column(
        JSONB, nullable=True, default=None
    )
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), default=ist_now, index=True
    )

    user: Mapped["User"] = relationship("User", back_populates="resume_versions")
    __table_args__ = (
        Index("ix_resume_user_created", "user_id", "created_at"),
        Index("ix_resume_user_company", "user_id", "company_name", "job_title"),
    )

    def to_dict(self) -> dict[str, Any]:
        """Convert resume version to dictionary for API responses."""
        return {
            "id": str(self.id),
            "user_id": str(self.user_id),
            "company_name": self.company_name,
            "job_title": self.job_title,
            "source_resume": self.source_resume,
            "docx_path": self.docx_path,
            "pdf_path": self.pdf_path,
            "ats_score": self.ats_score,
            "keywords_added": self.keywords_added or [],
            "created_at": self.created_at.isoformat() if self.created_at else None,
        }


class JobFormAnswer(Base):
    __tablename__ = "job_form_answers"

    id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True), primary_key=True, default=uuid.uuid4
    )
    user_id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True), ForeignKey("users.id", ondelete="CASCADE"), index=True
    )
    question: Mapped[str] = mapped_column(String(200))
    answer: Mapped[str] = mapped_column(Text)
    answer_encrypted: Mapped[str | None] = mapped_column(Text, nullable=True)
    normalized_question: Mapped[str | None] = mapped_column(String(240), nullable=True)
    field_type: Mapped[str | None] = mapped_column(String(50), nullable=True)
    sensitivity: Mapped[str] = mapped_column(
        String(30), nullable=False, default="standard"
    )
    approved_for_reuse: Mapped[bool] = mapped_column(
        Boolean, nullable=False, default=False
    )
    source_portal: Mapped[str | None] = mapped_column(String(50), nullable=True)
    last_used_at: Mapped[datetime | None] = mapped_column(
        DateTime(timezone=True), nullable=True
    )
    updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), default=ist_now, onupdate=ist_now
    )
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), default=ist_now, index=True
    )

    user: Mapped["User"] = relationship("User", back_populates="job_form_answers")
    __table_args__ = (
        Index("ix_form_answer_user_question", "user_id", "question"),
        Index("ix_form_answer_user_created", "user_id", "created_at"),
    )

    def to_dict(self) -> dict[str, Any]:
        """Convert job form answer to dictionary for API responses."""
        return {
            "id": str(self.id),
            "user_id": str(self.user_id),
            "question": self.question,
            # Protected reusable values are decrypted only inside the matching
            # service and are never exposed by generic model serialization.
            "answer": "" if self.answer_encrypted else self.answer,
            "protected": bool(self.answer_encrypted),
            "normalized_question": self.normalized_question,
            "field_type": self.field_type,
            "sensitivity": self.sensitivity,
            "approved_for_reuse": self.approved_for_reuse,
            "source_portal": self.source_portal,
            "created_at": self.created_at.isoformat() if self.created_at else None,
        }


class AutomationWorkerDevice(Base):
    """Revocable, hashed credential for one narrowly scoped local worker."""

    __tablename__ = "automation_worker_devices"

    id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True), primary_key=True, default=uuid.uuid4
    )
    user_id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True),
        ForeignKey("users.id", ondelete="CASCADE"),
        nullable=False,
        index=True,
    )
    name: Mapped[str] = mapped_column(String(80), nullable=False)
    scope: Mapped[str] = mapped_column(String(80), nullable=False)
    token_digest: Mapped[str] = mapped_column(String(64), nullable=False, unique=True)
    expires_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False, index=True
    )
    revoked_at: Mapped[datetime | None] = mapped_column(
        DateTime(timezone=True), nullable=True
    )
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), default=ist_now
    )

    user: Mapped["User"] = relationship(
        "User", back_populates="automation_worker_devices"
    )
    __table_args__ = (
        CheckConstraint(
            "scope IN ('workday_account_gate', 'workday_application')",
            name="ck_worker_device_scope",
        ),
        Index(
            "ix_worker_device_user_active",
            "user_id",
            "revoked_at",
            "expires_at",
        ),
    )


class ApplicationAutomationBatch(Base):
    """A worker-neutral group of queued application work for one user."""

    __tablename__ = "application_automation_batches"
    id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True), primary_key=True, default=uuid.uuid4
    )
    user_id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True), ForeignKey("users.id", ondelete="CASCADE"), index=True
    )
    worker_kind: Mapped[str] = mapped_column(String(30), nullable=False)
    status: Mapped[str] = mapped_column(
        String(30), nullable=False, default="queued", index=True
    )
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), default=ist_now, index=True
    )
    updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), default=ist_now, onupdate=ist_now
    )

    user: Mapped["User"] = relationship("User", back_populates="automation_batches")
    applications: Mapped[list["JobApplication"]] = relationship(
        "JobApplication", back_populates="automation_batch", lazy="noload"
    )
    __table_args__ = (Index("ix_automation_batch_user_status", "user_id", "status"),)


class ApplicationHold(Base):
    """A safe, user-resolvable blocker for exactly one application."""

    __tablename__ = "application_holds"
    id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True), primary_key=True, default=uuid.uuid4
    )
    user_id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True), ForeignKey("users.id", ondelete="CASCADE"), index=True
    )
    application_id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True),
        ForeignKey("job_applications.id", ondelete="CASCADE"),
        index=True,
    )
    portal: Mapped[str | None] = mapped_column(String(50), nullable=True)
    question: Mapped[str | None] = mapped_column(Text, nullable=True)
    normalized_question: Mapped[str | None] = mapped_column(String(240), nullable=True)
    hold_code: Mapped[str] = mapped_column(String(80), nullable=False)
    remediation: Mapped[str] = mapped_column(Text, nullable=False)
    error_detail: Mapped[str | None] = mapped_column(Text, nullable=True)
    retry_count: Mapped[int] = mapped_column(Integer, nullable=False, default=0)
    status: Mapped[str] = mapped_column(
        String(30), nullable=False, default="open", index=True
    )
    resolved_at: Mapped[datetime | None] = mapped_column(
        DateTime(timezone=True), nullable=True
    )
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), default=ist_now, index=True
    )
    updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), default=ist_now, onupdate=ist_now
    )

    user: Mapped["User"] = relationship("User", back_populates="application_holds")
    application: Mapped["JobApplication"] = relationship(
        "JobApplication", back_populates="holds"
    )
    __table_args__ = (Index("ix_application_hold_user_status", "user_id", "status"),)


class ApplicationAutomationEvent(Base):
    """Append-only safe audit metadata; never includes browser session material."""

    __tablename__ = "application_automation_events"
    id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True), primary_key=True, default=uuid.uuid4
    )
    application_id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True),
        ForeignKey("job_applications.id", ondelete="CASCADE"),
        index=True,
    )
    batch_id: Mapped[uuid.UUID | None] = mapped_column(
        UUID(as_uuid=True),
        ForeignKey("application_automation_batches.id", ondelete="SET NULL"),
        nullable=True,
        index=True,
    )
    event_type: Mapped[str] = mapped_column(String(80), nullable=False)
    detail: Mapped[str | None] = mapped_column(Text, nullable=True)
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), default=ist_now, index=True
    )

    application: Mapped["JobApplication"] = relationship(
        "JobApplication", back_populates="automation_events"
    )


class ApplicationSubmittedAnswer(Base):
    """An exact, application-scoped answer recorded after a portal submission.

    This table deliberately contains no browser-session data.  It is separate
    from ``JobFormAnswer``, which is an answer-library entry that may be reused
    by more than one application.
    """

    __tablename__ = "application_submitted_answers"

    id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True), primary_key=True, default=uuid.uuid4
    )
    application_id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True),
        ForeignKey("job_applications.id", ondelete="CASCADE"),
        index=True,
    )
    question: Mapped[str] = mapped_column(Text, nullable=False)
    answer: Mapped[str] = mapped_column(Text, nullable=False)
    answer_source: Mapped[str] = mapped_column(String(30), nullable=False)
    review_reasons: Mapped[list[str]] = mapped_column(
        JSONB, nullable=False, default=list
    )
    submitted_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), default=ist_now, index=True
    )

    __table_args__ = (
        Index(
            "ix_submitted_answer_application_created", "application_id", "submitted_at"
        ),
    )


class ApplicationDraftAnswer(Base):
    """Latest user-reviewed answer proposal, before portal submission."""

    __tablename__ = "application_draft_answers"

    id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True), primary_key=True, default=uuid.uuid4
    )
    user_id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True), ForeignKey("users.id", ondelete="CASCADE"), index=True
    )
    application_id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True),
        ForeignKey("job_applications.id", ondelete="CASCADE"),
        index=True,
    )
    question: Mapped[str] = mapped_column(Text, nullable=False)
    answer: Mapped[str] = mapped_column(Text, nullable=False)
    answer_source: Mapped[str] = mapped_column(String(30), nullable=False)
    review_reasons: Mapped[list[str]] = mapped_column(
        JSONB, nullable=False, default=list
    )
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), default=ist_now, index=True
    )
    updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), default=ist_now, onupdate=ist_now
    )

    user: Mapped["User"] = relationship(
        "User", back_populates="application_draft_answers"
    )
    application: Mapped["JobApplication"] = relationship(
        "JobApplication", back_populates="draft_answers"
    )
    __table_args__ = (
        UniqueConstraint(
            "application_id", "question", name="uq_application_draft_question"
        ),
        Index("ix_draft_answer_application_updated", "application_id", "updated_at"),
    )


class WorkdayAccountGate(Base):
    """Private user/account/tenant gate for serialized Workday authentication."""

    __tablename__ = "workday_account_gates"

    id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True), primary_key=True, default=uuid.uuid4
    )
    account_ref: Mapped[str] = mapped_column(String(255), nullable=False)
    user_id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True),
        ForeignKey("users.id", ondelete="CASCADE"),
        nullable=False,
    )
    portal_scope: Mapped[str] = mapped_column(String(255), nullable=False)
    state: Mapped[str] = mapped_column(String(32), nullable=False, default="open")
    generation: Mapped[int] = mapped_column(Integer, nullable=False, default=0)
    cooldown_until: Mapped[datetime | None] = mapped_column(
        DateTime(timezone=True), nullable=True
    )
    user_min_until: Mapped[datetime | None] = mapped_column(
        DateTime(timezone=True), nullable=True
    )
    next_eligible_at: Mapped[datetime | None] = mapped_column(
        DateTime(timezone=True), nullable=True
    )
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), default=ist_now
    )
    updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), default=ist_now, onupdate=ist_now
    )

    __table_args__ = (
        UniqueConstraint(
            "user_id",
            "account_ref",
            "portal_scope",
            name="uq_workday_account_gate_identity",
        ),
        CheckConstraint(
            "state IN ('open', 'cooling_down', 'probe_in_progress', "
            "'auth_outcome_pending', 'review_required')",
            name="ck_workday_account_gate_state",
        ),
        CheckConstraint(
            "generation >= 0",
            name="ck_workday_account_gate_generation_nonnegative",
        ),
    )


class WorkdayAuthAttempt(Base):
    """Crash-safe private lease and bounded counters for one auth attempt."""

    __tablename__ = "workday_auth_attempts"

    id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True), primary_key=True, default=uuid.uuid4
    )
    gate_id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True),
        ForeignKey("workday_account_gates.id", ondelete="CASCADE"),
        nullable=False,
    )
    application_id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True),
        ForeignKey("job_applications.id", ondelete="CASCADE"),
        nullable=False,
    )
    generation: Mapped[int] = mapped_column(Integer, nullable=False)
    lease_token: Mapped[str] = mapped_column(String(64), nullable=False)
    lease_expires_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False
    )
    heartbeat_at: Mapped[datetime | None] = mapped_column(
        DateTime(timezone=True), nullable=True
    )
    secret_accessed: Mapped[bool] = mapped_column(
        Boolean, nullable=False, default=False
    )
    auth_submit_count: Mapped[int] = mapped_column(Integer, nullable=False, default=0)
    llm_repair_count: Mapped[int] = mapped_column(Integer, nullable=False, default=0)
    status: Mapped[str] = mapped_column(String(32), nullable=False, default="active")
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), default=ist_now
    )
    updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), default=ist_now, onupdate=ist_now
    )

    __table_args__ = (
        UniqueConstraint(
            "lease_token",
            name="uq_workday_auth_attempt_lease_token",
        ),
        CheckConstraint(
            "generation >= 0",
            name="ck_workday_auth_attempt_generation_nonnegative",
        ),
        CheckConstraint(
            "auth_submit_count BETWEEN 0 AND 1",
            name="ck_workday_auth_attempt_submit_count",
        ),
        CheckConstraint(
            "llm_repair_count BETWEEN 0 AND 1",
            name="ck_workday_auth_attempt_llm_repair_count",
        ),
        CheckConstraint(
            "status IN ('active', 'completed', 'abandoned', 'review_required')",
            name="ck_workday_auth_attempt_status",
        ),
        Index(
            "uq_workday_auth_attempt_one_active_per_gate",
            "gate_id",
            unique=True,
            postgresql_where=text("status = 'active'"),
            sqlite_where=text("status = 'active'"),
        ),
    )


class WorkdayUnit2Attempt(Base):
    """Crash-safe private lease and bounded counters for one Unit 2 attempt."""

    __tablename__ = "workday_unit2_attempts"

    id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True), primary_key=True, default=uuid.uuid4
    )
    application_id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True),
        ForeignKey("job_applications.id", ondelete="CASCADE"),
        nullable=False,
    )
    lease_id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True),
        nullable=False,
    )
    status: Mapped[str] = mapped_column(String(32), nullable=False, default="leased")
    mode: Mapped[str] = mapped_column(String(20), nullable=False, default="normal")
    save_claim_count: Mapped[int] = mapped_column(Integer, nullable=False, default=0)
    lease_expires_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False
    )
    heartbeat_at: Mapped[datetime | None] = mapped_column(
        DateTime(timezone=True), nullable=True
    )
    terminal_at: Mapped[datetime | None] = mapped_column(
        DateTime(timezone=True), nullable=True
    )
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), default=ist_now
    )
    updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), default=ist_now, onupdate=ist_now
    )

    __table_args__ = (
        UniqueConstraint(
            "lease_id",
            name="uq_workday_unit2_attempt_lease_id",
        ),
        CheckConstraint(
            "save_claim_count BETWEEN 0 AND 1",
            name="ck_workday_unit2_attempt_save_claim_count",
        ),
        CheckConstraint(
            "status IN ('leased', 'save_claimed', 'review_required', 'released', 'completed')",
            name="ck_workday_unit2_attempt_status",
        ),
        CheckConstraint(
            "mode IN ('normal', 'observe_only')",
            name="ck_workday_unit2_attempt_mode",
        ),
        Index(
            "uq_workday_unit2_attempt_one_active_per_application",
            "application_id",
            unique=True,
            postgresql_where=text("status IN ('leased', 'save_claimed')"),
            sqlite_where=text("status IN ('leased', 'save_claimed')"),
        ),
    )


class WorkdayCooldownNotice(Base):
    """Private, deduplicated user decision for one account-lock generation."""

    __tablename__ = "workday_cooldown_notices"

    id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True), primary_key=True, default=uuid.uuid4
    )
    gate_id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True),
        ForeignKey("workday_account_gates.id", ondelete="CASCADE"),
        nullable=False,
    )
    application_id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True),
        ForeignKey("job_applications.id", ondelete="CASCADE"),
        nullable=False,
    )
    gate_generation: Mapped[int] = mapped_column(Integer, nullable=False)
    status: Mapped[str] = mapped_column(String(24), nullable=False, default="pending")
    decided_at: Mapped[datetime | None] = mapped_column(
        DateTime(timezone=True), nullable=True
    )
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), default=ist_now
    )
    updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), default=ist_now, onupdate=ist_now
    )

    __table_args__ = (
        UniqueConstraint(
            "gate_id",
            "gate_generation",
            name="uq_workday_cooldown_notice_generation",
        ),
        CheckConstraint(
            "gate_generation >= 0",
            name="ck_workday_cooldown_notice_generation_nonnegative",
        ),
        CheckConstraint(
            "status IN ('pending', 'kept', 'extended', 'deleted')",
            name="ck_workday_cooldown_notice_status",
        ),
        Index(
            "ix_workday_cooldown_notice_application",
            "application_id",
        ),
    )


class WorkdayTransitionFamily(Base):
    """Shared identity and mutable CURRENT pointer for transition recipes."""

    __tablename__ = "workday_transition_families"

    id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True), primary_key=True, default=uuid.uuid4
    )
    visibility: Mapped[str] = mapped_column(
        String(32), nullable=False, default="shared_catalog"
    )
    portal_family: Mapped[str] = mapped_column(String(50), nullable=False)
    tenant_scope: Mapped[str] = mapped_column(String(255), nullable=False)
    task_type: Mapped[str] = mapped_column(String(80), nullable=False)
    from_state_signature: Mapped[str] = mapped_column(String(128), nullable=False)
    action_intent: Mapped[str] = mapped_column(String(80), nullable=False)
    current_version_id: Mapped[uuid.UUID | None] = mapped_column(
        UUID(as_uuid=True), nullable=True
    )
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), default=ist_now
    )
    updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), default=ist_now, onupdate=ist_now
    )

    __table_args__ = (
        CheckConstraint(
            "visibility = 'shared_catalog'",
            name="ck_workday_transition_family_shared_visibility",
        ),
        UniqueConstraint(
            "portal_family",
            "tenant_scope",
            "task_type",
            "from_state_signature",
            "action_intent",
            name="uq_workday_transition_family_identity",
        ),
        ForeignKeyConstraint(
            ["id", "current_version_id"],
            [
                "workday_transition_versions.family_id",
                "workday_transition_versions.id",
            ],
            name="fk_workday_transition_family_current_same_family",
            use_alter=True,
        ),
    )


class WorkdayTransitionVersion(Base):
    """Immutable shared recipe version with same-family lineage."""

    __tablename__ = "workday_transition_versions"

    id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True), primary_key=True, default=uuid.uuid4
    )
    family_id: Mapped[uuid.UUID] = mapped_column(UUID(as_uuid=True), nullable=False)
    parent_version_id: Mapped[uuid.UUID | None] = mapped_column(
        UUID(as_uuid=True), nullable=True
    )
    safe_locator_strategy: Mapped[dict[str, Any]] = mapped_column(JSONB, nullable=False)
    expected_to_state: Mapped[str] = mapped_column(String(64), nullable=False)
    risk_class: Mapped[str] = mapped_column(String(32), nullable=False)
    recipe_version: Mapped[int] = mapped_column(Integer, nullable=False)
    status: Mapped[str] = mapped_column(String(32), nullable=False)
    signature_version: Mapped[int] = mapped_column(Integer, nullable=False)
    executor_policy_version: Mapped[int] = mapped_column(Integer, nullable=False)
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), default=ist_now
    )

    __table_args__ = (
        ForeignKeyConstraint(
            ["family_id"],
            ["workday_transition_families.id"],
            name="fk_workday_transition_version_family",
        ),
        UniqueConstraint(
            "family_id",
            "id",
            name="uq_workday_transition_version_family_id",
        ),
        UniqueConstraint(
            "family_id",
            "recipe_version",
            name="uq_workday_transition_version_recipe",
        ),
        ForeignKeyConstraint(
            ["family_id", "parent_version_id"],
            [
                "workday_transition_versions.family_id",
                "workday_transition_versions.id",
            ],
            name="fk_workday_transition_version_parent_same_family",
        ),
        CheckConstraint(
            "jsonb_typeof(safe_locator_strategy) = 'object'",
            name="ck_workday_transition_version_locator_object",
        ),
        CheckConstraint(
            "risk_class IN ('navigation_only', 'auth_structure')",
            name="ck_workday_transition_version_risk",
        ),
        CheckConstraint(
            "status IN ('verified', 'quarantined', 'retired')",
            name="ck_workday_transition_version_status",
        ),
    )


class PortalSession(Base):
    __tablename__ = "portal_sessions"

    id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True), primary_key=True, default=uuid.uuid4
    )

    user_id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True), ForeignKey("users.id", ondelete="CASCADE")
    )

    portal_name: Mapped[str] = mapped_column(String(50))

    storage_state_path: Mapped[str] = mapped_column(String(500))

    last_login_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), default=ist_now
    )

    expires_at: Mapped[datetime | None] = mapped_column(
        DateTime(timezone=True), nullable=True
    )

    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), default=ist_now
    )

    updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), default=ist_now, onupdate=ist_now
    )

    user: Mapped["User"] = relationship("User", back_populates="portal_sessions")


class UserJobSearchPreference(Base):
    """Per-user job-search rules and explicitly configured public job boards."""

    __tablename__ = "user_job_search_preferences"
    id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True), primary_key=True, default=uuid.uuid4
    )
    user_id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True),
        ForeignKey("users.id", ondelete="CASCADE"),
        unique=True,
        index=True,
    )
    primary_skills: Mapped[list[str]] = mapped_column(
        JSONB, nullable=False, default=list
    )
    secondary_skills: Mapped[list[str]] = mapped_column(
        JSONB, nullable=False, default=list
    )
    roles: Mapped[list[str]] = mapped_column(JSONB, nullable=False, default=list)
    company_tiers: Mapped[list[str]] = mapped_column(
        JSONB, nullable=False, default=list
    )
    sources: Mapped[list[dict[str, Any]]] = mapped_column(
        JSONB, nullable=False, default=list
    )
    min_match_score: Mapped[float] = mapped_column(Float, nullable=False, default=0.65)
    require_review_before_apply: Mapped[bool] = mapped_column(
        Boolean, nullable=False, default=True
    )
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), default=ist_now
    )
    updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), default=ist_now, onupdate=ist_now
    )
    user: Mapped["User"] = relationship("User", back_populates="job_search_preferences")


class JobDiscovery(Base):
    """A deduplicated vacancy discovered from a public ATS board."""

    __tablename__ = "job_discoveries"
    id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True), primary_key=True, default=uuid.uuid4
    )
    user_id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True), ForeignKey("users.id", ondelete="CASCADE"), index=True
    )
    source: Mapped[str] = mapped_column(String(30), nullable=False)
    external_id: Mapped[str] = mapped_column(String(255), nullable=False)
    company_name: Mapped[str] = mapped_column(String(500), nullable=False)
    company_tier: Mapped[str | None] = mapped_column(String(30), nullable=True)
    title: Mapped[str] = mapped_column(String(500), nullable=False)
    location: Mapped[str | None] = mapped_column(String(500), nullable=True)
    job_url: Mapped[str] = mapped_column(Text, nullable=False)
    description: Mapped[str | None] = mapped_column(Text, nullable=True)
    match_score: Mapped[float] = mapped_column(Float, nullable=False, default=0.0)
    match_reasons: Mapped[list[str]] = mapped_column(
        JSONB, nullable=False, default=list
    )
    status: Mapped[str] = mapped_column(
        String(30), nullable=False, default="discovered", index=True
    )
    failure_code: Mapped[str | None] = mapped_column(String(80), nullable=True)
    failure_detail: Mapped[str | None] = mapped_column(Text, nullable=True)
    discovered_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), default=ist_now, index=True
    )
    updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), default=ist_now, onupdate=ist_now
    )
    user: Mapped["User"] = relationship("User", back_populates="job_discoveries")
    __table_args__ = (
        UniqueConstraint(
            "user_id", "source", "external_id", name="uq_user_job_discovery_source_id"
        ),
        Index("ix_discovery_user_status_score", "user_id", "status", "match_score"),
    )


# =============================================================================
# HELPER FUNCTIONS
# =============================================================================


def uuid_to_str(uid: uuid.UUID) -> str | None:
    """Convert UUID to string safely."""
    return str(uid) if uid else None


def str_to_uuid(uid_str: str) -> uuid.UUID | None:
    """Convert string to UUID safely."""
    if not uid_str:
        return None
    try:
        return uuid.UUID(uid_str)
    except (ValueError, TypeError):
        return None
