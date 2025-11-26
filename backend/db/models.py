from sqlalchemy import (
    BigInteger,
    Boolean,
    Column,
    Enum,
    ForeignKey,
    Integer,
    String,
    Text,
    DateTime,
    UniqueConstraint,
    Float,
    CheckConstraint,
)
from sqlalchemy.orm import relationship
import enum
from sqlalchemy.dialects.postgresql import JSON
from .database import Base
from datetime import datetime


class FileCategory(enum.Enum):
    original_upload = "original_upload"
    processed_image = "processed_image"
    json_output = "json_output"
    excel_output = "excel_output"


class StorageType(enum.Enum):
    local = "local"
    s3 = "s3"


class Department(Base):
    __tablename__ = "departments"

    department_id = Column(Integer, primary_key=True)
    department_name = Column(String(255), unique=True, nullable=False)

    users = relationship("User", back_populates="department")
    document_types = relationship(
        "DocumentType",
        secondary="department_doc_type_access",
        back_populates="departments",
    )


class User(Base):
    __tablename__ = "users"

    user_id = Column(Integer, primary_key=True)
    cognito_sub = Column(String(36), unique=True, nullable=False)
    name = Column(String(255), nullable=False)
    email = Column(String(255), unique=True, nullable=False)
    role = Column(String(50), nullable=False, default="user")
    department_id = Column(
        Integer,
        ForeignKey("departments.department_id", ondelete="SET NULL"),
        nullable=True,
    )

    department = relationship("Department", back_populates="users")


class DocumentType(Base):
    __tablename__ = "document_types"

    doc_type_id = Column(Integer, primary_key=True)
    type_name = Column(String(100), nullable=False, unique=True)
    type_code = Column(String(50), nullable=False, unique=True)
    description = Column(Text, nullable=True)
    template_json_path = Column(
        String(500),
        nullable=True,
        comment="S3 key or URL for uploaded template JSON file",
    )
    created_at = Column(DateTime, default=datetime.utcnow)
    updated_at = Column(DateTime, default=datetime.utcnow, onupdate=datetime.utcnow)

    departments = relationship(
        "Department",
        secondary="department_doc_type_access",
        back_populates="document_types",
    )
    configs = relationship("CompanyDocumentConfig", back_populates="document_type")
    jobs = relationship("ProcessingJob", back_populates="document_type")
    batch_jobs = relationship("BatchJob", back_populates="document_type")
    mapping_templates = relationship("MappingTemplate", back_populates="document_type")
    mapping_defaults = relationship("CompanyDocMappingDefault", back_populates="document_type")
    primary_orders = relationship(
        "OcrOrder",
        back_populates="primary_doc_type",
        foreign_keys="OcrOrder.primary_doc_type_id",
    )


class DepartmentDocTypeAccess(Base):
    __tablename__ = "department_doc_type_access"

    department_id = Column(
        Integer,
        ForeignKey("departments.department_id", ondelete="CASCADE"),
        primary_key=True,
    )
    doc_type_id = Column(
        Integer,
        ForeignKey("document_types.doc_type_id", ondelete="CASCADE"),
        primary_key=True,
    )


class Company(Base):
    __tablename__ = "companies"

    company_id = Column(Integer, primary_key=True)
    company_name = Column(String(100), nullable=False, unique=True)
    company_code = Column(String(50), nullable=False, unique=True)
    active = Column(Boolean, default=True)
    created_at = Column(DateTime, default=datetime.utcnow)
    updated_at = Column(DateTime, default=datetime.utcnow, onupdate=datetime.utcnow)

    configs = relationship("CompanyDocumentConfig", back_populates="company")
    jobs = relationship("ProcessingJob", back_populates="company")
    batch_jobs = relationship("BatchJob", back_populates="company")
    mapping_templates = relationship("MappingTemplate", back_populates="company")
    mapping_defaults = relationship("CompanyDocMappingDefault", back_populates="company")


class CompanyDocumentConfig(Base):
    __tablename__ = "company_document_configs"

    config_id = Column(Integer, primary_key=True)
    company_id = Column(Integer, ForeignKey("companies.company_id"), nullable=False)
    doc_type_id = Column(
        Integer, ForeignKey("document_types.doc_type_id"), nullable=False
    )
    prompt_path = Column(String(500), nullable=True, comment='Path to prompt file (supports local paths and S3 URIs like s3://bucket/prompts/...)')
    schema_path = Column(String(500), nullable=True, comment='Path to schema file (supports local paths and S3 URIs like s3://bucket/schemas/...)')
    storage_type = Column(Enum(StorageType), nullable=False, default=StorageType.local, comment='Storage backend type for prompts and schemas')
    storage_metadata = Column(JSON, nullable=True, comment='Additional metadata for storage backend (e.g., S3 bucket info, cache settings)')
    original_prompt_filename = Column(String(255), nullable=True, comment='Original filename of uploaded prompt file (e.g., invoice_prompt.txt)')
    original_schema_filename = Column(String(255), nullable=True, comment='Original filename of uploaded schema file (e.g., invoice_schema.json)')
    # Removed auto-mapping legacy fields: default_mapping_keys, auto_mapping_enabled
    # Removed cross-field mapping (legacy)
    active = Column(Boolean, default=True)
    created_at = Column(DateTime, default=datetime.utcnow)
    updated_at = Column(DateTime, default=datetime.utcnow, onupdate=datetime.utcnow)

    company = relationship("Company", back_populates="configs")
    document_type = relationship("DocumentType", back_populates="configs")

    __table_args__ = (UniqueConstraint("company_id", "doc_type_id"),)


class OrderItemType(enum.Enum):
    SINGLE_SOURCE = "single_source"
    MULTI_SOURCE = "multi_source"

# Persist enum using lowercase .value strings to match stored data and API
ItemTypeEnum = Enum(
    OrderItemType,
    values_callable=lambda obj: [e.value for e in obj],
    native_enum=False,
    name="orderitemtype",
)


class MappingTemplate(Base):
    __tablename__ = "mapping_templates"

    template_id = Column(Integer, primary_key=True)
    template_name = Column(String(255), nullable=False)
    company_id = Column(Integer, ForeignKey("companies.company_id"), nullable=True)
    doc_type_id = Column(Integer, ForeignKey("document_types.doc_type_id"), nullable=True)
    item_type = Column(ItemTypeEnum, nullable=False, default=OrderItemType.SINGLE_SOURCE)
    config = Column(JSON, nullable=False, comment='Serialized mapping configuration (join keys, OneDrive references, column policies)')
    priority = Column(Integer, nullable=False, default=100, comment='Lower number = higher precedence when resolving defaults')
    created_at = Column(DateTime, default=datetime.utcnow)
    updated_at = Column(DateTime, default=datetime.utcnow, onupdate=datetime.utcnow)

    company = relationship("Company", back_populates="mapping_templates")
    document_type = relationship("DocumentType", back_populates="mapping_templates")
    defaults = relationship("CompanyDocMappingDefault", back_populates="template")


class CompanyDocMappingDefault(Base):
    __tablename__ = "company_doc_mapping_defaults"

    default_id = Column(Integer, primary_key=True)
    company_id = Column(Integer, ForeignKey("companies.company_id"), nullable=False)
    doc_type_id = Column(Integer, ForeignKey("document_types.doc_type_id"), nullable=False)
    item_type = Column(ItemTypeEnum, nullable=False, default=OrderItemType.SINGLE_SOURCE)
    template_id = Column(Integer, ForeignKey("mapping_templates.template_id"), nullable=True)
    config_override = Column(JSON, nullable=True, comment='Optional override applied after template resolution')
    created_at = Column(DateTime, default=datetime.utcnow)
    updated_at = Column(DateTime, default=datetime.utcnow, onupdate=datetime.utcnow)

    company = relationship("Company", back_populates="mapping_defaults")
    document_type = relationship("DocumentType", back_populates="mapping_defaults")
    template = relationship("MappingTemplate", back_populates="defaults")

    __table_args__ = (UniqueConstraint("company_id", "doc_type_id", "item_type"),)


class File(Base):
    __tablename__ = "files"

    file_id = Column(Integer, primary_key=True)
    file_name = Column(String(255), nullable=False)
    file_path = Column(String(255), nullable=False, unique=True)
    file_type = Column(String(255), nullable=False)
    file_size = Column(BigInteger, nullable=True)
    mime_type = Column(String(255), nullable=True)
    created_at = Column(DateTime, default=datetime.utcnow)
    s3_bucket = Column(String(255), nullable=True)
    s3_key = Column(String(255), nullable=True)
    source_system = Column(String(50), nullable=True, default='upload', comment='Source system: upload, onedrive, s3')
    source_path = Column(String(500), nullable=True, comment='Original source path for deduplication (e.g., onedrive://{drive_id}/{item_id})')
    source_metadata = Column(JSON, nullable=True, comment='Metadata from source system (e.g., OneDrive item metadata)')

    document_files = relationship("DocumentFile", back_populates="file")


class ProcessingJob(Base):
    __tablename__ = "processing_jobs"

    job_id = Column(Integer, primary_key=True)
    company_id = Column(Integer, ForeignKey("companies.company_id"), nullable=False)
    doc_type_id = Column(
        Integer, ForeignKey("document_types.doc_type_id"), nullable=False
    )
    s3_pdf_path = Column(String(255), nullable=True)
    status = Column(String(20), nullable=False, default="pending")
    original_filename = Column(String(255), nullable=False)
    error_message = Column(Text, nullable=True)
    created_at = Column(DateTime, default=datetime.utcnow)
    updated_at = Column(DateTime, default=datetime.utcnow, onupdate=datetime.utcnow)

    # Add timing fields
    processing_started_at = Column(DateTime, nullable=True)
    processing_completed_at = Column(DateTime, nullable=True)
    processing_time_seconds = Column(Float, nullable=True)

    company = relationship("Company", back_populates="jobs")
    document_type = relationship("DocumentType", back_populates="jobs")
    files = relationship("DocumentFile", back_populates="job")
    api_usages = relationship("ApiUsage", back_populates="job")
    batch_id = Column(Integer, ForeignKey("batch_jobs.batch_id"), nullable=True)
    batch_job = relationship("BatchJob", back_populates="jobs")


class DocumentFile(Base):
    __tablename__ = "document_files"

    document_file_id = Column(Integer, primary_key=True)
    job_id = Column(Integer, ForeignKey("processing_jobs.job_id"), nullable=False)
    file_id = Column(Integer, ForeignKey("files.file_id"), nullable=False)
    file_category = Column(String(50), nullable=False)
    created_at = Column(DateTime, default=datetime.utcnow)

    job = relationship("ProcessingJob", back_populates="files")
    file = relationship("File", back_populates="document_files")

    __table_args__ = (UniqueConstraint("job_id", "file_category"),)


class ApiUsage(Base):
    __tablename__ = "api_usage"

    usage_id = Column(Integer, primary_key=True, index=True)
    # Legacy pipeline linkage (processing_jobs)
    job_id = Column(Integer, ForeignKey("processing_jobs.job_id"), nullable=True)
    # New order pipeline linkage (ocr_order_items)
    item_id = Column(Integer, ForeignKey("ocr_order_items.item_id"), nullable=True)
    input_token_count = Column(Integer, nullable=False)
    output_token_count = Column(Integer, nullable=False)
    api_call_timestamp = Column(DateTime, default=datetime.now, nullable=False)
    model = Column(String(255), nullable=False)

    # Add new fields for timing
    processing_time_seconds = Column(Float, nullable=True)
    # success, error, success_with_fallback, timeout, rate_limited, etc.
    status = Column(String(50), nullable=True)

    __table_args__ = (
        # Ensure at least one linkage (job or order item) is present
        CheckConstraint(
            "job_id IS NOT NULL OR item_id IS NOT NULL",
            name="ck_api_usage_job_or_item",
        ),
    )

    # Relationships
    job = relationship("ProcessingJob", back_populates="api_usages")
    order_item = relationship("OcrOrderItem", back_populates="api_usages")


class UploadType(enum.Enum):
    single_file = "single_file"
    multiple_files = "multiple_files"
    zip_file = "zip_file"
    mixed = "mixed"
    awb_monthly = "awb_monthly"


class BatchJob(Base):
    __tablename__ = "batch_jobs"

    batch_id = Column(Integer, primary_key=True)
    company_id = Column(Integer, ForeignKey("companies.company_id"), nullable=False)
    doc_type_id = Column(
        Integer, ForeignKey("document_types.doc_type_id"), nullable=False
    )
    uploader_user_id = Column(Integer, ForeignKey("users.user_id"), nullable=True)
    
    # Updated for unified batch system
    upload_description = Column(String(255), nullable=True, comment='Description of uploaded files (filename or summary)')
    s3_upload_path = Column(String(255), nullable=True, comment='S3 path to uploaded files')
    upload_type = Column(Enum(UploadType), nullable=False, default=UploadType.single_file, comment='Type of upload batch')
    original_file_names = Column(JSON, nullable=True, comment='Array of original uploaded filenames')
    file_count = Column(Integer, nullable=False, default=0, comment='Total number of files in this batch')
    
    # Keep legacy column for backward compatibility during transition
    original_zipfile = Column(String(255), nullable=True)
    
    total_files = Column(Integer, default=0)
    processed_files = Column(Integer, default=0)
    status = Column(String(20), nullable=False, default="pending")
    error_message = Column(Text, nullable=True)
    json_output_path = Column(String(255), nullable=True)
    excel_output_path = Column(String(255), nullable=True)
    csv_output_path = Column(String(255), nullable=True)
    
    # Cost allocation output files
    netsuite_csv_path = Column(String(255), nullable=True, comment='Path to NetSuite-ready CSV file')
    matching_report_path = Column(String(255), nullable=True, comment='Path to matching details report (Excel)')
    summary_report_path = Column(String(255), nullable=True, comment='Path to cost summary report (Excel)')
    unmatched_count = Column(Integer, nullable=True, default=0, comment='Number of unmatched records in cost allocation')
    
    created_at = Column(DateTime, default=datetime.utcnow)
    updated_at = Column(DateTime, default=datetime.utcnow, onupdate=datetime.utcnow)

    # Relationships
    company = relationship("Company", back_populates="batch_jobs")
    document_type = relationship("DocumentType", back_populates="batch_jobs")
    jobs = relationship("ProcessingJob", back_populates="batch_job")
    uploader = relationship("User", backref="batch_jobs")


# OCR Order System Models

class OrderStatus(enum.Enum):
    DRAFT = "DRAFT"
    PROCESSING = "PROCESSING"
    OCR_COMPLETED = "OCR_COMPLETED"  # NEW: OCR finished, ready for mapping configuration
    MAPPING = "MAPPING"
    COMPLETED = "COMPLETED"
    FAILED = "FAILED"
    LOCKED = "LOCKED"  # NEW: Order is locked, no modifications allowed


class OrderItemStatus(enum.Enum):
    PENDING = "PENDING"
    PROCESSING = "PROCESSING"
    COMPLETED = "COMPLETED"
    FAILED = "FAILED"


class ScheduleMode(enum.Enum):
    INTERVAL = "INTERVAL"
    WINDOWED_INTERVAL = "WINDOWED_INTERVAL"


class ScheduledFileStatus(enum.Enum):
    PENDING = "PENDING"
    PROCESSING = "PROCESSING"
    WAITING_FOR_EXCEL = "WAITING_FOR_EXCEL"
    COMPLETED = "COMPLETED"
    ERROR = "ERROR"


class OcrSchedule(Base):
    __tablename__ = "ocr_schedules"

    schedule_id = Column(Integer, primary_key=True)
    name = Column(String(255), nullable=False)
    enabled = Column(Boolean, nullable=False, default=True)

    # Optional linkage to existing company/doc type configurations
    company_id = Column(Integer, ForeignKey("companies.company_id"), nullable=True)
    doc_type_id = Column(Integer, ForeignKey("document_types.doc_type_id"), nullable=True)

    # OneDrive roots (per schedule)
    material_root_path = Column(
        String(500),
        nullable=False,
        comment="OneDrive root path for material files",
    )
    history_root_path = Column(
        String(500),
        nullable=False,
        comment="OneDrive root path for history/archive files",
    )
    output_root_path = Column(
        String(500),
        nullable=False,
        comment="OneDrive root path for monthly Excel outputs",
    )
    failed_subfolder_name = Column(
        String(255),
        nullable=False,
        default="_Failed",
        comment="Subfolder name for failed files under monthly material folder",
    )

    # Scheduling pattern
    schedule_mode = Column(
        Enum(ScheduleMode),
        nullable=False,
        default=ScheduleMode.INTERVAL,
    )
    start_at = Column(
        DateTime,
        nullable=False,
        comment="UTC timestamp when this schedule becomes active",
    )
    interval_seconds = Column(
        Integer,
        nullable=False,
        comment="Normalized interval between runs in seconds",
    )

    # Original UI fields (kept for transparency and re-editing in admin UI)
    period_unit = Column(
        String(16),
        nullable=True,
        comment="UI hint: second/minute/hour/day",
    )
    period_value = Column(
        Integer,
        nullable=True,
        comment="UI hint: period length in units",
    )
    runs_per_period = Column(
        Integer,
        nullable=True,
        comment="UI hint: how many runs per period",
    )

    # Optional complex window configuration (local business time)
    window_start_time = Column(
        String(8),
        nullable=True,
        comment="Daily window start time HH:MM",
    )
    window_end_time = Column(
        String(8),
        nullable=True,
        comment="Daily window end time HH:MM",
    )
    allowed_weekdays = Column(
        String(32),
        nullable=True,
        comment="Comma-separated weekday numbers (0=Monday .. 6=Sunday)",
    )

    # Safety limiter: maximum number of files to process per scheduler cycle
    max_files_per_cycle = Column(
        Integer,
        nullable=True,
        comment="Max files processed per scheduler cycle (defaults applied in code if null)",
    )

    # Execution bookkeeping
    last_run_at = Column(DateTime, nullable=True)
    next_run_at = Column(DateTime, nullable=True)

    # Audit
    created_by_user_id = Column(
        Integer,
        ForeignKey("users.user_id"),
        nullable=True,
    )
    created_at = Column(DateTime, default=datetime.utcnow)
    updated_at = Column(DateTime, default=datetime.utcnow, onupdate=datetime.utcnow)

    # Relationships
    company = relationship("Company")
    document_type = relationship("DocumentType")
    scheduled_files = relationship(
        "OcrScheduledFile",
        back_populates="schedule",
        cascade="all, delete-orphan",
    )


class OcrScheduledFile(Base):
    __tablename__ = "ocr_scheduled_files"

    id = Column(Integer, primary_key=True)
    schedule_id = Column(
        Integer,
        ForeignKey("ocr_schedules.schedule_id", ondelete="CASCADE"),
        nullable=False,
    )
    month_str = Column(
        String(6),
        nullable=False,
        comment="YearMonth bucket, e.g. 202511",
    )

    # OneDrive path and identity
    onedrive_path = Column(
        String(1000),
        nullable=False,
        comment="Full OneDrive path to the file at time of processing",
    )
    filename = Column(String(500), nullable=False)

    status = Column(
        Enum(ScheduledFileStatus),
        nullable=False,
        default=ScheduledFileStatus.PENDING,
    )
    error_message = Column(Text, nullable=True)
    attempt_count = Column(Integer, nullable=False, default=0)

    # Optional links to OCR/Excel output
    ocr_json_path = Column(
        String(500),
        nullable=True,
        comment="Path (e.g. S3 key) to OCR JSON output",
    )
    output_excel_path = Column(
        String(500),
        nullable=True,
        comment="OneDrive path to monthly Excel file",
    )
    excel_row_index = Column(
        Integer,
        nullable=True,
        comment="Excel row index for this file, if tracked",
    )

    created_at = Column(DateTime, default=datetime.utcnow)
    updated_at = Column(DateTime, default=datetime.utcnow, onupdate=datetime.utcnow)

    # Relationships
    schedule = relationship("OcrSchedule", back_populates="scheduled_files")


class OcrOrder(Base):
    __tablename__ = "ocr_orders"

    order_id = Column(Integer, primary_key=True)
    order_name = Column(String(255), nullable=True, comment='User-friendly name for the order')
    status = Column(Enum(OrderStatus), nullable=False, default=OrderStatus.DRAFT, comment='Current status of the order')
    mapping_file_path = Column(String(500), nullable=True, comment='[Deprecated] Order-level mapping file path - use item-level mapping_config instead')
    mapping_keys = Column(JSON, nullable=True, comment='[Deprecated] Order-level mapping keys - use item-level mapping_config instead')
    final_report_paths = Column(JSON, nullable=True, comment='Paths to final consolidated reports (NetSuite CSV, Excel reports)')
    total_items = Column(Integer, nullable=False, default=0, comment='Total number of order items')
    completed_items = Column(Integer, nullable=False, default=0, comment='Number of completed order items')
    failed_items = Column(Integer, nullable=False, default=0, comment='Number of failed order items')
    error_message = Column(Text, nullable=True, comment='Error message if order processing fails')
    primary_doc_type_id = Column(
        Integer,
        ForeignKey("document_types.doc_type_id", ondelete="SET NULL"),
        nullable=True,
        comment='Primary document type driving template-based special CSV generation',
    )
    created_at = Column(DateTime, default=datetime.utcnow)
    updated_at = Column(DateTime, default=datetime.utcnow, onupdate=datetime.utcnow)

    # Relationships
    items = relationship("OcrOrderItem", back_populates="order", cascade="all, delete-orphan")
    primary_doc_type = relationship(
        "DocumentType",
        back_populates="primary_orders",
        foreign_keys=[primary_doc_type_id],
    )


class OcrOrderItem(Base):
    __tablename__ = "ocr_order_items"

    item_id = Column(Integer, primary_key=True)
    order_id = Column(Integer, ForeignKey("ocr_orders.order_id", ondelete="CASCADE"), nullable=False)
    company_id = Column(Integer, ForeignKey("companies.company_id"), nullable=False)
    doc_type_id = Column(Integer, ForeignKey("document_types.doc_type_id"), nullable=False)
    primary_file_id = Column(Integer, ForeignKey("files.file_id", ondelete="SET NULL"), nullable=True, comment='Primary uploaded file for this item')
    item_name = Column(String(255), nullable=True, comment='User-friendly name for this item')
    status = Column(Enum(OrderItemStatus), nullable=False, default=OrderItemStatus.PENDING)
    item_type = Column(ItemTypeEnum, nullable=False, default=OrderItemType.SINGLE_SOURCE, comment='single_source or multi_source mapping mode')
    applied_template_id = Column(Integer, ForeignKey("mapping_templates.template_id"), nullable=True, comment='Mapping template applied to generate current config')
    file_count = Column(Integer, nullable=False, default=0, comment='Number of attachment files in this item (excludes primary file)')
    ocr_result_json_path = Column(String(500), nullable=True, comment='S3 path to primary file OCR result JSON')
    ocr_result_csv_path = Column(String(500), nullable=True, comment='S3 path to mapped CSV result (primary + attachments)')
    mapping_config = Column(JSON, nullable=True, comment='Per-item mapping configuration (join keys, master CSV path, template references)')
    processing_started_at = Column(DateTime, nullable=True)
    processing_completed_at = Column(DateTime, nullable=True)
    processing_time_seconds = Column(Float, nullable=True)
    error_message = Column(Text, nullable=True)
    created_at = Column(DateTime, default=datetime.utcnow)
    updated_at = Column(DateTime, default=datetime.utcnow, onupdate=datetime.utcnow)

    # Relationships
    order = relationship("OcrOrder", back_populates="items")
    company = relationship("Company")
    document_type = relationship("DocumentType")
    primary_file = relationship("File", foreign_keys=[primary_file_id])
    files = relationship("OrderItemFile", back_populates="order_item", cascade="all, delete-orphan")
    applied_template = relationship("MappingTemplate")
    api_usages = relationship("ApiUsage", back_populates="order_item")


class OrderItemFile(Base):
    __tablename__ = "order_item_files"

    item_id = Column(Integer, ForeignKey("ocr_order_items.item_id", ondelete="CASCADE"), primary_key=True)
    file_id = Column(Integer, ForeignKey("files.file_id", ondelete="CASCADE"), primary_key=True)
    upload_order = Column(Integer, nullable=True, comment='Order in which file was uploaded to this item')
    created_at = Column(DateTime, default=datetime.utcnow)

    # Relationships
    order_item = relationship("OcrOrderItem", back_populates="files")
    file = relationship("File")


# OneDrive Sync Tracking

class OneDriveSync(Base):
    """Track OneDrive sync history and status"""
    __tablename__ = "onedrive_sync"

    sync_id = Column(Integer, primary_key=True)
    last_sync_time = Column(DateTime, nullable=False, comment='Timestamp of last sync')
    sync_status = Column(String(50), nullable=False, comment='Status: success, failed, in_progress')
    files_processed = Column(Integer, default=0, comment='Number of files successfully processed')
    files_failed = Column(Integer, default=0, comment='Number of files that failed')
    error_message = Column(Text, nullable=True, comment='Error message if sync failed')
    sync_metadata = Column(JSON, nullable=True, comment='Additional sync metadata (s3_prefix, folder paths, etc.)')
    created_at = Column(DateTime, default=datetime.utcnow)
