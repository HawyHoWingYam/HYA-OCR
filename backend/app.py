from fastapi import (
    FastAPI,
    UploadFile,
    File,
    HTTPException,
    Form,
    WebSocket,
    Depends,
    BackgroundTasks,
    Query,
)
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import JSONResponse, FileResponse, Response, StreamingResponse
from pydantic import BaseModel
from sqlalchemy.orm import Session
from typing import Any, Dict, List, Optional, Union
import os
import shutil
import tempfile
import io
import zipfile
from datetime import datetime, timedelta
import json
import logging
import asyncio
from sqlalchemy import func
import time

# Optional APScheduler imports with graceful fallback
try:
    from apscheduler.schedulers.background import BackgroundScheduler
    from apscheduler.triggers.cron import CronTrigger
    APSCHEDULER_AVAILABLE = True
except ImportError:
    BackgroundScheduler = None
    CronTrigger = None
    APSCHEDULER_AVAILABLE = False

# 導入配置管理器
from config_loader import config_loader, get_api_key_manager, validate_and_log_config

from db.database import get_db, engine, get_database_info, SessionLocal
from db.models import (
    Base,
    Company,
    DocumentType,
    CompanyDocumentConfig,
    ProcessingJob,
    File as DBFile,
    DocumentFile,
    ApiUsage,
    BatchJob,
    UploadType,
    OcrOrder,
    OcrOrderItem,
    OrderItemFile,
    OrderStatus,
    OrderItemStatus,
    OneDriveSync,
    MappingTemplate,
    CompanyDocMappingDefault,
    OrderItemType,
    OcrSchedule,
    OcrScheduledFile,
    ScheduleMode,
    ScheduledFileStatus,
)
from main import extract_text_from_image, extract_text_from_pdf
from utils.excel_converter import json_to_excel, json_to_csv
from utils.s3_storage import get_s3_manager, is_s3_enabled
from utils.file_storage import get_file_storage
from utils.template_service import (
    build_template_object_name,
    collect_computed_expressions,
    pretty_print_template,
    sanitize_template_version,
    extract_template_version_from_path,
    validate_template_payload,
)
from utils.prompt_schema_manager import get_prompt_schema_manager, load_prompt_and_schema
from utils.company_file_manager import FileType
from utils.force_delete_manager import ForceDeleteManager
from utils.order_processor import (
    start_order_processing,
    start_order_ocr_only_processing,
    start_order_mapping_only_processing,
    escape_excel_formulas,
    OrderProcessor,
)
from utils.mapping_config import (
    MappingItemType,
    normalise_mapping_config,
    normalise_mapping_override,
)
from utils.mapping_config_resolver import MappingConfigResolver
from utils.order_stats import compute_order_attachment_stats

# Cost allocation imports
from cost_allocation.dynamic_mapping_processor import process_dynamic_mapping_file
from cost_allocation.matcher import enrich_ocr_data
from cost_allocation.netsuite_formatter import generate_netsuite_csv
from cost_allocation.report_generator import generate_matching_report, generate_summary_report

# 獲取應用配置
try:
    app_config = config_loader.get_app_config()
    logger = logging.getLogger(__name__)

    # 驗證配置
    validate_and_log_config()

except Exception as e:
    logging.error(f"Failed to load configuration: {e}")
    raise

# Create tables only in development/test environments; rely on Alembic elsewhere
try:
    env = app_config.get("environment", "development") if 'app_config' in globals() else os.getenv("ENVIRONMENT", "development")
    if env in {"development", "test"}:
        if engine:
            Base.metadata.create_all(bind=engine)
            logger.info("✅ Database tables created/verified (dev/test mode)")
        else:
            logger.warning("⚠️  Database engine not initialized, tables not created")
    else:
        logger.info("ℹ️ Skipping Base.metadata.create_all outside dev/test (use Alembic)")
except Exception as e:
    logger.error(f"❌ Failed during table initialization: {e}")

app = FastAPI(title="Document Processing API")

# Request models for migration
class MigrateJobsRequest(BaseModel):
    target_company_id: int

class MigrateDocTypeJobsRequest(BaseModel):
    target_doc_type_id: int

# CORS configuration
app.add_middleware(
    CORSMiddleware,
    allow_origins=["*"],  # For production, replace with specific origins
    allow_credentials=True,
    allow_methods=["*"],
    allow_headers=["*"],
)

# Configure logging
logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s - %(name)s - %(levelname)s - %(message)s",
)

# Optional CloudWatch Logs integration via watchtower
try:
    cw_log_group = os.getenv("CLOUDWATCH_LOG_GROUP")
    if cw_log_group:
        try:
            import watchtower  # type: ignore
            import socket
            import boto3

            region = os.getenv("AWS_DEFAULT_REGION", "ap-southeast-1")
            hostname = os.getenv("HOSTNAME") or socket.gethostname()
            env_name = os.getenv("ENVIRONMENT", "production")
            stream_name = f"{hostname}-{env_name}"

            session = boto3.Session(region_name=region)
            cw_handler = watchtower.CloudWatchLogHandler(
                boto3_session=session,
                log_group=cw_log_group,
                stream_name=stream_name,
                create_log_group=False,
            )
            cw_handler.setLevel(logging.INFO)
            root_logger = logging.getLogger()
            root_logger.addHandler(cw_handler)
            root_logger.info(
                f"✅ CloudWatch logging enabled: group={cw_log_group}, stream={stream_name}, region={region}"
            )
        except Exception as _cw_exc:
            logging.getLogger(__name__).warning(
                f"⚠️ Failed to initialize CloudWatch logging: {_cw_exc}"
            )
except Exception:
    # Never fail app startup due to logging setup
    pass

# WebSocket connections store
active_connections = {}

from utils.ws_notify import (
    register as ws_register,
    unregister as ws_unregister,
    register_summary as ws_register_summary,
    unregister_summary as ws_unregister_summary,
)

@app.websocket("/ws/orders/{order_id}")
async def ws_orders(websocket: WebSocket, order_id: int):
    await websocket.accept()
    try:
        await ws_register(order_id, websocket)
        while True:
            # Keep connection alive; ignore incoming messages
            await websocket.receive_text()
    except Exception:
        pass
    finally:
        try:
            await ws_unregister(order_id, websocket)
        except Exception:
            pass


@app.websocket("/ws/orders/summary")
async def ws_orders_summary(websocket: WebSocket):
    await websocket.accept()
    try:
        await ws_register_summary(websocket)
        while True:
            # Keep connection alive; ignore incoming messages
            await websocket.receive_text()
    except Exception:
        pass
    finally:
        try:
            await ws_unregister_summary(websocket)
        except Exception:
            pass

# Background Scheduler for OneDrive Sync and OCR schedules
# Initialize only if APScheduler is available
scheduler = BackgroundScheduler() if APSCHEDULER_AVAILABLE else None


def run_onedrive_sync():
    """Wrapper for OneDrive sync task"""
    try:
        logger.info("🔄 Running scheduled OneDrive sync...")
        from scripts.onedrive_ingest import run_onedrive_sync as sync_func
        sync_func()
    except Exception as e:
        logger.error(f"❌ Scheduled sync failed: {str(e)}")


def run_ocr_schedules_job():
    """Wrapper for OCR schedule runner task."""
    try:
        logger.info("🔄 Running scheduled OCR schedules...")
        from utils.ocr_schedule_runner import run_ocr_schedules

        run_ocr_schedules()
    except Exception as e:
        logger.error(f"❌ OCR schedule runner failed: {str(e)}")


def _calculate_interval_seconds(period_unit: str, period_value: int, runs_per_period: int) -> int:
    """Normalise period configuration into a base interval in seconds."""
    if runs_per_period <= 0:
        raise ValueError("runs_per_period must be positive")

    unit_map = {
        "second": 1,
        "seconds": 1,
        "minute": 60,
        "minutes": 60,
        "hour": 3600,
        "hours": 3600,
        "day": 86400,
        "days": 86400,
    }
    if period_unit not in unit_map:
        raise ValueError(f"Unsupported period_unit: {period_unit}")

    period_seconds = period_value * unit_map[period_unit]
    if period_seconds <= 0:
        raise ValueError("period_seconds must be positive")

    # Spread runs evenly across the period
    interval = max(1, period_seconds // runs_per_period)
    return interval


def _serialize_ocr_schedule(s: OcrSchedule) -> dict:
    """Serialize OcrSchedule to an API-friendly dict."""
    return {
        "schedule_id": s.schedule_id,
        "name": s.name,
        "enabled": s.enabled,
        "company_id": s.company_id,
        "doc_type_id": s.doc_type_id,
        "material_root_path": s.material_root_path,
        "history_root_path": s.history_root_path,
        "output_root_path": s.output_root_path,
        "failed_subfolder_name": s.failed_subfolder_name,
        "schedule_mode": s.schedule_mode.value if s.schedule_mode else None,
        "start_at": s.start_at.isoformat() if s.start_at else None,
        "interval_seconds": s.interval_seconds,
        "period_unit": s.period_unit,
        "period_value": s.period_value,
        "runs_per_period": s.runs_per_period,
        "window_start_time": s.window_start_time,
        "window_end_time": s.window_end_time,
        "allowed_weekdays": s.allowed_weekdays,
        "max_files_per_cycle": s.max_files_per_cycle,
        "last_run_at": s.last_run_at.isoformat() if s.last_run_at else None,
        "next_run_at": s.next_run_at.isoformat() if s.next_run_at else None,
        "created_by_user_id": s.created_by_user_id,
        "created_at": s.created_at.isoformat() if s.created_at else None,
        "updated_at": s.updated_at.isoformat() if s.updated_at else None,
    }


# Health check endpoint
@app.get("/health")
def health_check():
    """增強的健康檢查端點"""
    health_status = {
        "status": "healthy",
        "timestamp": datetime.utcnow().isoformat(),
        "services": {},
        "config": {},
    }

    # 檢查數據庫連接
    try:
        db_info = get_database_info()
        health_status["services"]["database"] = {
            "status": "healthy" if db_info["status"] == "connected" else "unhealthy",
            "info": db_info,
        }
        if db_info["status"] != "connected":
            health_status["status"] = "degraded"
    except Exception as e:
        health_status["services"]["database"] = {"status": "unhealthy", "error": str(e)}
        health_status["status"] = "unhealthy"

    # 檢查上傳目錄（僅本地存儲）
    try:
        from utils.s3_storage import is_s3_enabled
        if not is_s3_enabled():
            uploads_path = os.getenv("LOCAL_UPLOAD_DIR")
            if uploads_path and os.path.exists(uploads_path) and os.access(uploads_path, os.W_OK):
                health_status["services"]["storage"] = {
                    "status": "healthy",
                    "message": f"Uploads directory accessible: {uploads_path}",
                }
            else:
                health_status["services"]["storage"] = {
                    "status": "unhealthy",
                    "message": "Local storage in use but LOCAL_UPLOAD_DIR is missing or not writable",
                }
                if health_status["status"] == "healthy":
                    health_status["status"] = "degraded"
    except Exception as e:
        health_status["services"]["storage"] = {"status": "unhealthy", "error": str(e)}
        if health_status["status"] == "healthy":
            health_status["status"] = "degraded"

    # 檢查配置狀態
    try:
        api_keys = config_loader.get_gemini_api_keys()
        app_config = config_loader.get_app_config()

        health_status["config"] = {
            "gemini_api_keys": len(api_keys),
            "environment": app_config["environment"],
            "model": app_config["model_name"],
            "api_base_url": app_config["api_base_url"],
        }

        health_status["services"]["configuration"] = {
            "status": "healthy",
            "message": "Configuration loaded successfully",
        }
    except Exception as e:
        health_status["services"]["configuration"] = {
            "status": "unhealthy",
            "error": str(e),
        }
        if health_status["status"] == "healthy":
            health_status["status"] = "degraded"

    # 檢查 S3 存储状态
    try:
        if is_s3_enabled():
            s3_manager = get_s3_manager()
            s3_health = s3_manager.get_health_status()
            health_status["services"]["s3_storage"] = {
                "status": s3_health["status"],
                "info": s3_health,
                "message": f"S3 storage: {s3_health['status']}",
            }
            if (
                s3_health["status"] != "healthy"
                and health_status["status"] == "healthy"
            ):
                health_status["status"] = "degraded"
        else:
            health_status["services"]["s3_storage"] = {
                "status": "disabled",
                "message": "Using local file storage",
            }
            health_status["config"]["storage_type"] = "local"
    except Exception as e:
        health_status["services"]["s3_storage"] = {
            "status": "unhealthy",
            "error": str(e),
        }
        if health_status["status"] == "healthy":
            health_status["status"] = "degraded"

    # 檢查 WebSocket 連接狀態
    try:
        websocket_status = {
            "active_connections": len(active_connections),
            "connection_ids": list(active_connections.keys()),
        }

        health_status["services"]["websocket"] = {
            "status": "healthy",
            "info": websocket_status,
            "message": f"{len(active_connections)} active WebSocket connections",
        }

        health_status["websocket"] = websocket_status

    except Exception as e:
        health_status["services"]["websocket"] = {
            "status": "unhealthy",
            "error": str(e),
        }
        if health_status["status"] == "healthy":
            health_status["status"] = "degraded"

    # 設置適當的 HTTP 狀態碼
    status_code = 200
    if health_status["status"] == "unhealthy":
        status_code = 503
    elif health_status["status"] == "degraded":
        status_code = 200  # 對於降級服務仍返回 200

    return JSONResponse(content=health_status, status_code=status_code)


# OCR Schedules API

@app.get("/ocr-schedules", response_model=List[dict])
def list_ocr_schedules(db: Session = Depends(get_db)):
    schedules = db.query(OcrSchedule).all()
    return [_serialize_ocr_schedule(s) for s in schedules]


@app.post("/ocr-schedules", response_model=dict)
def create_ocr_schedule(schedule_data: dict, db: Session = Depends(get_db)):
    """Create a new OCR schedule.

    Expected payload (minimum):
    - name: str
    - start_date: 'YYYY-MM-DD'
    - start_time: 'HH:MM'
    - period_unit: 'second' | 'minute' | 'hour' | 'day'
    - period_value: int
    - runs_per_period: int
    - material_root_path, history_root_path, output_root_path: OneDrive paths
    - optional: schedule_mode, window_*, allowed_weekdays, max_files_per_cycle
    """
    try:
        name = schedule_data["name"]
        start_date = schedule_data["start_date"]
        start_time = schedule_data["start_time"]
        period_unit = schedule_data["period_unit"]
        period_value = int(schedule_data["period_value"])
        runs_per_period = int(schedule_data["runs_per_period"])
    except KeyError as e:
        raise HTTPException(status_code=400, detail=f"Missing required field: {e.args[0]}")
    except ValueError:
        raise HTTPException(status_code=400, detail="period_value and runs_per_period must be integers")

    try:
        start_at = datetime.fromisoformat(f"{start_date}T{start_time}:00")
    except Exception:
        raise HTTPException(status_code=400, detail="Invalid start_date/start_time format")

    try:
        interval_seconds = _calculate_interval_seconds(period_unit, period_value, runs_per_period)
    except ValueError as e:
        raise HTTPException(status_code=400, detail=str(e))

    schedule_mode_str = schedule_data.get("schedule_mode", "INTERVAL")
    try:
        schedule_mode = ScheduleMode[schedule_mode_str]
    except KeyError:
        raise HTTPException(status_code=400, detail=f"Invalid schedule_mode: {schedule_mode_str}")

    schedule = OcrSchedule(
        name=name,
        enabled=bool(schedule_data.get("enabled", True)),
        company_id=schedule_data.get("company_id"),
        doc_type_id=schedule_data.get("doc_type_id"),
        material_root_path=schedule_data["material_root_path"],
        history_root_path=schedule_data["history_root_path"],
        output_root_path=schedule_data["output_root_path"],
        failed_subfolder_name=schedule_data.get("failed_subfolder_name", "_Failed"),
        schedule_mode=schedule_mode,
        start_at=start_at,
        interval_seconds=interval_seconds,
        period_unit=period_unit,
        period_value=period_value,
        runs_per_period=runs_per_period,
        window_start_time=schedule_data.get("window_start_time"),
        window_end_time=schedule_data.get("window_end_time"),
        allowed_weekdays=schedule_data.get("allowed_weekdays"),
        max_files_per_cycle=schedule_data.get("max_files_per_cycle"),
        created_by_user_id=schedule_data.get("created_by_user_id"),
    )

    # Initial next_run_at is the start_at; scheduler will respect this.
    schedule.next_run_at = start_at

    db.add(schedule)
    db.commit()
    db.refresh(schedule)

    return _serialize_ocr_schedule(schedule)


@app.put("/ocr-schedules/{schedule_id}", response_model=dict)
def update_ocr_schedule(schedule_id: int, schedule_data: dict, db: Session = Depends(get_db)):
    schedule = db.query(OcrSchedule).filter(OcrSchedule.schedule_id == schedule_id).first()
    if not schedule:
        raise HTTPException(status_code=404, detail="OCR schedule not found")

    # Update simple scalar fields if present
    for field in [
        "name",
        "enabled",
        "company_id",
        "doc_type_id",
        "material_root_path",
        "history_root_path",
        "output_root_path",
        "failed_subfolder_name",
        "window_start_time",
        "window_end_time",
        "allowed_weekdays",
        "max_files_per_cycle",
        "created_by_user_id",
    ]:
        if field in schedule_data:
            setattr(schedule, field, schedule_data[field])

    # Optionally update schedule_mode and period configuration
    if "schedule_mode" in schedule_data:
        mode_str = schedule_data["schedule_mode"]
        try:
            schedule.schedule_mode = ScheduleMode[mode_str]
        except KeyError:
            raise HTTPException(status_code=400, detail=f"Invalid schedule_mode: {mode_str}")

    period_unit = schedule_data.get("period_unit", schedule.period_unit)
    period_value = schedule_data.get("period_value", schedule.period_value)
    runs_per_period = schedule_data.get("runs_per_period", schedule.runs_per_period)

    if period_unit and period_value and runs_per_period:
        try:
            period_value_int = int(period_value)
            runs_per_period_int = int(runs_per_period)
            schedule.interval_seconds = _calculate_interval_seconds(
                period_unit, period_value_int, runs_per_period_int
            )
            schedule.period_unit = period_unit
            schedule.period_value = period_value_int
            schedule.runs_per_period = runs_per_period_int
        except ValueError as e:
            raise HTTPException(status_code=400, detail=str(e))

    # Optionally update start_at
    start_date = schedule_data.get("start_date")
    start_time = schedule_data.get("start_time")
    if start_date and start_time:
        try:
            schedule.start_at = datetime.fromisoformat(f"{start_date}T{start_time}:00")
        except Exception:
            raise HTTPException(status_code=400, detail="Invalid start_date/start_time format")

    # Reset next_run_at if requested or if start_at changed
    if schedule_data.get("reset_next_run", False) or (start_date and start_time):
        schedule.next_run_at = schedule.start_at or datetime.utcnow()

    db.commit()
    db.refresh(schedule)
    return _serialize_ocr_schedule(schedule)


@app.post("/ocr-schedules/{schedule_id}/run-now", response_model=dict)
def trigger_ocr_schedule(schedule_id: int, db: Session = Depends(get_db)):
    schedule = db.query(OcrSchedule).filter(OcrSchedule.schedule_id == schedule_id).first()
    if not schedule:
        raise HTTPException(status_code=404, detail="OCR schedule not found")

    now = datetime.utcnow()
    schedule.next_run_at = now
    db.commit()

    return {
        "message": "Schedule marked to run as soon as possible",
        "schedule": _serialize_ocr_schedule(schedule),
    }


@app.get("/ocr-schedules/{schedule_id}/files", response_model=List[dict])
def list_ocr_schedule_files(
    schedule_id: int,
    status: Optional[str] = Query(None),
    db: Session = Depends(get_db),
):
    schedule = db.query(OcrSchedule).filter(OcrSchedule.schedule_id == schedule_id).first()
    if not schedule:
        raise HTTPException(status_code=404, detail="OCR schedule not found")

    query = db.query(OcrScheduledFile).filter(OcrScheduledFile.schedule_id == schedule_id)
    if status:
        try:
            status_enum = ScheduledFileStatus[status]
        except KeyError:
            raise HTTPException(status_code=400, detail=f"Invalid status: {status}")
        query = query.filter(OcrScheduledFile.status == status_enum)

    files = query.order_by(OcrScheduledFile.created_at.desc()).all()

    results = []
    for f in files:
        results.append(
            {
                "id": f.id,
                "schedule_id": f.schedule_id,
                "month_str": f.month_str,
                "onedrive_path": f.onedrive_path,
                "filename": f.filename,
                "status": f.status.value if f.status else None,
                "error_message": f.error_message,
                "attempt_count": f.attempt_count,
                "ocr_json_path": f.ocr_json_path,
                "output_excel_path": f.output_excel_path,
                "excel_row_index": f.excel_row_index,
                "created_at": f.created_at.isoformat() if f.created_at else None,
                "updated_at": f.updated_at.isoformat() if f.updated_at else None,
            }
        )

    return results


# Startup and Shutdown Events for Scheduler
@app.on_event("startup")
async def startup_event():
    """Initialize scheduler on startup"""
    try:
        if not APSCHEDULER_AVAILABLE or scheduler is None:
            logger.warning(
                "⚠️ APScheduler not installed. Background jobs (OneDrive sync / OCR schedules) are disabled. "
                "Install with: pip install -r backend/requirements.txt (use your app's virtualenv pip)"
            )
            return

        # OneDrive daily sync toggle
        onedrive_env = os.getenv("ONEDRIVE_SYNC_ENABLED")
        onedrive_enabled = bool(onedrive_env and onedrive_env.lower() == "true")

        if onedrive_enabled:
            scheduler.add_job(
                run_onedrive_sync,
                CronTrigger(hour=2, minute=0),
                id="onedrive_daily_sync",
                name="OneDrive Daily Sync",
                replace_existing=True,
            )
            logger.info("✅ OneDrive sync scheduled for 2:00 AM daily")
        else:
            logger.info(
                "ℹ️ OneDrive sync disabled (ONEDRIVE_SYNC_ENABLED not set to 'true')"
            )

        # OCR schedule runner toggle
        ocr_env = os.getenv("OCR_SCHEDULE_ENABLED")
        ocr_enabled = bool(ocr_env and ocr_env.lower() == "true")

        if ocr_enabled:
            scheduler.add_job(
                run_ocr_schedules_job,
                # Default: check every 5 minutes for due work
                CronTrigger(minute="*/5"),
                id="ocr_schedules_runner",
                name="OCR Schedules Runner",
                replace_existing=True,
            )
            logger.info(
                "✅ OCR schedule runner enabled - will poll for due schedules every 5 minutes"
            )
        else:
            logger.info(
                "ℹ️ OCR schedule runner disabled (OCR_SCHEDULE_ENABLED not set to 'true')"
            )

        # Start the shared scheduler
        scheduler.start()
        logger.info("✅ APScheduler started")

    except Exception as e:
        logger.error(f"❌ Failed to start scheduler: {str(e)}")


@app.on_event("shutdown")
async def shutdown_event():
    """Clean up scheduler on shutdown"""
    try:
        if APSCHEDULER_AVAILABLE and scheduler is not None and scheduler.running:
            scheduler.shutdown()
            logger.info("✅ APScheduler shut down gracefully")
    except Exception as e:
        logger.error(f"❌ Error shutting down scheduler: {str(e)}")


# Companies API endpoints
@app.get("/companies", response_model=List[dict])
def get_companies(db: Session = Depends(get_db)):
    companies = db.query(Company).all()
    return [
        {
            "company_id": company.company_id,
            "company_name": company.company_name,
            "company_code": company.company_code,
            "active": company.active,
            "created_at": company.created_at.isoformat(),
            "updated_at": company.updated_at.isoformat(),
        }
        for company in companies
    ]


@app.post("/companies", response_model=dict)
def create_company(company_data: dict, db: Session = Depends(get_db)):
    company = Company(
        company_name=company_data["company_name"],
        company_code=company_data["company_code"],
        active=company_data.get("active", True),
    )

    db.add(company)
    db.commit()
    db.refresh(company)

    return {
        "company_id": company.company_id,
        "company_name": company.company_name,
        "company_code": company.company_code,
        "active": company.active,
        "created_at": company.created_at.isoformat(),
        "updated_at": company.updated_at.isoformat(),
    }


@app.get("/companies/{company_id}", response_model=dict)
def get_company(company_id: int, db: Session = Depends(get_db)):
    company = db.query(Company).filter(Company.company_id == company_id).first()
    if not company:
        raise HTTPException(status_code=404, detail="Company not found")

    return {
        "company_id": company.company_id,
        "company_name": company.company_name,
        "company_code": company.company_code,
        "active": company.active,
        "created_at": company.created_at.isoformat(),
        "updated_at": company.updated_at.isoformat(),
    }


@app.put("/companies/{company_id}", response_model=dict)
def update_company(company_id: int, company_data: dict, db: Session = Depends(get_db)):
    company = db.query(Company).filter(Company.company_id == company_id).first()
    if not company:
        raise HTTPException(status_code=404, detail="Company not found")

    company.company_name = company_data["company_name"]
    company.company_code = company_data["company_code"]
    company.active = company_data.get("active", company.active)

    db.commit()
    db.refresh(company)

    return {
        "company_id": company.company_id,
        "company_name": company.company_name,
        "company_code": company.company_code,
        "active": company.active,
        "created_at": company.created_at.isoformat(),
        "updated_at": company.updated_at.isoformat(),
    }


@app.delete("/companies/{company_id}")
def delete_company(company_id: int, db: Session = Depends(get_db)):
    from utils.dependency_checker import check_can_delete_company
    
    # 使用統一的依賴檢查服務
    can_delete, error_message = check_can_delete_company(db, company_id)
    
    if not can_delete:
        if "not found" in error_message:
            raise HTTPException(status_code=404, detail=error_message)
        else:
            raise HTTPException(status_code=400, detail=error_message)
    
    # 獲取公司信息
    company = db.query(Company).filter(Company.company_id == company_id).first()
    
    try:
        db.delete(company)
        db.commit()
        return {"message": f"Company '{company.company_name}' deleted successfully"}
    except Exception as e:
        db.rollback()
        logger.error(f"Error deleting company {company_id}: {str(e)}")
        raise HTTPException(
            status_code=500,
            detail=f"Failed to delete company: {str(e)}"
        )


# Document Types API endpoints
def _serialize_document_type(doc_type: DocumentType) -> dict:
    """Serialize DocumentType model to API-friendly payload."""

    template_path = doc_type.template_json_path
    template_version = extract_template_version_from_path(template_path)

    return {
        "doc_type_id": doc_type.doc_type_id,
        "type_name": doc_type.type_name,
        "type_code": doc_type.type_code,
        "description": doc_type.description,
        "template_json_path": template_path,
        "template_version": template_version,
        "has_template": bool(template_path),
        "created_at": doc_type.created_at.isoformat(),
        "updated_at": doc_type.updated_at.isoformat(),
    }


@app.get("/document-types", response_model=List[dict])
def get_document_types(db: Session = Depends(get_db)):
    doc_types = db.query(DocumentType).all()
    return [_serialize_document_type(doc_type) for doc_type in doc_types]


@app.post("/document-types", response_model=dict)
def create_document_type(doc_type_data: dict, db: Session = Depends(get_db)):
    doc_type = DocumentType(
        type_name=doc_type_data["type_name"],
        type_code=doc_type_data["type_code"],
        description=doc_type_data.get("description", ""),
    )

    db.add(doc_type)
    db.commit()
    db.refresh(doc_type)

    return _serialize_document_type(doc_type)


@app.get("/document-types/{doc_type_id}", response_model=dict)
def get_document_type(doc_type_id: int, db: Session = Depends(get_db)):
    doc_type = (
        db.query(DocumentType).filter(DocumentType.doc_type_id == doc_type_id).first()
    )
    if not doc_type:
        raise HTTPException(status_code=404, detail="Document type not found")

    return _serialize_document_type(doc_type)


@app.put("/document-types/{doc_type_id}", response_model=dict)
def update_document_type(
    doc_type_id: int, doc_type_data: dict, db: Session = Depends(get_db)
):
    doc_type = (
        db.query(DocumentType).filter(DocumentType.doc_type_id == doc_type_id).first()
    )
    if not doc_type:
        raise HTTPException(status_code=404, detail="Document type not found")

    doc_type.type_name = doc_type_data["type_name"]
    doc_type.type_code = doc_type_data["type_code"]
    doc_type.description = doc_type_data.get("description", doc_type.description)

    db.commit()
    db.refresh(doc_type)

    return {
        "doc_type_id": doc_type.doc_type_id,
        "type_name": doc_type.type_name,
        "type_code": doc_type.type_code,
        "description": doc_type.description,
        "created_at": doc_type.created_at.isoformat(),
        "updated_at": doc_type.updated_at.isoformat(),
    }


@app.delete("/document-types/{doc_type_id}")
def delete_document_type(doc_type_id: int, db: Session = Depends(get_db)):
    from utils.dependency_checker import check_can_delete_document_type
    
    # 使用統一的依賴檢查服務
    can_delete, error_message = check_can_delete_document_type(db, doc_type_id)
    
    if not can_delete:
        if "not found" in error_message:
            raise HTTPException(status_code=404, detail=error_message)
        else:
            raise HTTPException(status_code=400, detail=error_message)
    
    # 獲取文檔類型信息
    doc_type = db.query(DocumentType).filter(DocumentType.doc_type_id == doc_type_id).first()
    
    try:
        db.delete(doc_type)
        db.commit()
        return {"message": f"Document type '{doc_type.type_name}' deleted successfully"}
    except Exception as e:
        db.rollback()
        logger.error(f"Error deleting document type {doc_type_id}: {str(e)}")
        raise HTTPException(
            status_code=500,
            detail=f"Failed to delete document type: {str(e)}"
        )


@app.post("/document-types/{doc_type_id}/template", response_model=dict)
async def upload_document_type_template(
    doc_type_id: int,
    template_file: UploadFile = File(...),
    db: Session = Depends(get_db),
):
    """Upload and store a template.json for a document type."""

    doc_type = db.query(DocumentType).filter(DocumentType.doc_type_id == doc_type_id).first()
    if not doc_type:
        raise HTTPException(status_code=404, detail="Document type not found")

    use_s3 = is_s3_enabled()

    try:
        raw_bytes = await template_file.read()
    except Exception as exc:
        logger.error(
            "Failed to read uploaded template file for doc_type %s: %s",
            doc_type_id,
            exc,
        )
        raise HTTPException(status_code=400, detail="Failed to read uploaded file") from exc

    if not raw_bytes:
        raise HTTPException(status_code=400, detail="Uploaded template file is empty")

    try:
        template_text = raw_bytes.decode("utf-8")
        template_json = json.loads(template_text)
    except UnicodeDecodeError as exc:
        logger.warning(
            "Template upload decoding failed for doc_type %s: %s",
            doc_type_id,
            exc,
        )
        raise HTTPException(status_code=400, detail="Template must be valid UTF-8 JSON") from exc
    except json.JSONDecodeError as exc:
        logger.warning(
            "Template upload JSON parsing failed for doc_type %s: %s",
            doc_type_id,
            exc,
        )
        raise HTTPException(status_code=400, detail="Template file is not valid JSON") from exc

    try:
        validate_template_payload(template_json)
    except ValueError as exc:
        logger.warning(
            "Template validation failed for doc_type %s: %s | payload=%s",
            doc_type_id,
            exc,
            pretty_print_template(template_json),
        )
        raise HTTPException(status_code=400, detail=str(exc)) from exc

    computed_expressions = collect_computed_expressions(template_json["column_definitions"])
    logger.info(
        "Validated template upload for doc_type %s: version=%s, computed_columns=%d",
        doc_type_id,
        template_json.get("version", "unspecified"),
        len(computed_expressions),
    )

    safe_version = sanitize_template_version(template_json.get("version", "latest"))

    if use_s3:
        s3_manager = get_s3_manager()
        if not s3_manager:
            raise HTTPException(status_code=500, detail="S3 storage manager is not configured")

        object_key = build_template_object_name(doc_type_id, safe_version)

        metadata = {
            "doc_type_id": str(doc_type_id),
            "template_name": str(template_json.get("template_name", ""))[:50],
            "template_version": safe_version,
        }

        logger.info(
            "Uploading template for doc_type %s to s3://%s/%s",
            doc_type_id,
            s3_manager.bucket_name,
            f"{s3_manager.upload_prefix}{object_key}",
        )

        try:
            upload_success = s3_manager.upload_file(
                file_content=raw_bytes,
                key=object_key,
                content_type="application/json",
                metadata=metadata,
            )
        except Exception as exc:
            logger.error(
                "Unexpected error uploading template for doc_type %s: %s",
                doc_type_id,
                exc,
            )
            raise HTTPException(status_code=500, detail="Failed to upload template to S3") from exc

        if not upload_success:
            raise HTTPException(status_code=500, detail="Failed to upload template to S3")

        template_uri = f"s3://{s3_manager.bucket_name}/{s3_manager.upload_prefix}{object_key}"
    else:
        base_dir = os.getenv("LOCAL_UPLOAD_DIR")
        if not base_dir:
            raise HTTPException(status_code=500, detail="LOCAL_UPLOAD_DIR not set for local storage")
        template_dir = os.path.join(base_dir, "templates", "document_types", str(doc_type_id))
        os.makedirs(template_dir, exist_ok=True)
        template_filename = f"template_{safe_version}.json"
        template_path = os.path.join(template_dir, template_filename)
        try:
            with open(template_path, "w", encoding="utf-8") as f:
                json.dump(template_json, f, ensure_ascii=False, indent=2)
            template_uri = template_path
            logger.info("Template for doc_type %s saved locally at %s", doc_type_id, template_uri)
        except Exception as exc:
            logger.error("Failed to save local template for doc_type %s: %s", doc_type_id, exc)
            raise HTTPException(status_code=500, detail="Failed to save template to local storage") from exc
    previous_path = doc_type.template_json_path

    try:
        doc_type.template_json_path = template_uri
        doc_type.updated_at = datetime.utcnow()
        db.commit()
    except Exception as exc:
        db.rollback()
        logger.error(
            "Failed to persist template path for doc_type %s: %s",
            doc_type_id,
            exc,
        )
        raise HTTPException(status_code=500, detail="Failed to save template metadata") from exc

    logger.info(
        "Template uploaded for doc_type %s. Stored at %s (previous=%s)",
        doc_type_id,
        template_uri,
        previous_path,
    )

    return {
        "message": "Template uploaded successfully",
        "template_path": template_uri,
        "previous_template_path": previous_path,
        "computed_columns": list(computed_expressions.keys()),
    }


@app.get("/document-types/{doc_type_id}/template")
def download_document_type_template(doc_type_id: int, db: Session = Depends(get_db)):
    """Download the stored template.json for a document type."""

    doc_type = db.query(DocumentType).filter(DocumentType.doc_type_id == doc_type_id).first()
    if not doc_type:
        raise HTTPException(status_code=404, detail="Document type not found")

    if not doc_type.template_json_path:
        raise HTTPException(status_code=404, detail="Document type has no template configured")

    file_storage = get_file_storage()
    template_content = file_storage.download_file(doc_type.template_json_path)

    if not template_content:
        logger.error(
            "Template download failed for doc_type %s: file not found at %s",
            doc_type_id,
            doc_type.template_json_path,
        )
        raise HTTPException(status_code=500, detail="Failed to download template file")

    file_name = doc_type.template_json_path.split("/")[-1] or f"doc_type_{doc_type_id}_template.json"
    logger.info(
        "Template downloaded for doc_type %s from %s",
        doc_type_id,
        doc_type.template_json_path,
    )

    return StreamingResponse(
        io.BytesIO(template_content),
        media_type="application/json",
        headers={
            "Content-Disposition": f"attachment; filename={file_name}",
        },
    )


@app.delete("/document-types/{doc_type_id}/template", response_model=dict)
def delete_document_type_template(doc_type_id: int, db: Session = Depends(get_db)):
    """Delete the configured template.json for a document type."""

    doc_type = db.query(DocumentType).filter(DocumentType.doc_type_id == doc_type_id).first()
    if not doc_type:
        raise HTTPException(status_code=404, detail="Document type not found")

    if not doc_type.template_json_path:
        raise HTTPException(status_code=404, detail="Document type has no template configured")

    template_path = doc_type.template_json_path
    file_storage = get_file_storage()

    deletion_success = file_storage.delete_file(template_path)
    logger.info(
        "Template deletion for doc_type %s requested. Path=%s deleted=%s",
        doc_type_id,
        template_path,
        deletion_success,
    )

    try:
        doc_type.template_json_path = None
        doc_type.updated_at = datetime.utcnow()
        db.commit()
    except Exception as exc:
        db.rollback()
        logger.error(
            "Failed to clear template metadata for doc_type %s: %s",
            doc_type_id,
            exc,
        )
        raise HTTPException(status_code=500, detail="Failed to update document type") from exc

    return {
        "message": "Template removed successfully",
        "template_path": template_path,
        "deleted": deletion_success,
    }


# Dependency Management API endpoints
@app.get("/companies/{company_id}/dependencies")
def get_company_dependencies(company_id: int, db: Session = Depends(get_db)):
    """獲取公司的詳細依賴關係信息"""
    from utils.dependency_checker import DependencyChecker
    
    checker = DependencyChecker(db)
    dependencies = checker.check_company_dependencies(company_id)
    
    if not dependencies["exists"]:
        raise HTTPException(status_code=404, detail=dependencies["error"])
    
    # 如果有依賴，獲取詳細信息和遷移建議
    if not dependencies["can_delete"]:
        dependencies["detailed_dependencies"] = checker.get_detailed_dependencies("company", company_id)
        dependencies["migration_targets"] = checker.suggest_migration_targets("company", company_id)
    
    return dependencies


@app.get("/document-types/{doc_type_id}/dependencies")
def get_document_type_dependencies(doc_type_id: int, db: Session = Depends(get_db)):
    """獲取文檔類型的詳細依賴關係信息"""
    from utils.dependency_checker import DependencyChecker
    
    checker = DependencyChecker(db)
    dependencies = checker.check_document_type_dependencies(doc_type_id)
    
    if not dependencies["exists"]:
        raise HTTPException(status_code=404, detail=dependencies["error"])
    
    # 如果有依賴，獲取詳細信息和遷移建議
    if not dependencies["can_delete"]:
        dependencies["detailed_dependencies"] = checker.get_detailed_dependencies("document_type", doc_type_id)
        dependencies["migration_targets"] = checker.suggest_migration_targets("document_type", doc_type_id)
    
    return dependencies


@app.post("/companies/{company_id}/migrate-jobs")
def migrate_company_jobs(
    company_id: int, 
    request: MigrateJobsRequest,
    db: Session = Depends(get_db)
):
    """將公司的處理作業遷移到另一個公司"""
    from utils.dependency_checker import DependencyChecker
    
    # 驗證源公司和目標公司存在
    source_company = db.query(Company).filter(Company.company_id == company_id).first()
    target_company = db.query(Company).filter(Company.company_id == request.target_company_id).first()
    
    if not source_company:
        raise HTTPException(status_code=404, detail="Source company not found")
    if not target_company:
        raise HTTPException(status_code=404, detail="Target company not found")
    
    try:
        # 遷移 processing jobs
        processing_jobs_updated = (
            db.query(ProcessingJob)
            .filter(ProcessingJob.company_id == company_id)
            .update({"company_id": request.target_company_id})
        )
        
        # 遷移 batch jobs
        batch_jobs_updated = (
            db.query(BatchJob)
            .filter(BatchJob.company_id == company_id)
            .update({"company_id": request.target_company_id})
        )
        
        db.commit()
        
        return {
            "message": f"Successfully migrated jobs from '{source_company.company_name}' to '{target_company.company_name}'",
            "processing_jobs_migrated": processing_jobs_updated,
            "batch_jobs_migrated": batch_jobs_updated
        }
        
    except Exception as e:
        db.rollback()
        logger.error(f"Error migrating company jobs: {str(e)}")
        raise HTTPException(
            status_code=500,
            detail=f"Failed to migrate jobs: {str(e)}"
        )


@app.post("/document-types/{doc_type_id}/migrate-jobs")
def migrate_document_type_jobs(
    doc_type_id: int, 
    request: MigrateDocTypeJobsRequest,
    db: Session = Depends(get_db)
):
    """將文檔類型的處理作業遷移到另一個文檔類型"""
    from utils.dependency_checker import DependencyChecker
    
    # 驗證源文檔類型和目標文檔類型存在
    source_doc_type = db.query(DocumentType).filter(DocumentType.doc_type_id == doc_type_id).first()
    target_doc_type = db.query(DocumentType).filter(DocumentType.doc_type_id == request.target_doc_type_id).first()
    
    if not source_doc_type:
        raise HTTPException(status_code=404, detail="Source document type not found")
    if not target_doc_type:
        raise HTTPException(status_code=404, detail="Target document type not found")
    
    try:
        # 遷移 processing jobs
        processing_jobs_updated = (
            db.query(ProcessingJob)
            .filter(ProcessingJob.doc_type_id == doc_type_id)
            .update({"doc_type_id": request.target_doc_type_id})
        )
        
        # 遷移 batch jobs
        batch_jobs_updated = (
            db.query(BatchJob)
            .filter(BatchJob.doc_type_id == doc_type_id)
            .update({"doc_type_id": request.target_doc_type_id})
        )
        
        db.commit()
        
        return {
            "message": f"Successfully migrated jobs from '{source_doc_type.type_name}' to '{target_doc_type.type_name}'",
            "processing_jobs_migrated": processing_jobs_updated,
            "batch_jobs_migrated": batch_jobs_updated
        }
        
    except Exception as e:
        db.rollback()
        logger.error(f"Error migrating document type jobs: {str(e)}")
        raise HTTPException(
            status_code=500,
            detail=f"Failed to migrate jobs: {str(e)}"
        )


# Configuration API endpoints
@app.get("/configs", response_model=List[dict])
def get_configurations(db: Session = Depends(get_db)):
    configs = db.query(CompanyDocumentConfig).all()
    return [
        {
            "config_id": config.config_id,
            "company_id": config.company_id,
            "company_name": config.company.company_name if config.company else None,
            "doc_type_id": config.doc_type_id,
            "type_name": (
                config.document_type.type_name if config.document_type else None
            ),
            "prompt_path": config.prompt_path,
            "schema_path": config.schema_path,
            "active": config.active,
            "created_at": config.created_at.isoformat(),
            "updated_at": config.updated_at.isoformat(),
        }
        for config in configs
    ]


@app.post("/configs", response_model=dict)
def create_configuration(config_data: dict, db: Session = Depends(get_db)):
    # Check if company and document type exist
    company = (
        db.query(Company)
        .filter(Company.company_id == config_data["company_id"])
        .first()
    )
    if not company:
        raise HTTPException(status_code=404, detail="Company not found")

    doc_type = (
        db.query(DocumentType)
        .filter(DocumentType.doc_type_id == config_data["doc_type_id"])
        .first()
    )
    if not doc_type:
        raise HTTPException(status_code=404, detail="Document type not found")

    # Check if config already exists
    existing_config = (
        db.query(CompanyDocumentConfig)
        .filter(
            CompanyDocumentConfig.company_id == config_data["company_id"],
            CompanyDocumentConfig.doc_type_id == config_data["doc_type_id"],
        )
        .first()
    )

    if existing_config:
        raise HTTPException(
            status_code=400,
            detail="Configuration already exists for this company and document type",
        )

    config = CompanyDocumentConfig(
        company_id=config_data["company_id"],
        doc_type_id=config_data["doc_type_id"],
        prompt_path=config_data.get("prompt_path"),  # Allow None for new configs
        schema_path=config_data.get("schema_path"),  # Allow None for new configs
        original_prompt_filename=config_data.get("original_prompt_filename"),
        original_schema_filename=config_data.get("original_schema_filename"),
        active=config_data.get("active", True),
    )

    db.add(config)
    db.commit()
    db.refresh(config)

    return {
        "config_id": config.config_id,
        "company_id": config.company_id,
        "company_name": config.company.company_name,
        "doc_type_id": config.doc_type_id,
        "type_name": config.document_type.type_name,
        "prompt_path": config.prompt_path,
        "schema_path": config.schema_path,
        "active": config.active,
        "created_at": config.created_at.isoformat(),
        "updated_at": config.updated_at.isoformat(),
    }


@app.get("/configs/{config_id}", response_model=dict)
def get_configuration(config_id: int, db: Session = Depends(get_db)):
    config = (
        db.query(CompanyDocumentConfig)
        .filter(CompanyDocumentConfig.config_id == config_id)
        .first()
    )
    if not config:
        raise HTTPException(status_code=404, detail="Configuration not found")

    return {
        "config_id": config.config_id,
        "company_id": config.company_id,
        "company_name": config.company.company_name,
        "doc_type_id": config.doc_type_id,
        "type_name": config.document_type.type_name,
        "prompt_path": config.prompt_path,
        "schema_path": config.schema_path,
        "active": config.active,
        "created_at": config.created_at.isoformat(),
        "updated_at": config.updated_at.isoformat(),
    }


@app.put("/configs/{config_id}", response_model=dict)
def update_configuration(
    config_id: int, config_data: dict, db: Session = Depends(get_db)
):
    config = (
        db.query(CompanyDocumentConfig)
        .filter(CompanyDocumentConfig.config_id == config_id)
        .first()
    )
    if not config:
        raise HTTPException(status_code=404, detail="Configuration not found")

    # Update only provided fields (support partial updates)
    if "prompt_path" in config_data:
        config.prompt_path = config_data["prompt_path"]
    if "schema_path" in config_data:
        config.schema_path = config_data["schema_path"]
    if "original_prompt_filename" in config_data:
        config.original_prompt_filename = config_data["original_prompt_filename"]
    if "original_schema_filename" in config_data:
        config.original_schema_filename = config_data["original_schema_filename"]
    if "active" in config_data:
        config.active = config_data["active"]

    db.commit()
    db.refresh(config)

    return {
        "config_id": config.config_id,
        "company_id": config.company_id,
        "company_name": config.company.company_name,
        "doc_type_id": config.doc_type_id,
        "type_name": config.document_type.type_name,
        "prompt_path": config.prompt_path,
        "schema_path": config.schema_path,
        "active": config.active,
        "created_at": config.created_at.isoformat(),
        "updated_at": config.updated_at.isoformat(),
    }


@app.delete("/configs/{config_id}")
def delete_configuration(config_id: int, db: Session = Depends(get_db)):
    config = (
        db.query(CompanyDocumentConfig)
        .filter(CompanyDocumentConfig.config_id == config_id)
        .first()
    )
    if not config:
        raise HTTPException(status_code=404, detail="Configuration not found")

    db.delete(config)
    db.commit()

    return {"message": "Configuration deleted successfully"}


# Configuration file download endpoint (S3-only)
@app.get("/configs/{config_id}/download/{file_type}")
def download_config_file(config_id: int, file_type: str, db: Session = Depends(get_db)):
    """
    Download prompt or schema file for a configuration (S3-only)
    
    Args:
        config_id: Configuration ID
        file_type: "prompt" or "schema"
    """
    # Validate file_type parameter
    if file_type not in ["prompt", "schema"]:
        raise HTTPException(status_code=400, detail="file_type must be 'prompt' or 'schema'")
    
    # Get configuration from database
    config = (
        db.query(CompanyDocumentConfig)
        .filter(CompanyDocumentConfig.config_id == config_id)
        .first()
    )
    if not config:
        raise HTTPException(status_code=404, detail="Configuration not found")
    
    # Get company and document type for S3 path construction
    company = config.company
    doc_type = config.document_type
    if not company or not doc_type:
        raise HTTPException(status_code=500, detail="Configuration missing company or document type")
    
    logger.info(f"📥 S3-only download request - Config ID: {config_id}, Type: {file_type}, Company: {company.company_code}, DocType: {doc_type.type_code}")
    compat_enabled = os.getenv('S3_READ_COMPAT_ENABLED', 'true').lower() == 'true'
    
    try:
        # Always try S3 download first
        s3_manager = get_s3_manager()
        if not s3_manager:
            raise HTTPException(status_code=500, detail="S3 storage not available - S3 is required for downloads")
        
        # Extract filename from stored path or use default
        stored_path = config.prompt_path if file_type == "prompt" else config.schema_path
        if stored_path and "/" in stored_path:
            filename = stored_path.split("/")[-1]
        else:
            # Default filename based on type
            filename = f"{file_type}.{'txt' if file_type == 'prompt' else 'json'}"
        
        # 🚀 SMART DYNAMIC FILE DISCOVERY - Multiple strategies for resilient file finding
        file_content = None
        successful_path = None
        
        # === PRIORITY STRATEGY: STORED DATABASE PATH (highest priority) ===
        logger.info("🎯 Trying STORED database path first")
        try:
            if stored_path:
                logger.info(f"📍 Found stored path in database: {stored_path}")
                
                # Use the new S3 manager method for direct path download
                file_content = s3_manager.download_file_by_stored_path(stored_path)
                
                if file_content is not None:
                    successful_path = stored_path
                    
                    # Get filename from path
                    if "/" in stored_path:
                        filename = stored_path.split("/")[-1]
                    
                    logger.info(f"✅ Downloaded using STORED database path: {stored_path}")
                    logger.info(f"📂 Download filename: {filename}")
                else:
                    logger.info(f"⚠️ Could not download from stored path: {stored_path}")
            else:
                logger.info("⚠️ No stored path in database, trying other strategies...")
                
        except Exception as e:
            logger.warning(f"⚠️ STORED path download failed: {e}")
        
        # === STRATEGY 0: CLEAN PATH STRUCTURE (highest priority fallback) ===
        if file_content is None:
            logger.info("🎯 Trying CLEAN path structure with database-stored filename first")
            try:
                # Get original filename from database first
                original_filename = None
                if file_type == "prompt" and config.original_prompt_filename:
                    original_filename = config.original_prompt_filename
                elif file_type == "schema" and config.original_schema_filename:
                    original_filename = config.original_schema_filename
                
                # Try with original filename from database
                if original_filename:
                    logger.info(f"📁 Using original filename from database: {original_filename}")
                    
                    # Construct clean S3 path: companies/{company_id}/{type}/{doc_type_id}/{config_id}/filename
                    clean_s3_path = f"companies/{company.company_id}/{'prompts' if file_type == 'prompt' else 'schemas'}/{doc_type.doc_type_id}/{config_id}/{original_filename}"
                    
                    # Try to download directly using clean path
                    if file_type == "prompt":
                        file_content = s3_manager.download_prompt_by_id(
                            company_id=company.company_id,
                            doc_type_id=doc_type.doc_type_id,
                            config_id=config_id,
                            filename=original_filename
                        )
                    else:
                        schema_data = s3_manager.download_schema_by_id(
                            company_id=company.company_id,
                            doc_type_id=doc_type.doc_type_id,
                            config_id=config_id,
                            filename=original_filename
                        )
                        if schema_data:
                            file_content = json.dumps(schema_data, indent=2, ensure_ascii=False).encode('utf-8')
                    
                    if file_content is not None:
                        successful_path = clean_s3_path
                        filename = original_filename  # Use original filename for download
                        logger.info(f"✅ Found using CLEAN path structure: {successful_path}")
                        logger.info(f"📂 Download filename: {filename}")
                    else:
                        logger.info("⚠️ CLEAN path not found, trying fallback strategies...")
                else:
                    logger.info("⚠️ No original filename in database, trying fallback strategies...")
                    
            except Exception as e:
                logger.info(f"⚠️ CLEAN path download failed: {e}")
        
        # === STRATEGY 1: CONFIG-SPECIFIC paths (most unique) ===
        config_specific_paths = [
            f"config_{config_id}",  # config_6, config_5 (unique per config)
            f"company_{company.company_id}/doctype_{doc_type.doc_type_id}/config_{config_id}",  # company_1/doctype_11/config_6
            f"c{company.company_id}/d{doc_type.doc_type_id}/cfg{config_id}",  # c1/d11/cfg6
        ]
        
        # === STRATEGY 2: ID-based paths (stable) ===
        id_based_paths = [
            f"company_{company.company_id}/doctype_{doc_type.doc_type_id}",  # company_1/doctype_11
            f"c{company.company_id}/d{doc_type.doc_type_id}",  # c1/d11 (shorter)
            f"{company.company_id}_{doc_type.doc_type_id}",    # 1_11 (minimal)
        ]
        
        # === STRATEGY 3: Current name-based paths (compat only) ===
        name_based_paths = []
        if compat_enabled:
            # Company variants
            company_variants = [
                company.company_code if company.company_code else "unknown",
                company.company_code.lower() if company.company_code else "unknown",
                company.company_code.upper() if company.company_code else "unknown",
                company.company_name.lower().replace(" ", "_") if company.company_name else "unknown",
                "hana",  # Common fallback
            ]

            # Document type variants
            doc_type_variants = [
                doc_type.type_code if doc_type.type_code else "unknown",
                doc_type.type_name if doc_type.type_name else "unknown",
                # Handle common transformations
                doc_type.type_code.replace("[Admin]", "[Finance]") if doc_type.type_code and "[Admin]" in doc_type.type_code else None,
                doc_type.type_code.replace("[Finance]", "[Admin]") if doc_type.type_code and "[Finance]" in doc_type.type_code else None,
                doc_type.type_code.replace("[Production]", "[Admin]") if doc_type.type_code and "[Production]" in doc_type.type_code else None,
                # Remove prefixes and clean up
                doc_type.type_code.replace("[Admin]_", "").replace("[Finance]_", "").replace("[Production]_", "") if doc_type.type_code else None,
                # Common patterns
                "[Finance]_hkbn_billing",  # Known working pattern
                "hkbn_billing", "admin_hkbn_billing", "finance_hkbn_billing",
            ]

            # Remove None and duplicates
            doc_type_variants = list(set([v for v in doc_type_variants if v]))

            # Build all name-based combinations
            for company_variant in set(company_variants):
                for doc_type_variant in doc_type_variants:
                    name_based_paths.append(f"{company_variant}/{doc_type_variant}")
        
        # === STRATEGY 4: Enhanced wildcard search in S3 with disambiguation ===
        def try_wildcard_search():
            if not compat_enabled:
                return None, None
            logger.info(f"🔍 Attempting enhanced wildcard search for config_id={config_id}")
            # List all files and find matches by filename pattern
            all_prompts = s3_manager.list_prompts() if file_type == "prompt" else []
            all_schemas = s3_manager.list_schemas() if file_type == "schema" else []
            all_files = all_prompts if file_type == "prompt" else all_schemas
            
            # Enhanced search terms with priority scoring
            high_priority_terms = [
                f"config_{config_id}",  # Highest priority: exact config match
                f"cfg{config_id}",
                str(config_id),
            ]
            
            medium_priority_terms = [
                f"{company.company_id}_{doc_type.doc_type_id}",
                company.company_code.lower() if company.company_code else "",
                doc_type.type_code.lower() if doc_type.type_code else "",
            ]
            
            low_priority_terms = [
                filename.replace('.txt', '').replace('.json', ''),
                "hkbn", "billing",
                company.company_name.lower() if company.company_name else "",
                doc_type.type_name.lower().replace("[", "").replace("]", "") if doc_type.type_name else ""
            ]
            
            # Find files with scoring system
            scored_matches = []
            
            for file_info in all_files:
                file_key = file_info['key'].lower()
                score = 0
                
                # High priority matches (config-specific)
                for term in high_priority_terms:
                    if term and term.lower() in file_key:
                        score += 100
                        logger.info(f"🎯 HIGH PRIORITY match: {term} in {file_info['key']}")
                
                # Medium priority matches 
                for term in medium_priority_terms:
                    if term and term.lower() in file_key:
                        score += 10
                        logger.info(f"🔍 MEDIUM PRIORITY match: {term} in {file_info['key']}")
                
                # Low priority matches
                for term in low_priority_terms:
                    if term and term.lower() in file_key:
                        score += 1
                        logger.info(f"🔍 LOW PRIORITY match: {term} in {file_info['key']}")
                
                if score > 0:
                    scored_matches.append((score, file_info))
            
            # Sort by score (highest first) and return best match
            if scored_matches:
                scored_matches.sort(key=lambda x: x[0], reverse=True)
                best_score, best_file = scored_matches[0]
                logger.info(f"🏆 Best match with score {best_score}: {best_file['key']}")
                
                # Extract company and doctype from the found path
                path_parts = best_file['key'].split('/')
                if len(path_parts) >= 2:
                    found_company, found_doctype = path_parts[0], path_parts[1]
                    found_filename = path_parts[-1]
                    
                    if file_type == "prompt":
                        return s3_manager.download_prompt_raw(found_company, found_doctype, found_filename), f"{found_company}/{found_doctype}/{found_filename}"
                    else:
                        return s3_manager.download_schema_raw(found_company, found_doctype, found_filename), f"{found_company}/{found_doctype}/{found_filename}"
            
            logger.warning(f"❌ No matches found via wildcard search for config_id={config_id}")
            return None, None
        
        # === EXECUTE SEARCH STRATEGIES ===
        all_paths_to_try = config_specific_paths + id_based_paths + name_based_paths
        
        # Try all path combinations
        for path in all_paths_to_try:
            if file_content is not None:
                break
                
            if '/' in path:
                company_part, doc_type_part = path.split('/', 1)
                logger.info(f"🔍 Trying path: {path}/{filename}")
                
                if file_type == "prompt":
                    file_content = s3_manager.download_prompt_raw(company_part, doc_type_part, filename)
                else:
                    file_content = s3_manager.download_schema_raw(company_part, doc_type_part, filename)
                
                if file_content is not None:
                    successful_path = f"{path}/{filename}"
                    logger.info(f"✅ Found file at: {successful_path}")
                    break
        
        # Try alternative filenames if primary filename fails - with unique identifiers
        if file_content is None and compat_enabled:
            alternative_filenames = [
                # Config-specific filenames (most unique)
                f"config_{config_id}_{file_type}.{'txt' if file_type == 'prompt' else 'json'}",  # config_6_prompt.txt
                f"cfg{config_id}_{file_type}.{'txt' if file_type == 'prompt' else 'json'}",      # cfg6_prompt.txt
                f"{config_id}_{file_type}.{'txt' if file_type == 'prompt' else 'json'}",        # 6_prompt.txt
                
                # Company+DocType+Config combinations
                f"{company.company_id}_{doc_type.doc_type_id}_{config_id}_{file_type}.{'txt' if file_type == 'prompt' else 'json'}",  # 2_3_6_prompt.txt
                f"c{company.company_id}_d{doc_type.doc_type_id}_cfg{config_id}_{file_type}.{'txt' if file_type == 'prompt' else 'json'}",  # c2_d3_cfg6_prompt.txt
                
                # Timestamp-based alternatives
                f"{filename.split('.')[0]}_config_{config_id}.{'txt' if file_type == 'prompt' else 'json'}",  # invoice_prompt_config_6.txt
                f"{config_id}_{filename}",  # 6_invoice_prompt.txt
                
                # Original alternatives
                "prompt.txt" if file_type == "prompt" else "schema.json",
                f"{company.company_id}_{doc_type.doc_type_id}_{file_type}.{'txt' if file_type == 'prompt' else 'json'}",
                filename.replace(" ", "_").replace(".", "_").replace("_txt", ".txt").replace("_json", ".json"),
            ]
            
            for alt_filename in alternative_filenames:
                if file_content is not None:
                    break
                    
                for path in all_paths_to_try:
                    if file_content is not None:
                        break
                    
                    if '/' in path:
                        company_part, doc_type_part = path.split('/', 1)
                        logger.info(f"🔍 Trying alternative: {path}/{alt_filename}")
                        
                        if file_type == "prompt":
                            file_content = s3_manager.download_prompt_raw(company_part, doc_type_part, alt_filename)
                        else:
                            file_content = s3_manager.download_schema_raw(company_part, doc_type_part, alt_filename)
                        
                        if file_content is not None:
                            filename = alt_filename
                            successful_path = f"{path}/{alt_filename}"
                            logger.info(f"✅ Found file with alternative name: {successful_path}")
                            break
        
        # Last resort: wildcard search
        if file_content is None and compat_enabled:
            logger.info("🔍 Attempting wildcard search as last resort")
            file_content, successful_path = try_wildcard_search()
            if successful_path:
                filename = successful_path.split('/')[-1]
        
        if file_content is None:
            raise HTTPException(
                status_code=404, 
                detail=f"{file_type.title()} file not found in S3 for {company.company_code}/{doc_type.type_code}"
            )
        
        # PRIORITY 1: Try to get original filename from database (most reliable)
        original_filename = filename  # fallback to current filename
        try:
            if file_type == "prompt" and config.original_prompt_filename:
                original_filename = config.original_prompt_filename
                logger.info(f"📁 Using original filename from database: {original_filename}")
            elif file_type == "schema" and config.original_schema_filename:
                original_filename = config.original_schema_filename
                logger.info(f"📁 Using original filename from database: {original_filename}")
            else:
                logger.info(f"⚠️ No original filename in database for {file_type}, trying S3 metadata")
        except Exception as e:
            logger.warning(f"⚠️ Failed to get original filename from database: {e}")
        
        # PRIORITY 2: Try to get original filename from S3 metadata (backup method)
        # Only try S3 metadata if database didn't provide original filename
        if (original_filename == filename and successful_path):
            try:
                # Normalise successful_path into a pure S3 key (no folder prefixing mistakes)
                key_only = successful_path
                if key_only.startswith('s3://'):
                    parts = key_only[5:].split('/', 1)
                    key_only = parts[1] if len(parts) == 2 else key_only

                # When the key is ID-based (companies/...), query head_object directly
                if key_only.startswith('companies/'):
                    head = s3_manager.s3_client.head_object(Bucket=s3_manager.bucket_name, Key=key_only)
                    metadata = head.get('Metadata', {})
                else:
                    # For legacy foldered keys (prompts/, schemas/, upload/, results/ ...)
                    # get_file_info expects a key relative to the folder argument.
                    folder_type = 'prompts' if file_type == 'prompt' else 'schemas'
                    rel_key = key_only
                    for prefix in (f'{folder_type}/', 'upload/', 'uploads/', 'results/', 'exports/'):
                        if rel_key.startswith(prefix):
                            rel_key = rel_key[len(prefix):]
                            break
                    info = s3_manager.get_file_info(rel_key, folder=folder_type)
                    metadata = info.get('metadata', {}) if info else {}

                if metadata and 'original_filename' in metadata:
                    original_filename = metadata['original_filename']
                    logger.info(f"📁 Retrieved original filename from S3 metadata: {original_filename}")
                else:
                    logger.info("⚠️ No original_filename in S3 metadata, using stored filename")
            except Exception as e:
                logger.warning(f"⚠️ Failed to retrieve original filename from S3 metadata: {e}")
        
        # Create temporary file for FileResponse
        with tempfile.NamedTemporaryFile(delete=False, suffix=f"_{original_filename}") as temp_file:
            # Ensure file_content is bytes for writing
            if isinstance(file_content, str):
                temp_file.write(file_content.encode('utf-8'))
            else:
                temp_file.write(file_content)
            temp_file_path = temp_file.name
        
        # Determine content type
        content_type = "text/plain" if file_type == "prompt" else "application/json"
        
        logger.info(f"✅ S3 file download successful: {company.company_code}/{doc_type.type_code}/{original_filename}")
        return FileResponse(
            path=temp_file_path,
            filename=original_filename,
            media_type=content_type
        )
    
    except HTTPException:
        raise
    except Exception as e:
        logger.error(f"❌ S3-only config file download failed: {e}")
        raise HTTPException(status_code=500, detail=f"Failed to download {file_type} file from S3: {str(e)}")


# File upload endpoint
@app.post("/upload", response_model=dict)
async def upload_file(file: UploadFile = File(...), path: str = Form(...)):
    
    try:
        # Check if this is a prompt or schema file for config upload
        path_parts = path.split('/')
        
        # NEW ID-BASED FORMAT: document_type/{doc_type_id}/{company_id}/prompt|schema/{filename}
        if (len(path_parts) >= 5 and 
            path_parts[0] == "document_type" and 
            path_parts[3] in ["prompt", "schema"]):
            
            # Parse IDs from path
            try:
                doc_type_id = int(path_parts[1])  # Now expecting ID, not code
                company_id = int(path_parts[2])   # Now expecting ID, not code  
            except ValueError:
                # Fallback to legacy name-based format for compatibility
                doc_type_code = path_parts[1]
                company_code = path_parts[2]
                
                # Convert codes to IDs by querying database
                db = next(get_db())
                try:
                    company = db.query(Company).filter(Company.company_code == company_code).first()
                    doc_type = db.query(DocumentType).filter(DocumentType.type_code == doc_type_code).first()
                    
                    if not company or not doc_type:
                        raise HTTPException(status_code=404, detail="Company or document type not found")
                    
                    company_id = company.company_id
                    doc_type_id = doc_type.doc_type_id
                finally:
                    db.close()
            
            file_type = path_parts[3]  # "prompt" or "schema"
            filename = path_parts[4]
            
            logger.info(f"Uploading {file_type} file using ID-based path: company_id={company_id}, doc_type_id={doc_type_id}, filename={filename}")
            
            # Parse config_id from filename if it follows the new format
            config_id = None
            if filename.startswith('config_'):
                try:
                    config_id = int(filename.split('_')[1])
                except (IndexError, ValueError):
                    logger.warning(f"Could not parse config_id from filename: {filename}")

            use_s3 = is_s3_enabled()

            # Read file content once
            file_content = await file.read()
            original_filename = file.filename if hasattr(file, 'filename') else filename
            stored_path = None

            if file_type == "prompt":
                # For prompt files, decode to text
                content_text = file_content.decode('utf-8')
                
                if use_s3:
                    # S3 上传
                    s3_manager = get_s3_manager()
                    if not s3_manager:
                        raise HTTPException(status_code=500, detail="S3 storage not available")

                    upload_metadata = {
                        "original_filename": original_filename,
                        "upload_source": "admin_config"
                    }
                    
                    if config_id:
                        # Use ID-based method with config_id and original filename
                        s3_path = s3_manager.upload_prompt_by_id(
                            company_id=company_id,
                            doc_type_id=doc_type_id,
                            config_id=config_id,
                            prompt_content=content_text,
                            filename=original_filename,
                            metadata=upload_metadata
                        )
                    else:
                        # Use generic company file method with original filename
                        s3_path = s3_manager.upload_company_file(
                            company_id=company_id,
                            file_type=FileType.PROMPT,
                            content=content_text,
                            filename=original_filename,
                            doc_type_id=doc_type_id,
                            metadata=upload_metadata
                        )
                        
                    if not s3_path:
                        raise HTTPException(status_code=500, detail="Failed to upload prompt to S3")

                    stored_path = f"s3://{s3_manager.bucket_name}/{s3_path}"
                else:
                    # 本地存储
                    base_dir = os.getenv("LOCAL_UPLOAD_DIR")
                    if not base_dir:
                        raise HTTPException(status_code=500, detail="LOCAL_UPLOAD_DIR not set for local storage")
                    local_dir = os.path.join(base_dir, "configs", str(company_id), str(doc_type_id), "prompts")
                    os.makedirs(local_dir, exist_ok=True)
                    local_path = os.path.join(local_dir, original_filename)
                    with open(local_path, "w", encoding="utf-8") as f:
                        f.write(content_text)
                    stored_path = local_path
                    logger.info(f"Prompt config saved locally at {stored_path}")
                
            else:  # schema
                # For schema files, parse JSON
                content_text = file_content.decode('utf-8')
                try:
                    schema_data = json.loads(content_text)
                except json.JSONDecodeError as e:
                    raise HTTPException(status_code=400, detail=f"Invalid JSON format: {str(e)}")
                
                if use_s3:
                    s3_manager = get_s3_manager()
                    if not s3_manager:
                        raise HTTPException(status_code=500, detail="S3 storage not available")

                    upload_metadata = {
                        "original_filename": original_filename,
                        "upload_source": "admin_config"
                    }
                    
                    if config_id:
                        # Use ID-based method with config_id and original filename
                        s3_path = s3_manager.upload_schema_by_id(
                            company_id=company_id,
                            doc_type_id=doc_type_id,
                            config_id=config_id,
                            schema_data=schema_data,
                            filename=original_filename,
                            metadata=upload_metadata
                        )
                    else:
                        # Use generic company file method with original filename
                        s3_path = s3_manager.upload_company_file(
                            company_id=company_id,
                            file_type=FileType.SCHEMA,
                            content=content_text,
                            filename=original_filename,
                            doc_type_id=doc_type_id,
                            metadata=upload_metadata
                        )
                    
                    if not s3_path:
                        raise HTTPException(status_code=500, detail="Failed to upload schema to S3")

                    stored_path = f"s3://{s3_manager.bucket_name}/{s3_path}"
                else:
                    # 本地存储
                    base_dir = os.getenv("LOCAL_UPLOAD_DIR")
                    if not base_dir:
                        raise HTTPException(status_code=500, detail="LOCAL_UPLOAD_DIR not set for local storage")
                    local_dir = os.path.join(base_dir, "configs", str(company_id), str(doc_type_id), "schemas")
                    os.makedirs(local_dir, exist_ok=True)
                    local_path = os.path.join(local_dir, original_filename)
                    with open(local_path, "w", encoding="utf-8") as f:
                        json.dump(schema_data, f, ensure_ascii=False, indent=2)
                    stored_path = local_path
                    logger.info(f"Schema config saved locally at {stored_path}")
            
            logger.info(f"✅ Successfully uploaded {file_type} config using backend={'s3' if use_s3 else 'local'}: {stored_path}")
            
            # Auto-update configuration with file path if config_id exists
            if config_id and stored_path:
                try:
                    db = next(get_db())
                    try:
                        config = db.query(CompanyDocumentConfig).filter(
                            CompanyDocumentConfig.config_id == config_id
                        ).first()
                        
                        if config:
                            original_filename_for_db = original_filename
                            
                            if file_type == "prompt":
                                config.prompt_path = stored_path
                                config.original_prompt_filename = original_filename_for_db
                                logger.info(f"📝 Updated config {config_id} with prompt_path: {stored_path}")
                            else:  # schema
                                config.schema_path = stored_path
                                config.original_schema_filename = original_filename_for_db
                                logger.info(f"📝 Updated config {config_id} with schema_path: {stored_path}")
                            
                            db.commit()
                        else:
                            logger.warning(f"⚠️ Config {config_id} not found for path update")
                    finally:
                        db.close()
                except Exception as e:
                    logger.error(f"❌ Failed to update config {config_id} with file path: {e}")
                    # Don't fail the upload if config update fails
            
            return {"file_path": stored_path}
        
        else:
            # For other file types, always use local storage (explicit path from env)
            logger.info(f"Uploading non-prompt/schema file to local storage: {path}")
            
            base_dir = os.getenv("LOCAL_UPLOAD_DIR")
            if not base_dir:
                raise HTTPException(status_code=500, detail="LOCAL_UPLOAD_DIR not set for local storage")
            directory = os.path.join(base_dir, os.path.dirname(path))
            os.makedirs(directory, exist_ok=True)

            file_path = os.path.join(base_dir, path)

            with open(file_path, "wb") as buffer:
                shutil.copyfileobj(file.file, buffer)

            return {"file_path": file_path}

    except json.JSONDecodeError as e:
        logger.error(f"Error parsing schema JSON: {str(e)}")
        raise HTTPException(status_code=400, detail=f"Invalid JSON format in schema file: {str(e)}")
    except UnicodeDecodeError as e:
        logger.error(f"Error decoding file content: {str(e)}")
        raise HTTPException(status_code=400, detail=f"File encoding error - ensure UTF-8 encoding: {str(e)}")
    except Exception as e:
        logger.error(f"Error uploading file: {str(e)}")
        raise HTTPException(status_code=500, detail=f"Error uploading file: {str(e)}")


# Get job status endpoint
@app.get("/jobs/{job_id}", response_model=dict)
def get_job_status(job_id: int, db: Session = Depends(get_db)):
    logger.warning(f"[DEPRECATED_API] /jobs/{{job_id}} is deprecated and scheduled for removal. job_id={job_id}")
    job = db.query(ProcessingJob).filter(ProcessingJob.job_id == job_id).first()
    if not job:
        raise HTTPException(status_code=404, detail="Job not found")

    # Get associated files
    files_query = (
        db.query(DocumentFile, DBFile)
        .join(DBFile, DocumentFile.file_id == DBFile.file_id)
        .filter(DocumentFile.job_id == job_id)
    )

    files = []
    for doc_file, file in files_query:
        # Calculate file size if not already stored
        file_size = file.file_size
        if file_size is None and os.path.exists(file.file_path):
            file_size = os.path.getsize(file.file_path)
            # Update the file size in the database
            file.file_size = file_size
            db.commit()

        files.append(
            {
                "file_id": file.file_id,
                "file_name": file.file_name,
                "file_path": file.file_path,
                "file_category": doc_file.file_category,
                "file_size": file_size or 0,  # Default to 0 if size can't be determined
                "file_type": file.file_type
                or "application/octet-stream",  # Default content type
                "created_at": file.created_at.isoformat(),
            }
        )

    return {
        "job_id": job.job_id,
        "company_id": job.company_id,
        "company_name": job.company.company_name if job.company else None,
        "doc_type_id": job.doc_type_id,
        "type_name": job.document_type.type_name if job.document_type else None,
        "status": job.status,
        "original_filename": job.original_filename,
        "error_message": job.error_message,
        "created_at": job.created_at.isoformat(),
        "updated_at": job.updated_at.isoformat(),
        "files": files,
    }


# List jobs endpoint
@app.get("/jobs", response_model=List[dict])
async def list_jobs(
    company_id: Optional[int] = None,
    doc_type_id: Optional[int] = None,
    status: Optional[str] = None,
    limit: int = Query(20, ge=1, le=100),
    offset: int = Query(0, ge=0),
    db: Session = Depends(get_db),
):
    logger.warning("[DEPRECATED_API] /jobs is deprecated and scheduled for removal.")

    # Create a regular function (not async) to run in the executor
    def get_jobs():
        # Create a new session to avoid sharing with busy sessions
        with Session(engine) as session:
            query = session.query(ProcessingJob)

            if company_id is not None:
                query = query.filter(ProcessingJob.company_id == company_id)

            if doc_type_id is not None:
                query = query.filter(ProcessingJob.doc_type_id == doc_type_id)

            if status is not None:
                query = query.filter(ProcessingJob.status == status)

            # Order by most recent first
            query = query.order_by(ProcessingJob.created_at.desc())

            # Apply pagination
            jobs = query.offset(offset).limit(limit).all()

            # Convert to dict before leaving the function to avoid session issues
            result = []
            for job in jobs:
                result.append(
                    {
                        "job_id": job.job_id,
                        "company_id": job.company_id,
                        "company_name": job.company.company_name
                        if job.company
                        else None,
                        "doc_type_id": job.doc_type_id,
                        "type_name": job.document_type.type_name
                        if job.document_type
                        else None,
                        "status": job.status,
                        "original_filename": job.original_filename,
                        "created_at": job.created_at.isoformat(),
                        "updated_at": job.updated_at.isoformat(),
                    }
                )
            return result

    # Run the database query in a separate thread using ThreadPoolExecutor
    import asyncio
    from concurrent.futures import ThreadPoolExecutor

    executor = ThreadPoolExecutor()
    result = await asyncio.get_event_loop().run_in_executor(executor, get_jobs)

    return result


# Get file download endpoint
@app.get("/files/{file_id}")
def get_file(file_id: int, db: Session = Depends(get_db)):
    file = db.query(DBFile).filter(DBFile.file_id == file_id).first()
    if not file:
        raise HTTPException(status_code=404, detail="File not found")

    if not os.path.exists(file.file_path):
        raise HTTPException(status_code=404, detail="File not found on disk")

    return {
        "file_path": file.file_path,
        "file_name": file.file_name,
        "file_type": file.file_type,
    }


# Add this endpoint
@app.get("/download/{file_id}")
def download_file(file_id: int, db: Session = Depends(get_db)):
    file = db.query(DBFile).filter(DBFile.file_id == file_id).first()
    if not file:
        raise HTTPException(status_code=404, detail="File not found")

    logger.info(f"📥 下载请求 - 文件ID: {file_id}, 路径: {file.file_path}")

    # Check if file is stored in S3
    if file.file_path.startswith("s3://"):
        logger.info(f"📥 从S3下载文件: {file.file_path}")
        s3_manager = get_s3_manager()

        if not s3_manager:
            raise HTTPException(status_code=500, detail="S3存储不可用")

        try:
            # Parse S3 path to extract bucket and key
            # Format: s3://bucket-name/folder/path/to/file
            s3_parts = file.file_path.replace("s3://", "").split("/", 1)
            if len(s3_parts) != 2:
                raise HTTPException(status_code=500, detail="无效的S3文件路径")

            bucket_name, full_key = s3_parts

            # Determine folder from the full key
            if full_key.startswith("results/"):
                key = full_key[8:]  # Remove "results/" prefix
                file_content = s3_manager.get_json_result(key)
                if file_content is not None:
                    # Convert dict back to JSON string for download
                    json_str = json.dumps(file_content, ensure_ascii=False, indent=2)
                    file_bytes = json_str.encode("utf-8")
                else:
                    raise HTTPException(status_code=404, detail="S3中未找到文件")
            elif full_key.startswith("exports/"):
                key = full_key[8:]  # Remove "exports/" prefix
                file_bytes = s3_manager.get_excel_export(key)
                if file_bytes is None:
                    raise HTTPException(status_code=404, detail="S3中未找到文件")
            elif full_key.startswith("upload/"):
                key = full_key[7:]  # Remove "upload/" prefix
                file_bytes = s3_manager.download_file(key, folder="upload")
                if file_bytes is None:
                    raise HTTPException(status_code=404, detail="S3中未找到文件")
            else:
                raise HTTPException(status_code=500, detail="无法识别的S3文件夹路径")

            # Create temporary file for FileResponse
            with tempfile.NamedTemporaryFile(
                delete=False, suffix=f"_{file.file_name}"
            ) as temp_file:
                temp_file.write(file_bytes)
                temp_file_path = temp_file.name

            logger.info(f"✅ S3文件下载成功: {file.file_name}")

            # Create custom FileResponse that cleans up temp file
            class CleanupFileResponse(FileResponse):
                def __init__(self, *args, temp_path=None, **kwargs):
                    super().__init__(*args, **kwargs)
                    self.temp_path = temp_path

                async def __call__(self, scope, receive, send):
                    try:
                        await super().__call__(scope, receive, send)
                    finally:
                        if self.temp_path and os.path.exists(self.temp_path):
                            try:
                                os.unlink(self.temp_path)
                                logger.info(f"🧹 清理临时文件: {self.temp_path}")
                            except Exception as e:
                                logger.warning(f"⚠️ 清理临时文件失败: {e}")

            return CleanupFileResponse(
                path=temp_file_path,
                filename=file.file_name,
                media_type=file.file_type or "application/octet-stream",
                temp_path=temp_file_path,
            )

        except Exception as e:
            logger.error(f"❌ S3文件下载失败: {str(e)}")
            raise HTTPException(status_code=500, detail=f"S3文件下载失败: {str(e)}")

    else:
        # Local file handling
        logger.info(f"📥 从本地下载文件: {file.file_path}")
        if not os.path.exists(file.file_path):
            raise HTTPException(status_code=404, detail="本地文件未找到")

        return FileResponse(
            path=file.file_path,
            filename=file.file_name,
            media_type=file.file_type or "application/octet-stream",
        )


# Prompt and Schema Management API endpoints
@app.get("/health/prompts-schemas", response_model=dict)
async def get_prompt_schema_health():
    """Get prompt and schema management system health status"""
    try:
        prompt_schema_manager = get_prompt_schema_manager()
        health = prompt_schema_manager.get_health_status()
        return health
    except Exception as e:
        return {
            "status": "unhealthy",
            "error": str(e),
            "timestamp": datetime.now().isoformat()
        }


@app.get("/prompts-schemas/templates", response_model=dict)
async def list_templates(company_code: Optional[str] = None):
    """List available prompt and schema templates"""
    try:
        prompt_schema_manager = get_prompt_schema_manager()
        templates = await prompt_schema_manager.list_available_templates(company_code)
        return {
            "status": "success",
            "data": templates,
            "timestamp": datetime.now().isoformat()
        }
    except Exception as e:
        raise HTTPException(status_code=500, detail=f"Failed to list templates: {e}")


@app.get("/prompts-schemas/{company_code}/{doc_type_code}/prompt", response_model=dict)
async def get_prompt(company_code: str, doc_type_code: str, filename: str = "prompt.txt"):
    """Get prompt content for a specific company and document type"""
    try:
        prompt_schema_manager = get_prompt_schema_manager()
        prompt_content = await prompt_schema_manager.get_prompt(company_code, doc_type_code, filename)
        
        if not prompt_content:
            raise HTTPException(
                status_code=404, 
                detail=f"Prompt not found for {company_code}/{doc_type_code}/{filename}"
            )
        
        return {
            "status": "success",
            "data": {
                "company_code": company_code,
                "doc_type_code": doc_type_code,
                "filename": filename,
                "content": prompt_content,
                "content_length": len(prompt_content)
            },
            "timestamp": datetime.now().isoformat()
        }
    except HTTPException:
        raise
    except Exception as e:
        raise HTTPException(status_code=500, detail=f"Failed to get prompt: {e}")


@app.get("/prompts-schemas/{company_code}/{doc_type_code}/schema", response_model=dict)
async def get_schema(company_code: str, doc_type_code: str, filename: str = "schema.json"):
    """Get schema data for a specific company and document type"""
    try:
        prompt_schema_manager = get_prompt_schema_manager()
        schema_data = await prompt_schema_manager.get_schema(company_code, doc_type_code, filename)
        
        if not schema_data:
            raise HTTPException(
                status_code=404, 
                detail=f"Schema not found for {company_code}/{doc_type_code}/{filename}"
            )
        
        return {
            "status": "success",
            "data": {
                "company_code": company_code,
                "doc_type_code": doc_type_code,
                "filename": filename,
                "schema": schema_data
            },
            "timestamp": datetime.now().isoformat()
        }
    except HTTPException:
        raise
    except Exception as e:
        raise HTTPException(status_code=500, detail=f"Failed to get schema: {e}")


@app.post("/prompts-schemas/{company_code}/{doc_type_code}/prompt", response_model=dict)
async def upload_prompt(
    company_code: str, 
    doc_type_code: str, 
    prompt_data: dict,
    filename: str = "prompt.txt"
):
    """Upload a new prompt for a specific company and document type"""
    try:
        if "content" not in prompt_data:
            raise HTTPException(status_code=400, detail="Missing 'content' field in request body")
        
        content = prompt_data["content"]
        metadata = prompt_data.get("metadata", {})
        
        prompt_schema_manager = get_prompt_schema_manager()
        success = await prompt_schema_manager.upload_prompt(
            company_code, doc_type_code, content, filename, metadata
        )
        
        if not success:
            raise HTTPException(status_code=500, detail="Failed to upload prompt")
        
        return {
            "status": "success",
            "message": f"Prompt uploaded successfully for {company_code}/{doc_type_code}/{filename}",
            "data": {
                "company_code": company_code,
                "doc_type_code": doc_type_code,
                "filename": filename,
                "content_length": len(content)
            },
            "timestamp": datetime.now().isoformat()
        }
    except HTTPException:
        raise
    except Exception as e:
        raise HTTPException(status_code=500, detail=f"Failed to upload prompt: {e}")


@app.post("/prompts-schemas/{company_code}/{doc_type_code}/schema", response_model=dict)
async def upload_schema(
    company_code: str, 
    doc_type_code: str, 
    schema_request: dict,
    filename: str = "schema.json"
):
    """Upload a new schema for a specific company and document type"""
    try:
        if "schema" not in schema_request:
            raise HTTPException(status_code=400, detail="Missing 'schema' field in request body")
        
        schema_data = schema_request["schema"]
        metadata = schema_request.get("metadata", {})
        
        prompt_schema_manager = get_prompt_schema_manager()
        success = await prompt_schema_manager.upload_schema(
            company_code, doc_type_code, schema_data, filename, metadata
        )
        
        if not success:
            raise HTTPException(status_code=500, detail="Failed to upload schema")
        
        return {
            "status": "success",
            "message": f"Schema uploaded successfully for {company_code}/{doc_type_code}/{filename}",
            "data": {
                "company_code": company_code,
                "doc_type_code": doc_type_code,
                "filename": filename,
                "schema_properties": len(schema_data.get("properties", {}))
            },
            "timestamp": datetime.now().isoformat()
        }
    except HTTPException:
        raise
    except Exception as e:
        raise HTTPException(status_code=500, detail=f"Failed to upload schema: {e}")


@app.post("/prompts-schemas/{company_code}/{doc_type_code}/validate", response_model=dict)
async def validate_prompt_schema(company_code: str, doc_type_code: str):
    """Validate that both prompt and schema exist and are valid for a company/doc_type combination"""
    try:
        # Load both prompt and schema
        prompt_content, schema_data = await load_prompt_and_schema(company_code, doc_type_code)
        
        validation_results = {
            "company_code": company_code,
            "doc_type_code": doc_type_code,
            "prompt": {
                "exists": prompt_content is not None,
                "valid": False,
                "message": ""
            },
            "schema": {
                "exists": schema_data is not None,
                "valid": False,
                "message": ""
            }
        }
        
        # Validate prompt
        if prompt_content:
            from utils.prompt_schema_manager import PromptSchemaValidator
            validator = PromptSchemaValidator()
            is_valid, message = validator.validate_prompt(prompt_content)
            validation_results["prompt"]["valid"] = is_valid
            validation_results["prompt"]["message"] = message
            validation_results["prompt"]["length"] = len(prompt_content)
        else:
            validation_results["prompt"]["message"] = "Prompt not found"
        
        # Validate schema
        if schema_data:
            from utils.prompt_schema_manager import PromptSchemaValidator
            validator = PromptSchemaValidator()
            is_valid, message = validator.validate_schema(schema_data)
            validation_results["schema"]["valid"] = is_valid
            validation_results["schema"]["message"] = message
            validation_results["schema"]["properties_count"] = len(schema_data.get("properties", {}))
        else:
            validation_results["schema"]["message"] = "Schema not found"
        
        overall_valid = (validation_results["prompt"]["valid"] and 
                        validation_results["schema"]["valid"])
        
        return {
            "status": "success",
            "data": validation_results,
            "overall_valid": overall_valid,
            "timestamp": datetime.now().isoformat()
        }
        
    except Exception as e:
        raise HTTPException(status_code=500, detail=f"Failed to validate prompt/schema: {e}")


# Initialize database function
@app.on_event("startup")
async def startup_db_client():
    try:
        # Create necessary directories when using local storage
        from utils.s3_storage import is_s3_enabled
        if not is_s3_enabled():
            base_dir = os.getenv("LOCAL_UPLOAD_DIR")
            if not base_dir:
                logger.error("LOCAL_UPLOAD_DIR not set but STORAGE_BACKEND=local")
            else:
                os.makedirs(base_dir, exist_ok=True)

        # Try connecting to database
        next(get_db())
        logger.info("Successfully connected to database")
    except Exception as e:
        logger.error(f"Failed to connect to database: {str(e)}")
        raise e


# Add this endpoint to your FastAPI app
@app.get("/document-types/{doc_type_id}/companies", response_model=List[dict])
def get_companies_for_document_type(doc_type_id: int, db: Session = Depends(get_db)):
    # Verify document type exists
    doc_type = (
        db.query(DocumentType).filter(DocumentType.doc_type_id == doc_type_id).first()
    )
    if not doc_type:
        raise HTTPException(status_code=404, detail="Document type not found")

    # Get companies that have configurations for this document type
    companies_query = (
        db.query(Company)
        .join(
            CompanyDocumentConfig,
            Company.company_id == CompanyDocumentConfig.company_id,
        )
        .filter(
            CompanyDocumentConfig.doc_type_id == doc_type_id,
            CompanyDocumentConfig.active,
            Company.active,
        )
        .all()
    )

    return [
        {
            "company_id": company.company_id,
            "company_name": company.company_name,
            "company_code": company.company_code,
            "active": company.active,
            "created_at": company.created_at.isoformat(),
            "updated_at": company.updated_at.isoformat(),
        }
        for company in companies_query
    ]


@app.get("/api/admin/usage/daily", response_model=List[dict])
async def get_daily_usage(db: Session = Depends(get_db)):
    """Get daily token usage for the last 30 days."""
    # SQL query using SQLAlchemy to get daily usage
    thirty_days_ago = datetime.now() - timedelta(days=30)

    query = (
        db.query(
            func.date_trunc("day", ApiUsage.api_call_timestamp).label("date"),
            func.sum(ApiUsage.input_token_count).label("input_tokens"),
            func.sum(ApiUsage.output_token_count).label("output_tokens"),
            func.count(ApiUsage.usage_id).label("request_count"),
        )
        .filter(ApiUsage.api_call_timestamp >= thirty_days_ago)
        .group_by(func.date_trunc("day", ApiUsage.api_call_timestamp))
        .order_by(func.date_trunc("day", ApiUsage.api_call_timestamp))
    )

    results = query.all()

    return [
        {
            "date": result.date.strftime("%Y-%m-%d"),
            "input_tokens": result.input_tokens,
            "output_tokens": result.output_tokens,
            "total_tokens": result.input_tokens + result.output_tokens,
            "request_count": result.request_count,
        }
        for result in results
    ]


@app.get("/api/admin/usage/monthly", response_model=List[dict])
async def get_monthly_usage(db: Session = Depends(get_db)):
    """Get monthly token usage for the last 12 months."""
    # SQL query using SQLAlchemy to get monthly usage
    twelve_months_ago = datetime.now() - timedelta(days=365)

    query = (
        db.query(
            func.date_trunc("month", ApiUsage.api_call_timestamp).label("month"),
            func.sum(ApiUsage.input_token_count).label("input_tokens"),
            func.sum(ApiUsage.output_token_count).label("output_tokens"),
            func.count(ApiUsage.usage_id).label("request_count"),
        )
        .filter(ApiUsage.api_call_timestamp >= twelve_months_ago)
        .group_by(func.date_trunc("month", ApiUsage.api_call_timestamp))
        .order_by(func.date_trunc("month", ApiUsage.api_call_timestamp))
    )

    results = query.all()

    return [
        {
            "month": result.month.strftime("%Y-%m"),
            "input_tokens": result.input_tokens,
            "output_tokens": result.output_tokens,
            "total_tokens": result.input_tokens + result.output_tokens,
            "request_count": result.request_count,
        }
        for result in results
    ]

@app.get("/api/admin/usage/by-job")
async def get_api_usage_by_job(
    job_id: int = None, 
    batch_id: int = None, 
    limit: int = 50, 
    db: Session = Depends(get_db)
):
    """Get API usage statistics by individual job or batch"""
    try:
        query = db.query(
            ApiUsage.job_id,
            ProcessingJob.original_filename,
            ProcessingJob.batch_id,
            ApiUsage.input_token_count,
            ApiUsage.output_token_count,
            ApiUsage.processing_time_seconds,
            ApiUsage.api_call_timestamp,
            ApiUsage.model,
            ApiUsage.status
        ).join(ProcessingJob, ApiUsage.job_id == ProcessingJob.job_id)
        
        if job_id:
            query = query.filter(ApiUsage.job_id == job_id)
        elif batch_id:
            query = query.filter(ProcessingJob.batch_id == batch_id)
        else:
            # Return latest records if no filter
            query = query.order_by(ApiUsage.api_call_timestamp.desc()).limit(limit)
        
        results = query.all()
        
        return [
            {
                "job_id": result.job_id,
                "filename": result.original_filename,
                "batch_id": result.batch_id,
                "input_tokens": result.input_token_count,
                "output_tokens": result.output_token_count,
                "total_tokens": result.input_token_count + result.output_token_count,
                "processing_time": result.processing_time_seconds,
                "timestamp": result.api_call_timestamp,
                "model": result.model,
                "status": result.status
            }
            for result in results
        ]
    except Exception as e:
        raise HTTPException(status_code=500, detail=f"Failed to fetch API usage by job: {str(e)}")

@app.get("/api/admin/usage/summary")
async def get_api_usage_summary(db: Session = Depends(get_db)):
    """Get overall API usage summary"""
    try:
        summary = db.query(
            func.count(ApiUsage.usage_id).label("total_calls"),
            func.sum(ApiUsage.input_token_count).label("total_input_tokens"),
            func.sum(ApiUsage.output_token_count).label("total_output_tokens"),
            func.avg(ApiUsage.processing_time_seconds).label("avg_processing_time"),
            func.min(ApiUsage.api_call_timestamp).label("first_call"),
            func.max(ApiUsage.api_call_timestamp).label("last_call")
        ).first()
        
        # Get status breakdown
        status_breakdown = db.query(
            ApiUsage.status,
            func.count(ApiUsage.usage_id).label("count")
        ).group_by(ApiUsage.status).all()
        
        # Get model usage breakdown
        model_breakdown = db.query(
            ApiUsage.model,
            func.count(ApiUsage.usage_id).label("count"),
            func.sum(ApiUsage.input_token_count + ApiUsage.output_token_count).label("total_tokens")
        ).group_by(ApiUsage.model).all()
        
        return {
            "summary": {
                "total_api_calls": summary.total_calls or 0,
                "total_input_tokens": summary.total_input_tokens or 0,
                "total_output_tokens": summary.total_output_tokens or 0,
                "total_tokens": (summary.total_input_tokens or 0) + (summary.total_output_tokens or 0),
                "average_processing_time": float(summary.avg_processing_time or 0),
                "first_call": summary.first_call,
                "last_call": summary.last_call
            },
            "status_breakdown": [
                {"status": status.status, "count": status.count}
                for status in status_breakdown
            ],
            "model_breakdown": [
                {
                    "model": model.model, 
                    "calls": model.count,
                    "total_tokens": model.total_tokens or 0
                }
                for model in model_breakdown
            ]
        }
    except Exception as e:
        raise HTTPException(status_code=500, detail=f"Failed to fetch API usage summary: {str(e)}")


@app.get("/download-by-path")
def download_file_by_path(path: str):
    """Download a file by its full path."""
    logger.warning(f"[DEPRECATED_API] /download-by-path is deprecated and scheduled for removal. path={path}")
    if not os.path.exists(path):
        raise HTTPException(status_code=404, detail=f"File not found on disk: {path}")

    # Get the filename from the path
    filename = os.path.basename(path)

    # Determine content type based on file extension
    file_extension = os.path.splitext(filename)[1].lower()
    content_type = "application/octet-stream"  # Default content type

    if file_extension == ".json":
        content_type = "application/json"
    elif file_extension == ".xlsx":
        content_type = (
            "application/vnd.openxmlformats-officedocument.spreadsheetml.sheet"
        )
    elif file_extension == ".pdf":
        content_type = "application/pdf"
    elif file_extension in [".jpg", ".jpeg"]:
        content_type = "image/jpeg"
    elif file_extension == ".png":
        content_type = "image/png"

    return FileResponse(
        path=path,
        filename=filename,
        media_type=content_type,
    )


@app.get("/download-s3-url")
def get_s3_download_url(s3_path: str, expires_in: int = 3600):
    """Generate a presigned URL for direct S3 download."""
    try:
        logger.warning(f"[DEPRECATED_API] /download-s3-url is deprecated and scheduled for removal. s3_path={s3_path}")
        s3_manager = get_s3_manager()
        if not s3_manager:
            raise HTTPException(status_code=500, detail="S3 storage not available")
        
        # Generate presigned URL
        download_url = s3_manager.generate_presigned_url_for_path(s3_path, expires_in)
        if not download_url:
            raise HTTPException(status_code=404, detail=f"Cannot generate download URL for: {s3_path}")
        
        return {"download_url": download_url, "expires_in": expires_in}
        
    except HTTPException:
        raise
    except Exception as e:
        logger.error(f"Failed to generate S3 download URL: {str(e)}")
        raise HTTPException(status_code=500, detail=f"Failed to generate download URL: {str(e)}")

@app.get("/download-s3")
def download_s3_file(s3_path: str):
    """Download a file from S3 by its S3 path or URI."""
    try:
        logger.warning(f"[DEPRECATED_API] /download-s3 is deprecated and scheduled for removal. s3_path={s3_path}")
        s3_manager = get_s3_manager()
        if not s3_manager:
            raise HTTPException(status_code=500, detail="S3 storage not available")
        
        # Download file content from S3
        file_content = s3_manager.download_file_by_stored_path(s3_path)
        if not file_content:
            raise HTTPException(status_code=404, detail=f"File not found in S3: {s3_path}")
        
        # Extract filename from S3 path
        if s3_path.startswith('s3://'):
            # Handle s3://bucket/path format
            path_parts = s3_path.replace('s3://', '').split('/')
            filename = path_parts[-1] if path_parts else "download"
        else:
            # Handle direct S3 key format
            filename = s3_path.split('/')[-1] if '/' in s3_path else s3_path
        
        # Determine content type based on file extension
        file_extension = os.path.splitext(filename)[1].lower()
        content_type = "application/octet-stream"  # Default
        
        if file_extension == ".json":
            content_type = "application/json"
        elif file_extension == ".xlsx":
            content_type = "application/vnd.openxmlformats-officedocument.spreadsheetml.sheet"
        elif file_extension == ".pdf":
            content_type = "application/pdf"
        elif file_extension in [".jpg", ".jpeg"]:
            content_type = "image/jpeg"
        elif file_extension == ".png":
            content_type = "image/png"
        
        # Create temporary file to serve
        import tempfile
        with tempfile.NamedTemporaryFile(delete=False, suffix=file_extension) as temp_file:
            temp_file.write(file_content)
            temp_file_path = temp_file.name
        
        # Return file response and schedule cleanup
        response = FileResponse(
            path=temp_file_path,
            filename=filename,
            media_type=content_type,
        )
        
        # Add header to indicate this is from S3
        response.headers["X-File-Source"] = "S3"
        
        return response
        
    except Exception as e:
        logger.error(f"Error downloading S3 file {s3_path}: {str(e)}")
        raise HTTPException(status_code=500, detail=f"Failed to download file: {str(e)}")


@app.get("/files")
def get_file_by_path(path: str, db: Session = Depends(get_db)):
    """Get file information by path."""
    file = db.query(DBFile).filter(DBFile.file_path == path).first()
    if not file:
        raise HTTPException(status_code=404, detail="File not found")

    return {
        "file_id": file.file_id,
        "file_name": file.file_name,
        "file_path": file.file_path,
        "file_type": file.file_type,
    }


# Force Delete API Endpoints
@app.delete("/document-types/{doc_type_id}/force-delete")
def force_delete_document_type(
    doc_type_id: int,
    db: Session = Depends(get_db)
):
    """強制刪除文檔類型及其所有依賴"""
    try:
        force_delete_manager = ForceDeleteManager(db)
        result = force_delete_manager.force_delete_document_type(doc_type_id)
        return result
    except ValueError as e:
        raise HTTPException(status_code=404, detail=str(e))
    except Exception as e:
        logger.error(f"Force delete document type failed: {str(e)}")
        raise HTTPException(
            status_code=500,
            detail=f"Force delete failed: {str(e)}"
        )


@app.delete("/companies/{company_id}/force-delete")
def force_delete_company(
    company_id: int,
    db: Session = Depends(get_db)
):
    """強制刪除公司及其所有依賴"""
    try:
        force_delete_manager = ForceDeleteManager(db)
        result = force_delete_manager.force_delete_company(company_id)
        return result
    except ValueError as e:
        raise HTTPException(status_code=404, detail=str(e))
    except Exception as e:
        logger.error(f"Force delete company failed: {str(e)}")
        raise HTTPException(
            status_code=500,
            detail=f"Force delete failed: {str(e)}"
        )


@app.delete("/configs/{config_id}/force-delete")
def force_delete_config(
    config_id: int,
    db: Session = Depends(get_db)
):
    """強制刪除配置及其相關文件"""
    try:
        force_delete_manager = ForceDeleteManager(db)
        result = force_delete_manager.force_delete_config(config_id)
        return result
    except ValueError as e:
        raise HTTPException(status_code=404, detail=str(e))
    except Exception as e:
        logger.error(f"Force delete config failed: {str(e)}")
        raise HTTPException(
            status_code=500,
            detail=f"Force delete failed: {str(e)}"
        )


# 為 Configurations 添加依賴檢查端點
@app.get("/configs/{config_id}/dependencies")
def get_config_dependencies(config_id: int, db: Session = Depends(get_db)):
    """獲取配置的依賴信息"""
    try:
        from utils.dependency_checker import DependencyChecker
        
        # 獲取配置信息
        config = db.query(CompanyDocumentConfig).filter(
            CompanyDocumentConfig.config_id == config_id
        ).first()
        
        if not config:
            raise HTTPException(status_code=404, detail="Configuration not found")
        
        # 檢查是否有相關的處理任務（通過 company_id 和 doc_type_id）
        processing_jobs_count = db.query(ProcessingJob).filter(
            ProcessingJob.company_id == config.company_id,
            ProcessingJob.doc_type_id == config.doc_type_id
        ).count()
        
        batch_jobs_count = db.query(BatchJob).filter(
            BatchJob.company_id == config.company_id,
            BatchJob.doc_type_id == config.doc_type_id
        ).count()
        
        s3_files_count = 0
        if config.prompt_path and config.prompt_path.startswith('s3://'):
            s3_files_count += 1
        if config.schema_path and config.schema_path.startswith('s3://'):
            s3_files_count += 1
        
        total_dependencies = processing_jobs_count + batch_jobs_count + s3_files_count
        can_delete = total_dependencies == 0
        
        config_name = f"{config.company.company_name} - {config.document_type.type_name}"
        
        dependencies = {
            "exists": True,
            "config_name": config_name,
            "can_delete": can_delete,
            "total_dependencies": total_dependencies,
            "dependencies": {
                "processing_jobs": processing_jobs_count,
                "batch_jobs": batch_jobs_count,
                "s3_files": s3_files_count
            },
            "blocking_message": None if can_delete else f"Cannot delete configuration '{config_name}': {processing_jobs_count} processing job(s), {batch_jobs_count} batch job(s), {s3_files_count} S3 file(s) exist."
        }
        
        return dependencies
        
    except HTTPException:
        raise
    except Exception as e:
        logger.error(f"Error checking config dependencies: {str(e)}")
        raise HTTPException(
            status_code=500,
            detail=f"Failed to check dependencies: {str(e)}"
        )


# ============================================================================
# OCR ORDER SYSTEM ENDPOINTS
# ============================================================================

# Pydantic models for OCR Order requests/responses
class CreateOrderRequest(BaseModel):
    order_name: Optional[str] = None
    primary_doc_type_id: Optional[int] = None

class UpdateOrderRequest(BaseModel):
    order_name: Optional[str] = None
    mapping_keys: Optional[List[str]] = None

class CreateOrderItemRequest(BaseModel):
    company_id: int
    doc_type_id: int
    item_name: Optional[str] = None
    item_type: Optional[str] = None
    mapping_config: Optional[Dict[str, Any]] = None

class OrderResponse(BaseModel):
    order_id: int
    order_name: Optional[str]
    status: str
    total_items: int
    completed_items: int
    failed_items: int
    total_attachments: int
    completed_attachments: int
    failed_attachments: int
    primary_doc_type_id: Optional[int]
    primary_doc_type: Optional[dict]
    mapping_file_path: Optional[str]
    mapping_keys: Optional[List[str]]
    final_report_paths: Optional[dict]
    created_at: str
    updated_at: str

class OrderItemFileResponse(BaseModel):
    file_id: int
    filename: str
    file_size: int
    file_type: str
    upload_order: Optional[int]
    uploaded_at: str

class OrderItemResponse(BaseModel):
    item_id: int
    order_id: int
    company_id: int
    doc_type_id: int
    item_name: Optional[str]
    status: str
    item_type: str
    file_count: int
    files: List[OrderItemFileResponse]
    company_name: Optional[str]
    doc_type_name: Optional[str]
    ocr_result_json_path: Optional[str]
    ocr_result_csv_path: Optional[str]
    mapping_config: Optional[Dict[str, Any]]
    applied_template_id: Optional[int]
    created_at: str
    updated_at: str


class MappingConfigUpdateRequest(BaseModel):
    item_type: Optional[str] = None
    mapping_config: Dict[str, Any]
    inherit_defaults: bool = True


class MappingTemplatePayload(BaseModel):
    template_name: str
    item_type: MappingItemType
    config: Dict[str, Any]
    company_id: Optional[int] = None
    doc_type_id: Optional[int] = None
    priority: Optional[int] = 100


class MappingTemplateResponse(BaseModel):
    template_id: int
    template_name: str
    item_type: MappingItemType
    company_id: Optional[int]
    doc_type_id: Optional[int]
    priority: int
    config: Dict[str, Any]
    created_at: str
    updated_at: str


class MappingDefaultPayload(BaseModel):
    company_id: int
    doc_type_id: int
    item_type: MappingItemType = MappingItemType.SINGLE_SOURCE
    template_id: Optional[int] = None
    config_override: Optional[Dict[str, Any]] = None


class MappingDefaultResponse(BaseModel):
    default_id: int
    company_id: int
    doc_type_id: int
    item_type: MappingItemType
    template_id: Optional[int]
    config_override: Optional[Dict[str, Any]]
    created_at: str
    updated_at: str


class MappingTemplateUpdatePayload(BaseModel):
    template_name: Optional[str] = None
    item_type: Optional[MappingItemType] = None
    config: Optional[Dict[str, Any]] = None
    company_id: Optional[int] = None
    doc_type_id: Optional[int] = None
    priority: Optional[int] = None


class MappingDefaultUpdatePayload(BaseModel):
    template_id: Optional[int] = None
    config_override: Optional[Dict[str, Any]] = None


def _serialize_primary_doc_type(doc_type: Optional[DocumentType]) -> Optional[dict]:
    """Serialize primary document type details for API responses."""

    if not doc_type:
        return None

    return {
        "doc_type_id": doc_type.doc_type_id,
        "type_name": doc_type.type_name,
        "type_code": doc_type.type_code,
        "template_json_path": doc_type.template_json_path,
        "template_version": extract_template_version_from_path(doc_type.template_json_path),
        "has_template": bool(doc_type.template_json_path),
    }


def _serialize_mapping_template(template: MappingTemplate) -> Dict[str, Any]:
    item_type_value = template.item_type.value if isinstance(template.item_type, OrderItemType) else template.item_type
    return {
        "template_id": template.template_id,
        "template_name": template.template_name,
        "item_type": MappingItemType(item_type_value),
        "company_id": template.company_id,
        "doc_type_id": template.doc_type_id,
        "priority": template.priority,
        "config": template.config or {},
        "created_at": template.created_at.isoformat() if template.created_at else None,
        "updated_at": template.updated_at.isoformat() if template.updated_at else None,
    }


def _serialize_mapping_default(default: CompanyDocMappingDefault) -> Dict[str, Any]:
    item_type_value = default.item_type.value if isinstance(default.item_type, OrderItemType) else default.item_type
    return {
        "default_id": default.default_id,
        "company_id": default.company_id,
        "doc_type_id": default.doc_type_id,
        "item_type": MappingItemType(item_type_value),
        "template_id": default.template_id,
        "config_override": default.config_override or {},
        "created_at": default.created_at.isoformat() if default.created_at else None,
        "updated_at": default.updated_at.isoformat() if default.updated_at else None,
    }


@app.post("/orders", response_model=dict)
def create_order(request: CreateOrderRequest, db: Session = Depends(get_db)):
    """Create a new OCR order"""
    try:
        primary_doc_type = None
        if request.primary_doc_type_id is not None:
            primary_doc_type = (
                db.query(DocumentType)
                .filter(DocumentType.doc_type_id == request.primary_doc_type_id)
                .first()
            )
            if not primary_doc_type:
                raise HTTPException(status_code=400, detail="Primary document type not found")

        order = OcrOrder(
            order_name=request.order_name,
            status=OrderStatus.DRAFT,
            primary_doc_type_id=primary_doc_type.doc_type_id if primary_doc_type else None,
        )
        db.add(order)
        db.commit()
        db.refresh(order)

        logger.info(
            "Created order %s with primary_doc_type_id=%s",
            order.order_id,
            order.primary_doc_type_id,
        )

        return {
            "order_id": order.order_id,
            "order_name": order.order_name,
            "status": order.status.value,
            "primary_doc_type_id": order.primary_doc_type_id,
            "primary_doc_type": _serialize_primary_doc_type(primary_doc_type),
            "message": "Order created successfully"
        }
    except Exception as e:
        db.rollback()
        logger.error(f"Failed to create order: {str(e)}")
        raise HTTPException(status_code=500, detail=f"Failed to create order: {str(e)}")

@app.get("/orders", response_model=dict)
def list_orders(
    status: Optional[str] = None,
    limit: int = Query(20, ge=1, le=100),
    offset: int = Query(0, ge=0),
    db: Session = Depends(get_db)
):
    """List OCR orders with pagination"""
    try:
        query = db.query(OcrOrder)

        if status:
            query = query.filter(OcrOrder.status == status)

        # Get total count
        total_count = query.count()

        # Apply pagination and ordering
        orders = query.order_by(OcrOrder.created_at.desc()).offset(offset).limit(limit).all()

        order_data = []
        for order in orders:
            # Compute attachment statistics per order
            attachment_stats = compute_order_attachment_stats(order, db)

            mapping_summary = [
                {
                    "item_id": item.item_id,
                    "item_type": item.item_type.value if item.item_type else None,
                    "has_mapping_config": bool(item.mapping_config),
                    "applied_template_id": item.applied_template_id,
                }
                for item in order.items
            ]
            order_data.append({
                "order_id": order.order_id,
                "order_name": order.order_name,
                "status": order.status.value,
                "primary_doc_type_id": order.primary_doc_type_id,
                "primary_doc_type": _serialize_primary_doc_type(order.primary_doc_type),
                "total_items": order.total_items,
                "completed_items": order.completed_items,
                "failed_items": order.failed_items,
                "total_attachments": attachment_stats["total_attachments"],
                "completed_attachments": attachment_stats["completed_attachments"],
                "failed_attachments": attachment_stats["failed_attachments"],
                "mapping_file_path": order.mapping_file_path,
                "mapping_keys": order.mapping_keys,
                "final_report_paths": order.final_report_paths,
                "item_mapping_summary": mapping_summary,
                "created_at": order.created_at.isoformat(),
                "updated_at": order.updated_at.isoformat()
            })

        # Calculate pagination info
        total_pages = (total_count + limit - 1) // limit

        return {
            "data": order_data,
            "pagination": {
                "total_count": total_count,
                "total_pages": total_pages,
                "current_page": (offset // limit) + 1,
                "page_size": limit,
                "has_next": offset + limit < total_count,
                "has_prev": offset > 0
            }
        }
    except Exception as e:
        logger.error(f"Failed to list orders: {str(e)}")
        raise HTTPException(status_code=500, detail=f"Failed to list orders: {str(e)}")

@app.get("/orders/{order_id}", response_model=dict)
def get_order(order_id: int, db: Session = Depends(get_db)):
    """Get order details including items"""
    try:
        order = db.query(OcrOrder).filter(OcrOrder.order_id == order_id).first()
        if not order:
            raise HTTPException(status_code=404, detail="Order not found")

        # Get order items with company and document type info
        items_query = db.query(OcrOrderItem).filter(OcrOrderItem.order_id == order_id)
        items = items_query.all()

        items_data = []
        total_attachments = 0
        completed_attachments = 0
        failed_attachments = 0

        for item in items:
            # Get files for this item - separated into primary and attachments
            file_links = db.query(OrderItemFile).filter(OrderItemFile.item_id == item.item_id).all()

            primary_file = None
            attachments = []

            for link in file_links:
                file_info = link.file
                file_data = {
                    "file_id": file_info.file_id,
                    "filename": file_info.file_name,
                    "file_size": file_info.file_size,
                    "file_type": file_info.file_type,
                    "uploaded_at": link.created_at.isoformat()
                }

                # Check if this is the primary file
                if item.primary_file_id and file_info.file_id == item.primary_file_id:
                    primary_file = file_data
                else:
                    file_data["upload_order"] = link.upload_order
                    attachments.append(file_data)

            attachment_count = len(attachments)
            total_attachments += attachment_count
            if attachment_count > 0:
                if item.status == OrderItemStatus.COMPLETED:
                    completed_attachments += attachment_count
                elif item.status == OrderItemStatus.FAILED:
                    failed_attachments += attachment_count

            items_data.append({
                "item_id": item.item_id,
                "order_id": item.order_id,
                "company_id": item.company_id,
                "doc_type_id": item.doc_type_id,
                "item_name": item.item_name,
                "status": item.status.value,
                "item_type": item.item_type.value if item.item_type else OrderItemType.SINGLE_SOURCE.value,
                "primary_file": primary_file,
                "attachments": attachments,
                "attachment_count": attachment_count,
                "company_name": item.company.company_name if item.company else None,
                "doc_type_name": item.document_type.type_name if item.document_type else None,
                "ocr_result_json_path": item.ocr_result_json_path,
                "ocr_result_csv_path": item.ocr_result_csv_path,
                "mapping_config": item.mapping_config,
                "applied_template_id": item.applied_template_id,
                "processing_started_at": item.processing_started_at.isoformat() if item.processing_started_at else None,
                "processing_completed_at": item.processing_completed_at.isoformat() if item.processing_completed_at else None,
                "processing_time_seconds": item.processing_time_seconds,
                "error_message": item.error_message,
                "created_at": item.created_at.isoformat(),
                "updated_at": item.updated_at.isoformat()
            })

        # Compute remap availability: at least one item with OCR JSON exists
        remap_item_count = sum(1 for it in items_data if bool(it.get("ocr_result_json_path")))
        can_remap = remap_item_count > 0

        return {
            "order_id": order.order_id,
            "order_name": order.order_name,
            "status": order.status.value,
            "primary_doc_type_id": order.primary_doc_type_id,
            "primary_doc_type": _serialize_primary_doc_type(order.primary_doc_type),
            "total_items": order.total_items,
            "completed_items": order.completed_items,
            "failed_items": order.failed_items,
            "mapping_file_path": order.mapping_file_path,
            "mapping_keys": order.mapping_keys,
            "final_report_paths": order.final_report_paths,
            "error_message": order.error_message,
            "items": items_data,
            "can_remap": can_remap,
            "remap_item_count": remap_item_count,
            "item_mapping_summary": [
                {
                    "item_id": item.item_id,
                    "item_type": item.item_type.value if item.item_type else None,
                    "has_mapping_config": bool(item.mapping_config),
                    "applied_template_id": item.applied_template_id,
                }
                for item in order.items
            ],
            "total_attachments": total_attachments,
            "completed_attachments": completed_attachments,
            "failed_attachments": failed_attachments,
            "created_at": order.created_at.isoformat(),
            "updated_at": order.updated_at.isoformat()
        }
    except HTTPException:
        raise
    except Exception as e:
        logger.error(f"Failed to get order {order_id}: {str(e)}")
        raise HTTPException(status_code=500, detail=f"Failed to get order: {str(e)}")

@app.put("/orders/{order_id}", response_model=dict)
def update_order(order_id: int, request: UpdateOrderRequest, db: Session = Depends(get_db)):
    """Update order details"""
    try:
        order = db.query(OcrOrder).filter(OcrOrder.order_id == order_id).first()
        if not order:
            raise HTTPException(status_code=404, detail="Order not found")

        # Allow mapping keys updates for DRAFT, COMPLETED, and MAPPING orders
        # But only allow other fields for DRAFT orders
        if request.order_name is not None:
            if order.status != OrderStatus.DRAFT:
                raise HTTPException(status_code=400, detail="Can only update order name for orders in DRAFT status")
            order.order_name = request.order_name

        if request.mapping_keys is not None:
            if order.status not in [OrderStatus.DRAFT, OrderStatus.OCR_COMPLETED, OrderStatus.COMPLETED, OrderStatus.MAPPING]:
                raise HTTPException(status_code=400, detail="Can only update mapping keys for orders in DRAFT, OCR_COMPLETED, COMPLETED, or MAPPING status")

            # 保存旧的mapping_keys用于比较
            old_mapping_keys = order.mapping_keys or []
            new_mapping_keys = request.mapping_keys or []

            # 更新mapping_keys
            order.mapping_keys = request.mapping_keys

            # 创建映射历史记录（如果映射键发生了变化）
            if old_mapping_keys != new_mapping_keys:
                create_mapping_history_on_update(
                    db=db,
                    order_id=order_id,
                    new_mapping_keys=new_mapping_keys,
                    operation_type="UPDATE",
                    operation_reason="Manual update via API"
                )

        order.updated_at = datetime.utcnow()

        db.commit()
        db.refresh(order)

        return {
            "order_id": order.order_id,
            "order_name": order.order_name,
            "status": order.status.value,
            "mapping_keys": order.mapping_keys,
            "message": "Order updated successfully"
        }
    except HTTPException:
        raise
    except Exception as e:
        db.rollback()
        logger.error(f"Failed to update order {order_id}: {str(e)}")
        raise HTTPException(status_code=500, detail=f"Failed to update order: {str(e)}")

@app.delete("/orders/{order_id}", response_model=dict)
def delete_order(order_id: int, db: Session = Depends(get_db)):
    """Delete order and all its items"""
    try:
        order = db.query(OcrOrder).filter(OcrOrder.order_id == order_id).first()
        if not order:
            raise HTTPException(status_code=404, detail="Order not found")

        # Only allow deletion of DRAFT or FAILED orders
        if order.status not in [OrderStatus.DRAFT, OrderStatus.FAILED]:
            raise HTTPException(status_code=400, detail="Can only delete orders in DRAFT or FAILED status")

        # Delete order (cascade will handle items and files)
        db.delete(order)
        db.commit()

        return {"message": "Order deleted successfully"}
    except HTTPException:
        raise
    except Exception as e:
        db.rollback()
        logger.error(f"Failed to delete order {order_id}: {str(e)}")
        raise HTTPException(status_code=500, detail=f"Failed to delete order: {str(e)}")

@app.post("/orders/{order_id}/items", response_model=dict)
def create_order_item(order_id: int, request: CreateOrderItemRequest, db: Session = Depends(get_db)):
    """Add an item to an order"""
    try:
        # Check if order exists and is in DRAFT status
        order = db.query(OcrOrder).filter(OcrOrder.order_id == order_id).first()
        if not order:
            raise HTTPException(status_code=404, detail="Order not found")

        if order.status != OrderStatus.DRAFT:
            raise HTTPException(status_code=400, detail="Can only add items to orders in DRAFT status")

        # Verify company and document type exist
        company = db.query(Company).filter(Company.company_id == request.company_id).first()
        if not company:
            raise HTTPException(status_code=404, detail="Company not found")

        doc_type = db.query(DocumentType).filter(DocumentType.doc_type_id == request.doc_type_id).first()
        if not doc_type:
            raise HTTPException(status_code=404, detail="Document type not found")

        requested_item_type = (request.item_type or OrderItemType.SINGLE_SOURCE.value).lower()
        try:
            item_type_enum = OrderItemType(requested_item_type)
        except ValueError:
            raise HTTPException(status_code=400, detail="Unsupported item_type")

        # Try to pre-resolve mapping defaults for convenience, but do not hard-fail
        # item creation if defaults are incomplete (e.g., missing master_csv_path).
        # This defers strict validation to the mapping stage and allows users to
        # add items first, then configure mapping.
        resolver = MappingConfigResolver(db)
        resolved_config = None
        try:
            resolved_config = resolver.resolve_for_item(
                company_id=request.company_id,
                doc_type_id=request.doc_type_id,
                item_type=item_type_enum,
                current_config=request.mapping_config,
            )
        except ValueError as exc:
            # Be tolerant at item creation time; log and proceed with empty config
            logger.warning(
                "Create item: mapping defaults invalid for company=%s doc_type=%s item_type=%s; deferring validation. Error=%s",
                request.company_id,
                request.doc_type_id,
                item_type_enum.value,
                str(exc),
            )
            resolved_config = None

        # Create order item
        # Ensure mapping_config is a JSON object to satisfy DB CHECK constraints
        effective_config = resolved_config.config if resolved_config else {}
        item = OcrOrderItem(
            order_id=order_id,
            company_id=request.company_id,
            doc_type_id=request.doc_type_id,
            item_name=request.item_name or f"{company.company_name} - {doc_type.type_name}",
            status=OrderItemStatus.PENDING,
            item_type=item_type_enum,
            mapping_config=effective_config,
            applied_template_id=resolved_config.template_id if resolved_config else None,
        )

        db.add(item)

        # Update order total_items count
        order.total_items += 1
        order.updated_at = datetime.utcnow()

        db.commit()
        db.refresh(item)

        return {
            "item_id": item.item_id,
            "order_id": item.order_id,
            "company_id": item.company_id,
            "doc_type_id": item.doc_type_id,
            "item_name": item.item_name,
            "status": item.status.value,
            "item_type": item.item_type.value,
            "mapping_config": item.mapping_config,
            "applied_template_id": item.applied_template_id,
            "message": "Order item created successfully"
        }
    except HTTPException:
        raise
    except Exception as e:
        db.rollback()
        logger.error(f"Failed to create order item: {str(e)}")
        raise HTTPException(status_code=500, detail=f"Failed to create order item: {str(e)}")

@app.delete("/orders/{order_id}/items/{item_id}", response_model=dict)
def delete_order_item(
    order_id: int,
    item_id: int,
    db: Session = Depends(get_db)
):
    """Delete an order item and all its associated files"""
    try:
        # Verify order exists and is in DRAFT status
        order = db.query(OcrOrder).filter(OcrOrder.order_id == order_id).first()
        if not order:
            raise HTTPException(status_code=404, detail="Order not found")

        if order.status != OrderStatus.DRAFT:
            raise HTTPException(status_code=400, detail="Can only delete items from orders in DRAFT status")

        # Verify item exists and belongs to the order
        item = db.query(OcrOrderItem).filter(
            OcrOrderItem.item_id == item_id,
            OcrOrderItem.order_id == order_id
        ).first()
        if not item:
            raise HTTPException(status_code=404, detail="Order item not found")

        # Get all files linked to this item for cleanup
        file_links = db.query(OrderItemFile).filter(OrderItemFile.item_id == item_id).all()
        file_storage = get_file_storage()

        # Delete files from storage and cleanup file records
        for file_link in file_links:
            file_info = file_link.file

            # Delete from storage using order file system
            try:
                file_storage.delete_order_file(file_info.file_path)
            except Exception as storage_error:
                logger.warning(f"Failed to delete order file {file_info.file_id} from storage: {str(storage_error)}")

            # Remove the file link
            db.delete(file_link)

            # Delete the file record if no other items reference it
            other_links = db.query(OrderItemFile).filter(OrderItemFile.file_id == file_info.file_id).count()
            if other_links == 0:
                db.delete(file_info)

        # Delete the order item (this will cascade delete file links due to DB constraints)
        db.delete(item)

        # Update order totals
        order.total_items = db.query(OcrOrderItem).filter(OcrOrderItem.order_id == order_id).count()
        order.completed_items = db.query(OcrOrderItem).filter(
            OcrOrderItem.order_id == order_id,
            OcrOrderItem.status == OrderItemStatus.COMPLETED
        ).count()
        order.failed_items = db.query(OcrOrderItem).filter(
            OcrOrderItem.order_id == order_id,
            OcrOrderItem.status == OrderItemStatus.FAILED
        ).count()
        order.updated_at = datetime.utcnow()

        db.commit()

        return {
            "message": "Order item deleted successfully",
            "item_id": item_id,
            "files_deleted": len(file_links),
            "order_totals": {
                "total_items": order.total_items,
                "completed_items": order.completed_items,
                "failed_items": order.failed_items
            }
        }

    except HTTPException:
        raise
    except Exception as e:
        db.rollback()
        logger.error(f"Failed to delete order item: {str(e)}")
        raise HTTPException(status_code=500, detail=f"Failed to delete order item: {str(e)}")


@app.put("/orders/{order_id}/items/{item_id}/mapping-config", response_model=dict)
def update_order_item_mapping_config(
    order_id: int,
    item_id: int,
    request: MappingConfigUpdateRequest,
    db: Session = Depends(get_db),
):
    """Update mapping configuration for a specific order item."""

    try:
        order = db.query(OcrOrder).filter(OcrOrder.order_id == order_id).first()
        if not order:
            raise HTTPException(status_code=404, detail="Order not found")

        item = (
            db.query(OcrOrderItem)
            .filter(
                OcrOrderItem.item_id == item_id,
                OcrOrderItem.order_id == order_id,
            )
            .first()
        )
        if not item:
            raise HTTPException(status_code=404, detail="Order item not found")

        if order.status not in [OrderStatus.DRAFT, OrderStatus.MAPPING, OrderStatus.OCR_COMPLETED]:
            raise HTTPException(status_code=400, detail="Mapping configuration can only be updated during DRAFT, OCR_COMPLETED or MAPPING states")

        requested_type = (request.item_type or item.item_type.value).lower()
        try:
            item_type_enum = OrderItemType(requested_type)
        except ValueError:
            raise HTTPException(status_code=400, detail="Unsupported item_type")

        mapping_item_type = MappingItemType(item_type_enum.value)

        resolver = MappingConfigResolver(db)
        payload_override = request.mapping_config or {}

        if request.inherit_defaults:
            try:
                resolved = resolver.resolve_for_item(
                    company_id=item.company_id,
                    doc_type_id=item.doc_type_id,
                    item_type=item_type_enum,
                    current_config=payload_override,
                )
            except ValueError as exc:
                raise HTTPException(status_code=400, detail=str(exc))

            if resolved is None:
                raise HTTPException(status_code=400, detail="No defaults available for this item; provide mapping_config explicitly")

            config_dict = resolved.config
            applied_template_id = resolved.template_id
            source = resolved.source
        else:
            if not payload_override:
                raise HTTPException(status_code=400, detail="mapping_config payload is required when inherit_defaults is false")
            try:
                config_dict = normalise_mapping_config(mapping_item_type, payload_override)
            except ValueError as exc:
                raise HTTPException(status_code=400, detail=str(exc))
            applied_template_id = item.applied_template_id
            source = "manual"

        item.item_type = item_type_enum
        item.mapping_config = config_dict
        item.applied_template_id = applied_template_id
        item.updated_at = datetime.utcnow()
        order.updated_at = datetime.utcnow()

        db.commit()
        db.refresh(item)

        return {
            "item_id": item.item_id,
            "order_id": item.order_id,
            "item_type": item.item_type.value,
            "mapping_config": item.mapping_config,
            "applied_template_id": item.applied_template_id,
            "source": source,
            "message": "Mapping configuration updated successfully",
        }
    except HTTPException:
        raise
    except Exception as e:  # pragma: no cover - defensive logging
        db.rollback()
        logger.error(f"Failed to update mapping config for order {order_id} item {item_id}: {str(e)}")
        raise HTTPException(status_code=500, detail=f"Failed to update mapping configuration: {str(e)}")


@app.post("/mapping/templates", response_model=MappingTemplateResponse)
def create_mapping_template(payload: MappingTemplatePayload, db: Session = Depends(get_db)):
    try:
        item_type_enum = OrderItemType(payload.item_type.value)
    except ValueError as exc:
        raise HTTPException(status_code=400, detail=str(exc))

    try:
        normalized_config = normalise_mapping_config(payload.item_type, payload.config)
    except ValueError as exc:
        raise HTTPException(status_code=400, detail=str(exc))

    template = MappingTemplate(
        template_name=payload.template_name,
        item_type=item_type_enum,
        config=normalized_config,
        company_id=payload.company_id,
        doc_type_id=payload.doc_type_id,
        priority=payload.priority or 100,
    )

    db.add(template)
    db.commit()
    db.refresh(template)

    return _serialize_mapping_template(template)


@app.get("/mapping/templates", response_model=List[MappingTemplateResponse])
def list_mapping_templates(
    company_id: Optional[int] = None,
    doc_type_id: Optional[int] = None,
    item_type: Optional[MappingItemType] = None,
    db: Session = Depends(get_db),
):
    query = db.query(MappingTemplate)
    if company_id is not None:
        query = query.filter((MappingTemplate.company_id == company_id) | (MappingTemplate.company_id.is_(None)))
    if doc_type_id is not None:
        query = query.filter((MappingTemplate.doc_type_id == doc_type_id) | (MappingTemplate.doc_type_id.is_(None)))
    if item_type is not None:
        try:
            item_type_enum = OrderItemType(item_type.value)
        except ValueError as exc:
            raise HTTPException(status_code=400, detail=str(exc))
        query = query.filter(MappingTemplate.item_type == item_type_enum)

    templates = query.order_by(MappingTemplate.priority.asc(), MappingTemplate.template_id.asc()).all()
    return [_serialize_mapping_template(template) for template in templates]


@app.put("/mapping/templates/{template_id}", response_model=MappingTemplateResponse)
def update_mapping_template(
    template_id: int,
    payload: MappingTemplateUpdatePayload,
    db: Session = Depends(get_db),
):
    template = db.query(MappingTemplate).filter(MappingTemplate.template_id == template_id).first()
    if not template:
        raise HTTPException(status_code=404, detail="Template not found")

    if payload.template_name is not None:
        template.template_name = payload.template_name

    if payload.item_type is not None:
        try:
            template.item_type = OrderItemType(payload.item_type.value)
        except ValueError as exc:
            raise HTTPException(status_code=400, detail=str(exc))

    if payload.company_id is not None:
        template.company_id = payload.company_id
    if payload.doc_type_id is not None:
        template.doc_type_id = payload.doc_type_id
    if payload.priority is not None:
        template.priority = payload.priority

    if payload.config is not None:
        try:
            mapping_item_type = MappingItemType(template.item_type.value if isinstance(template.item_type, OrderItemType) else template.item_type)
            template.config = normalise_mapping_config(mapping_item_type, payload.config)
        except ValueError as exc:
            raise HTTPException(status_code=400, detail=str(exc))

    template.updated_at = datetime.utcnow()

    db.commit()
    db.refresh(template)

    return _serialize_mapping_template(template)


@app.delete("/mapping/templates/{template_id}", response_model=dict)
def delete_mapping_template(template_id: int, db: Session = Depends(get_db)):
    template = db.query(MappingTemplate).filter(MappingTemplate.template_id == template_id).first()
    if not template:
        raise HTTPException(status_code=404, detail="Template not found")

    if template.defaults:
        raise HTTPException(status_code=400, detail="Cannot delete template that is referenced by defaults")

    db.delete(template)
    db.commit()

    return {"message": "Template deleted successfully"}


@app.post("/mapping/defaults", response_model=MappingDefaultResponse)
def upsert_mapping_default(payload: MappingDefaultPayload, db: Session = Depends(get_db)):
    try:
        item_type_enum = OrderItemType(payload.item_type.value)
    except ValueError as exc:
        raise HTTPException(status_code=400, detail=str(exc))

    default_record = (
        db.query(CompanyDocMappingDefault)
        .filter(
            CompanyDocMappingDefault.company_id == payload.company_id,
            CompanyDocMappingDefault.doc_type_id == payload.doc_type_id,
            CompanyDocMappingDefault.item_type == item_type_enum,
        )
        .first()
    )

    template = None
    if payload.template_id is not None:
        template = db.query(MappingTemplate).filter(MappingTemplate.template_id == payload.template_id).first()
        if not template:
            raise HTTPException(status_code=404, detail="Template not found")
        if template.item_type != item_type_enum:
            raise HTTPException(status_code=400, detail="Template item_type mismatch with default")

    normalised_override = None
    if payload.config_override is not None:
        template_config: Optional[Dict[str, Any]] = None
        if template and template.config:
            template_config = template.config
        elif (
            default_record
            and default_record.template
            and default_record.template.config
            and payload.template_id is None
        ):
            template_config = default_record.template.config

        try:
            normalised_override = normalise_mapping_override(
                payload.item_type,
                payload.config_override,
                template_config=template_config,
            )
        except ValueError as exc:
            raise HTTPException(status_code=400, detail=str(exc))

    if default_record:
        default_record.template_id = payload.template_id
        default_record.config_override = normalised_override
        default_record.updated_at = datetime.utcnow()
    else:
        default_record = CompanyDocMappingDefault(
            company_id=payload.company_id,
            doc_type_id=payload.doc_type_id,
            item_type=item_type_enum,
            template_id=payload.template_id,
            config_override=normalised_override,
        )
        db.add(default_record)

    db.commit()
    db.refresh(default_record)

    return _serialize_mapping_default(default_record)


@app.get("/mapping/defaults", response_model=List[MappingDefaultResponse])
def list_mapping_defaults(
    company_id: Optional[int] = None,
    doc_type_id: Optional[int] = None,
    item_type: Optional[MappingItemType] = None,
    db: Session = Depends(get_db),
):
    query = db.query(CompanyDocMappingDefault)
    if company_id is not None:
        query = query.filter(CompanyDocMappingDefault.company_id == company_id)
    if doc_type_id is not None:
        query = query.filter(CompanyDocMappingDefault.doc_type_id == doc_type_id)
    if item_type is not None:
        try:
            item_type_enum = OrderItemType(item_type.value)
        except ValueError as exc:
            raise HTTPException(status_code=400, detail=str(exc))
        query = query.filter(CompanyDocMappingDefault.item_type == item_type_enum)

    defaults = query.order_by(CompanyDocMappingDefault.company_id.asc(), CompanyDocMappingDefault.doc_type_id.asc()).all()
    return [_serialize_mapping_default(default) for default in defaults]


@app.get("/mapping/defaults/paged", response_model=dict)
def list_mapping_defaults_paged(
    company_id: Optional[int] = None,
    doc_type_id: Optional[int] = None,
    item_type: Optional[MappingItemType] = None,
    limit: int = Query(10, ge=1, le=100),
    offset: int = Query(0, ge=0),
    db: Session = Depends(get_db),
):
    query = db.query(CompanyDocMappingDefault)
    if company_id is not None:
        query = query.filter(CompanyDocMappingDefault.company_id == company_id)
    if doc_type_id is not None:
        query = query.filter(CompanyDocMappingDefault.doc_type_id == doc_type_id)
    if item_type is not None:
        try:
            item_type_enum = OrderItemType(item_type.value)
        except ValueError as exc:
            raise HTTPException(status_code=400, detail=str(exc))
        query = query.filter(CompanyDocMappingDefault.item_type == item_type_enum)

    total_count = query.count()
    rows = (
        query.order_by(CompanyDocMappingDefault.company_id.asc(), CompanyDocMappingDefault.doc_type_id.asc())
        .offset(offset)
        .limit(limit)
        .all()
    )
    data = [_serialize_mapping_default(r) for r in rows]
    total_pages = (total_count + limit - 1) // limit
    return {
        "data": data,
        "pagination": {
            "total_count": total_count,
            "page_size": limit,
            "current_page": (offset // limit) + 1,
            "total_pages": total_pages,
            "has_next": offset + limit < total_count,
            "has_prev": offset > 0,
        },
    }


@app.delete("/mapping/defaults/{default_id}", response_model=dict)
def delete_mapping_default(default_id: int, db: Session = Depends(get_db)):
    record = db.query(CompanyDocMappingDefault).filter(CompanyDocMappingDefault.default_id == default_id).first()
    if not record:
        raise HTTPException(status_code=404, detail="Default not found")

    db.delete(record)
    db.commit()

    return {"message": "Default deleted successfully"}

@app.post("/orders/{order_id}/submit", response_model=dict)
def submit_order(order_id: int, background_tasks: BackgroundTasks, db: Session = Depends(get_db)):
    """Submit order for processing"""
    try:
        order = db.query(OcrOrder).filter(OcrOrder.order_id == order_id).first()
        if not order:
            raise HTTPException(status_code=404, detail="Order not found")

        if order.status != OrderStatus.DRAFT:
            raise HTTPException(status_code=400, detail="Can only submit orders in DRAFT status")

        if order.total_items == 0:
            raise HTTPException(status_code=400, detail="Cannot submit order with no items")

        # Check if every item has at least a primary file or attachments
        # Previous logic only checked attachments (file_count), causing false negatives
        items_without_files = db.query(OcrOrderItem).filter(
            OcrOrderItem.order_id == order_id,
            OcrOrderItem.primary_file_id.is_(None),
            OcrOrderItem.file_count == 0,
        ).count()

        if items_without_files > 0:
            raise HTTPException(status_code=400, detail=f"{items_without_files} items have no files uploaded")

        # Update order status
        order.status = OrderStatus.PROCESSING
        order.updated_at = datetime.utcnow()

        db.commit()

        # Start background processing
        background_tasks.add_task(start_order_processing, order_id)

        return {
            "order_id": order.order_id,
            "status": order.status.value,
            "message": "Order submitted for processing successfully"
        }
    except HTTPException:
        raise
    except Exception as e:
        db.rollback()
        logger.error(f"Failed to submit order {order_id}: {str(e)}")
        raise HTTPException(status_code=500, detail=f"Failed to submit order: {str(e)}")

@app.post("/orders/{order_id}/process-ocr-only", response_model=dict)
def process_order_ocr_only(order_id: int, background_tasks: BackgroundTasks, db: Session = Depends(get_db)):
    """Submit order for OCR-only processing (no mapping)"""
    try:
        order = db.query(OcrOrder).filter(OcrOrder.order_id == order_id).first()
        if not order:
            raise HTTPException(status_code=404, detail="Order not found")

        if order.status != OrderStatus.DRAFT:
            raise HTTPException(status_code=400, detail="Can only process orders in DRAFT status")

        if order.total_items == 0:
            raise HTTPException(status_code=400, detail="Cannot process order with no items")

        # Check if every item has at least a primary file or attachments
        items_without_files = db.query(OcrOrderItem).filter(
            OcrOrderItem.order_id == order_id,
            OcrOrderItem.primary_file_id.is_(None),
            OcrOrderItem.file_count == 0,
        ).count()

        if items_without_files > 0:
            raise HTTPException(status_code=400, detail=f"{items_without_files} items have no files uploaded")

        # Update order status to PROCESSING
        order.status = OrderStatus.PROCESSING
        order.updated_at = datetime.utcnow()

        db.commit()

        # Start background OCR-only processing
        background_tasks.add_task(start_order_ocr_only_processing, order_id)

        return {
            "order_id": order.order_id,
            "status": order.status.value,
            "message": "Order submitted for OCR-only processing successfully"
        }
    except HTTPException:
        raise
    except Exception as e:
        db.rollback()
        logger.error(f"Failed to process order OCR-only {order_id}: {str(e)}")
        raise HTTPException(status_code=500, detail=f"Failed to process order: {str(e)}")


@app.post("/orders/{order_id}/process-mapping", response_model=dict)
def process_order_mapping_only(order_id: int, background_tasks: BackgroundTasks, db: Session = Depends(get_db)):
    """Submit order for mapping-only processing (OCR already completed)"""
    try:
        order = db.query(OcrOrder).filter(OcrOrder.order_id == order_id).first()
        if not order:
            raise HTTPException(status_code=404, detail="Order not found")

        if order.status not in [OrderStatus.OCR_COMPLETED, OrderStatus.MAPPING, OrderStatus.COMPLETED, OrderStatus.FAILED]:
            raise HTTPException(
                status_code=400,
                detail="Can only process mapping for orders in OCR_COMPLETED, MAPPING, COMPLETED, or FAILED status",
            )

        # Validate that items have OCR results available (json exists)
        items_without_ocr = db.query(OcrOrderItem).filter(
            OcrOrderItem.order_id == order_id,
            OcrOrderItem.ocr_result_json_path.is_(None)
        ).count()

        if items_without_ocr > 0:
            raise HTTPException(status_code=400, detail=f"{items_without_ocr} items don't have completed OCR results")

        # Update order status to MAPPING
        order.status = OrderStatus.MAPPING
        order.updated_at = datetime.utcnow()

        db.commit()

        # Start background mapping processing
        background_tasks.add_task(start_order_mapping_only_processing, order_id)

        return {
            "order_id": order.order_id,
            "status": order.status.value,
            "message": "Order submitted for mapping processing successfully"
        }
    except HTTPException:
        raise
    except Exception as e:
        db.rollback()
        logger.error(f"Failed to process order mapping {order_id}: {str(e)}")
        raise HTTPException(status_code=500, detail=f"Failed to process mapping: {str(e)}")


# ========== PRIMARY FILE ENDPOINTS ==========

@app.post("/orders/{order_id}/items/{item_id}/primary-file", response_model=dict)
def upload_primary_file_to_order_item(
    order_id: int,
    item_id: int,
    file: UploadFile = File(...),
    replace: bool = Query(False),
    db: Session = Depends(get_db)
):
    """Upload or replace the primary file for an order item"""
    try:
        # Verify order and item exist
        order = db.query(OcrOrder).filter(OcrOrder.order_id == order_id).first()
        if not order:
            raise HTTPException(status_code=404, detail="Order not found")

        if order.status != OrderStatus.DRAFT:
            raise HTTPException(status_code=400, detail="Can only upload files to orders in DRAFT status")

        item = db.query(OcrOrderItem).filter(
            OcrOrderItem.item_id == item_id,
            OcrOrderItem.order_id == order_id
        ).first()
        if not item:
            raise HTTPException(status_code=404, detail="Order item not found")

        # Validate file type
        valid_types = ['image/jpeg', 'image/png', 'application/pdf']
        valid_extensions = ['.jpg', '.jpeg', '.png', '.pdf']
        file_extension = '.' + file.filename.split('.')[-1].lower() if '.' in file.filename else ''

        if file.content_type not in valid_types and file_extension not in valid_extensions:
            raise HTTPException(
                status_code=400,
                detail=f"File {file.filename} type not supported. Please upload images or PDFs."
            )

        # If replacing and old primary file exists, delete it
        if replace and item.primary_file_id:
            _delete_primary_file_and_results(item, db)

        # Get file size
        file.file.seek(0, 2)
        file_size = file.file.tell()
        file.file.seek(0)

        # Get file storage manager
        file_storage = get_file_storage()

        # Save file to storage
        try:
            file_path, original_filename = file_storage.save_order_file(
                file, order_id, item_id
            )

            # Create file record
            db_file = DBFile(
                file_path=file_path,
                file_name=file.filename,
                file_size=file_size,
                file_type=file.content_type
            )
            db.add(db_file)
            db.flush()  # Get file_id

            # Link file as primary
            item.primary_file_id = db_file.file_id
            item.updated_at = datetime.utcnow()

            # Create association in order_item_files for unified cleanup
            order_item_file = OrderItemFile(
                item_id=item_id,
                file_id=db_file.file_id,
                upload_order=0  # Primary file gets upload_order=0
            )
            db.add(order_item_file)

            order.updated_at = datetime.utcnow()
            db.commit()

            return {
                "message": "Primary file uploaded successfully",
                "file_id": db_file.file_id,
                "filename": file.filename,
                "file_size": file_size,
                "file_path": file_path
            }

        except Exception as e:
            db.rollback()
            logger.error(f"Failed to save primary file {file.filename}: {str(e)}")
            raise HTTPException(status_code=500, detail=f"Failed to save file {file.filename}")

    except HTTPException:
        raise
    except Exception as e:
        logger.error(f"Failed to upload primary file: {str(e)}")
        raise HTTPException(status_code=500, detail=f"Failed to upload primary file: {str(e)}")


@app.delete("/orders/{order_id}/items/{item_id}/primary-file", response_model=dict)
def delete_primary_file_from_order_item(
    order_id: int,
    item_id: int,
    db: Session = Depends(get_db)
):
    """Delete the primary file from an order item"""
    try:
        # Verify order exists and is in DRAFT status
        order = db.query(OcrOrder).filter(OcrOrder.order_id == order_id).first()
        if not order:
            raise HTTPException(status_code=404, detail="Order not found")

        if order.status != OrderStatus.DRAFT:
            raise HTTPException(status_code=400, detail="Can only delete files from orders in DRAFT status")

        # Verify item exists and belongs to the order
        item = db.query(OcrOrderItem).filter(
            OcrOrderItem.item_id == item_id,
            OcrOrderItem.order_id == order_id
        ).first()
        if not item:
            raise HTTPException(status_code=404, detail="Order item not found")

        if not item.primary_file_id:
            raise HTTPException(status_code=404, detail="Item has no primary file")

        # Delete file and results
        _delete_primary_file_and_results(item, db)

        return {
            "message": "Primary file deleted successfully",
            "item_id": item_id
        }

    except HTTPException:
        raise
    except Exception as e:
        logger.error(f"Failed to delete primary file: {str(e)}")
        raise HTTPException(status_code=500, detail=f"Failed to delete primary file: {str(e)}")


def _delete_primary_file_and_results(item: OcrOrderItem, db: Session):
    """Helper to delete primary file and its associated OCR results."""
    try:
        file_id = item.primary_file_id
        if not file_id:
            return

        # Store paths before clearing them (they'll be used for S3 deletion)
        json_result_path = item.ocr_result_json_path
        csv_result_path = item.ocr_result_csv_path

        # Get file info for deletion from storage
        file_info = db.query(DBFile).filter(DBFile.file_id == file_id).first()
        if file_info:
            file_storage = get_file_storage()
            try:
                file_storage.delete_file(file_info.file_path)
            except Exception as e:
                logger.warning(f"Could not delete file from storage: {e}")

        # Remove from order_item_files
        file_link = db.query(OrderItemFile).filter(
            OrderItemFile.item_id == item.item_id,
            OrderItemFile.file_id == file_id
        ).first()
        if file_link:
            db.delete(file_link)

        # Delete from files table
        db.query(DBFile).filter(DBFile.file_id == file_id).delete()

        # Clear primary_file_id and JSON/CSV results
        item.primary_file_id = None
        item.ocr_result_json_path = None
        item.ocr_result_csv_path = None
        item.updated_at = datetime.utcnow()

        # Delete S3 results if they exist (use stored paths)
        try:
            s3_manager = get_s3_manager()
            if s3_manager:
                if json_result_path:
                    try:
                        s3_manager.delete_file_by_stored_path(json_result_path)
                        logger.info(f"Deleted S3 JSON result: {json_result_path}")
                    except Exception as e:
                        logger.warning(f"Failed to delete S3 JSON result {json_result_path}: {e}")

                if csv_result_path:
                    try:
                        s3_manager.delete_file_by_stored_path(csv_result_path)
                        logger.info(f"Deleted S3 CSV result: {csv_result_path}")
                    except Exception as e:
                        logger.warning(f"Failed to delete S3 CSV result {csv_result_path}: {e}")
        except Exception as e:
            logger.warning(f"Error deleting S3 results: {str(e)}")

        db.commit()

    except Exception as e:
        logger.error(f"Error in _delete_primary_file_and_results: {str(e)}")
        db.rollback()
        raise HTTPException(status_code=500, detail=f"Failed to delete primary file: {str(e)}")


@app.get("/orders/{order_id}/items/{item_id}/files/{file_id}/download/json", response_model=dict)
def download_attachment_json(
    order_id: int,
    item_id: int,
    file_id: int,
    db: Session = Depends(get_db)
):
    """Download JSON result for a specific attachment file"""
    try:
        # Verify order exists
        order = db.query(OcrOrder).filter(OcrOrder.order_id == order_id).first()
        if not order:
            raise HTTPException(status_code=404, detail="Order not found")

        # Verify item exists and belongs to order
        item = db.query(OcrOrderItem).filter(
            OcrOrderItem.item_id == item_id,
            OcrOrderItem.order_id == order_id
        ).first()
        if not item:
            raise HTTPException(status_code=404, detail="Order item not found")

        # Verify file exists and is linked to the item (but not primary)
        file_link = db.query(OrderItemFile).filter(
            OrderItemFile.item_id == item_id,
            OrderItemFile.file_id == file_id
        ).first()
        if not file_link:
            raise HTTPException(status_code=404, detail="File not linked to this item")

        # Check that this is not the primary file
        if file_id == item.primary_file_id:
            raise HTTPException(status_code=400, detail="Use the item JSON download for primary file results. This endpoint is for attachments only.")

        # Construct S3 path for attachment JSON
        # Path pattern: upload/results/orders/{item_id//1000}/items/{item_id}/files/file_{file_id}_result.json
        s3_manager = get_s3_manager()
        if not s3_manager:
            raise HTTPException(status_code=500, detail="S3 storage not available")

        # Build the stored path - S3StorageManager already adds upload/ prefix, so include it in the relative path
        stored_path = f"upload/results/orders/{item_id // 1000}/items/{item_id}/files/file_{file_id}_result.json"

        # Download file content from S3
        try:
            file_content = s3_manager.download_file_by_stored_path(stored_path)
            if not file_content:
                raise HTTPException(status_code=404, detail="Attachment JSON result not found or not yet processed")

            # Parse JSON content
            json_data = json.loads(file_content.decode('utf-8'))

            return {
                "file_id": file_id,
                "item_id": item_id,
                "json_data": json_data,
                "s3_path": stored_path
            }
        except json.JSONDecodeError as e:
            logger.error(f"Failed to parse JSON from attachment {file_id}: {str(e)}")
            raise HTTPException(status_code=500, detail="Invalid JSON format in attachment result")
        except Exception as e:
            logger.error(f"Failed to download attachment JSON from S3 path {stored_path}: {str(e)}")
            raise HTTPException(status_code=404, detail="Attachment JSON result not found or not yet processed")

    except HTTPException:
        raise
    except Exception as e:
        logger.error(f"Error downloading attachment JSON: {str(e)}")
        raise HTTPException(status_code=500, detail=f"Error downloading JSON: {str(e)}")


# ========== PRIMARY FILE RESULT DOWNLOAD ENDPOINTS ==========

@app.get("/orders/{order_id}/items/{item_id}/primary/download/json")
def download_primary_file_json(
    order_id: int,
    item_id: int,
    db: Session = Depends(get_db)
):
    """Download JSON result for the primary file of a specific order item.

    This reads the same per-file JSON used by attachment downloads, so that:
    primary-file JSON + all attachment JSONs == aggregated item results JSON.
    """
    try:
        # Verify order exists
        order = db.query(OcrOrder).filter(OcrOrder.order_id == order_id).first()
        if not order:
            raise HTTPException(status_code=404, detail="Order not found")

        # Verify item exists and belongs to order
        item = db.query(OcrOrderItem).filter(
            OcrOrderItem.item_id == item_id,
            OcrOrderItem.order_id == order_id
        ).first()
        if not item:
            raise HTTPException(status_code=404, detail="Order item not found")

        if not item.primary_file_id:
            raise HTTPException(status_code=404, detail="Item has no primary file")

        s3_manager = get_s3_manager()
        if not s3_manager:
            raise HTTPException(status_code=500, detail="S3 storage not available")

        # Prefer per-file JSON for the primary file, consistent with attachment endpoints
        stored_path = f"upload/results/orders/{item_id // 1000}/items/{item_id}/files/file_{item.primary_file_id}_result.json"
        file_content = s3_manager.download_file_by_stored_path(stored_path)

        # Fallback to legacy primary-only JSON if per-file JSON is missing (older jobs)
        if not file_content:
            if not item.ocr_result_json_path:
                raise HTTPException(status_code=404, detail="Primary JSON result not found or not yet processed")

            file_content = s3_manager.download_file_by_stored_path(item.ocr_result_json_path)
            stored_path = item.ocr_result_json_path

            if not file_content:
                raise HTTPException(status_code=404, detail="Primary JSON result not found or not yet processed")

        # Create temporary file for download
        import tempfile
        import os

        with tempfile.NamedTemporaryFile(delete=False, suffix=".json") as temp_file:
            temp_file.write(file_content)
            temp_file_path = temp_file.name

        filename = f"order_{order_id}_item_{item_id}_primary.json"
        response = FileResponse(
            path=temp_file_path,
            filename=filename,
            media_type="application/json",
        )
        response.headers["X-File-Source"] = "S3"
        response.headers["X-Result-Scope"] = "primary_file_only"
        response.headers["X-Result-Path"] = stored_path
        return response

    except HTTPException:
        raise
    except Exception as e:
        logger.error(f"Failed to download primary file JSON for item {item_id}: {str(e)}")
        raise HTTPException(status_code=500, detail=f"Failed to download JSON file: {str(e)}")


@app.get("/orders/{order_id}/items/{item_id}/primary/download/csv")
def download_primary_file_csv(
    order_id: int,
    item_id: int,
    db: Session = Depends(get_db)
):
    """Download CSV result for the primary file of a specific order item.

    This converts the primary file's per-file JSON to CSV using the same deep
    flattening as other CSV exports, so that primary CSV + all attachment CSVs
    align with the aggregated item CSV.
    """
    try:
        # Verify order exists
        order = db.query(OcrOrder).filter(OcrOrder.order_id == order_id).first()
        if not order:
            raise HTTPException(status_code=404, detail="Order not found")

        # Verify item exists and belongs to order
        item = db.query(OcrOrderItem).filter(
            OcrOrderItem.item_id == item_id,
            OcrOrderItem.order_id == order_id
        ).first()
        if not item:
            raise HTTPException(status_code=404, detail="Order item not found")

        if not item.primary_file_id:
            raise HTTPException(status_code=404, detail="Item has no primary file")

        s3_manager = get_s3_manager()
        if not s3_manager:
            raise HTTPException(status_code=500, detail="S3 storage not available")

        # Prefer per-file JSON for the primary file, consistent with attachment endpoints
        stored_path = f"upload/results/orders/{item_id // 1000}/items/{item_id}/files/file_{item.primary_file_id}_result.json"
        file_content = s3_manager.download_file_by_stored_path(stored_path)

        # Fallback to legacy primary-only JSON if per-file JSON is missing (older jobs)
        if not file_content:
            if not item.ocr_result_json_path:
                raise HTTPException(status_code=404, detail="Primary JSON result not found or not yet processed")

            file_content = s3_manager.download_file_by_stored_path(item.ocr_result_json_path)
            stored_path = item.ocr_result_json_path

            if not file_content:
                raise HTTPException(status_code=404, detail="Primary JSON result not found or not yet processed")

        # Parse JSON and convert to CSV
        try:
            json_data = json.loads(file_content.decode('utf-8'))
        except json.JSONDecodeError as e:
            logger.error(f"Failed to parse primary JSON for CSV for item {item_id}: {str(e)}")
            raise HTTPException(status_code=500, detail="Invalid JSON format in primary result")

        csv_content = convert_json_to_csv(json_data)
        if not csv_content:
            raise HTTPException(status_code=500, detail="Failed to convert JSON to CSV")

        # Apply Excel formula escaping and add UTF-8 BOM
        escaped_csv_content = escape_excel_formulas_in_csv(csv_content)
        file_content_with_bom = b'\xef\xbb\xbf' + escaped_csv_content.encode('utf-8')

        # Create temporary file for download
        import tempfile
        import os
        filename = f"order_{order_id}_item_{item_id}_primary.csv"
        with tempfile.NamedTemporaryFile(delete=False, suffix=".csv") as temp_file:
            temp_file.write(file_content_with_bom)
            temp_file_path = temp_file.name

        response = FileResponse(
            path=temp_file_path,
            filename=filename,
            media_type="text/csv; charset=utf-8",
        )
        response.headers["X-File-Source"] = "S3"
        response.headers["X-Result-Scope"] = "primary_file_only"
        response.headers["X-Result-Path"] = stored_path
        return response

    except HTTPException:
        raise
    except Exception as e:
        logger.error(f"Failed to download primary file CSV for item {item_id}: {str(e)}")
        raise HTTPException(status_code=500, detail=f"Failed to download CSV file: {str(e)}")


# ========== ATTACHMENT FILES ENDPOINTS ==========

@app.post("/orders/{order_id}/items/{item_id}/files", response_model=dict)
def upload_attachment_files_to_order_item(
    order_id: int,
    item_id: int,
    files: List[UploadFile] = File(...),
    db: Session = Depends(get_db)
):
    """Upload attachment files to a specific order item (attachments only, not primary file)"""
    try:
        # Verify order and item exist
        order = db.query(OcrOrder).filter(OcrOrder.order_id == order_id).first()
        if not order:
            raise HTTPException(status_code=404, detail="Order not found")

        if order.status != OrderStatus.DRAFT:
            raise HTTPException(status_code=400, detail="Can only upload files to orders in DRAFT status")

        item = db.query(OcrOrderItem).filter(
            OcrOrderItem.item_id == item_id,
            OcrOrderItem.order_id == order_id
        ).first()
        if not item:
            raise HTTPException(status_code=404, detail="Order item not found")

        # Get company and document type information for file storage
        company = db.query(Company).filter(Company.company_id == item.company_id).first()
        doc_type = db.query(DocumentType).filter(DocumentType.doc_type_id == item.doc_type_id).first()

        if not company or not doc_type:
            raise HTTPException(status_code=500, detail="Company or document type not found")

        # Get file storage manager
        file_storage = get_file_storage()
        uploaded_files = []

        for file in files:
            # Validate file type
            valid_types = ['image/jpeg', 'image/png', 'application/pdf']
            valid_extensions = ['.jpg', '.jpeg', '.png', '.pdf']
            file_extension = '.' + file.filename.split('.')[-1].lower() if '.' in file.filename else ''

            if file.content_type not in valid_types and file_extension not in valid_extensions:
                raise HTTPException(
                    status_code=400,
                    detail=f"File {file.filename} type not supported. Please upload images or PDFs."
                )

            # Get file size before upload (to avoid I/O operation on closed file)
            file.file.seek(0, 2)  # Move to end of file
            file_size = file.file.tell()  # Get file size
            file.file.seek(0)  # Reset to beginning

            # Save file to storage using dedicated order file system
            try:
                file_path, original_filename = file_storage.save_order_file(
                    file, order_id, item_id
                )

                # Create file record
                db_file = DBFile(
                    file_path=file_path,
                    file_name=file.filename,
                    file_size=file_size,
                    file_type=file.content_type
                )
                db.add(db_file)
                db.flush()  # Get file_id

                # Link file to order item
                order_item_file = OrderItemFile(
                    item_id=item_id,
                    file_id=db_file.file_id,
                    upload_order=item.file_count + len(uploaded_files) + 1
                )
                db.add(order_item_file)

                uploaded_files.append({
                    "file_id": db_file.file_id,
                    "filename": file.filename,
                    "file_size": file_size,
                    "file_path": file_path
                })

            except Exception as e:
                logger.error(f"Failed to save file {file.filename}: {str(e)}")
                raise HTTPException(status_code=500, detail=f"Failed to save file {file.filename}")

        # Update item file count (only for attachments, not including primary file)
        item.file_count += len(uploaded_files)
        item.updated_at = datetime.utcnow()

        # Update order timestamp
        order.updated_at = datetime.utcnow()

        db.commit()

        return {
            "message": f"Successfully uploaded {len(uploaded_files)} attachment files",
            "uploaded_files": uploaded_files,
            "attachment_count": item.file_count
        }

    except HTTPException:
        raise
    except Exception as e:
        db.rollback()
        logger.error(f"Failed to upload files to order item: {str(e)}")
        raise HTTPException(status_code=500, detail=f"Failed to upload files: {str(e)}")

@app.get("/orders/{order_id}/items/{item_id}/files", response_model=dict)
def list_order_item_files(order_id: int, item_id: int, db: Session = Depends(get_db)):
    """List files for a specific order item (separated into primary and attachments)"""
    try:
        # Verify order and item exist
        order = db.query(OcrOrder).filter(OcrOrder.order_id == order_id).first()
        if not order:
            raise HTTPException(status_code=404, detail="Order not found")

        item = db.query(OcrOrderItem).filter(
            OcrOrderItem.item_id == item_id,
            OcrOrderItem.order_id == order_id
        ).first()
        if not item:
            raise HTTPException(status_code=404, detail="Order item not found")

        # Get files linked to this item - separated into primary and attachments
        file_links = db.query(OrderItemFile).filter(OrderItemFile.item_id == item_id).all()

        primary_file = None
        attachments = []

        for link in file_links:
            file_info = link.file
            file_data = {
                "file_id": file_info.file_id,
                "filename": file_info.file_name,
                "file_size": file_info.file_size,
                "file_type": file_info.file_type,
                "uploaded_at": link.created_at.isoformat()
            }

            # Check if this is the primary file
            if item.primary_file_id and file_info.file_id == item.primary_file_id:
                primary_file = file_data
            else:
                file_data["upload_order"] = link.upload_order
                attachments.append(file_data)

        return {
            "item_id": item_id,
            "primary_file": primary_file,
            "attachments": attachments,
            "attachment_count": len(attachments)
        }

    except HTTPException:
        raise
    except Exception as e:
        logger.error(f"Failed to list order item files: {str(e)}")
        raise HTTPException(status_code=500, detail=f"Failed to list files: {str(e)}")

@app.delete("/orders/{order_id}/items/{item_id}/files/{file_id}", response_model=dict)
def delete_order_item_file(
    order_id: int,
    item_id: int,
    file_id: int,
    db: Session = Depends(get_db)
):
    """Delete a specific file from an order item"""
    try:
        # Verify order exists and is in DRAFT status
        order = db.query(OcrOrder).filter(OcrOrder.order_id == order_id).first()
        if not order:
            raise HTTPException(status_code=404, detail="Order not found")

        if order.status != OrderStatus.DRAFT:
            raise HTTPException(status_code=400, detail="Can only delete files from orders in DRAFT status")

        # Verify item exists and belongs to the order
        item = db.query(OcrOrderItem).filter(
            OcrOrderItem.item_id == item_id,
            OcrOrderItem.order_id == order_id
        ).first()
        if not item:
            raise HTTPException(status_code=404, detail="Order item not found")

        # Verify file exists and is linked to the item
        file_link = db.query(OrderItemFile).filter(
            OrderItemFile.item_id == item_id,
            OrderItemFile.file_id == file_id
        ).first()
        if not file_link:
            raise HTTPException(status_code=404, detail="File not linked to this item")

        # Get file info for deletion from storage
        file_info = db.query(DBFile).filter(DBFile.file_id == file_id).first()
        if not file_info:
            raise HTTPException(status_code=404, detail="File not found")

        # Delete file from storage using order file system
        try:
            file_storage = get_file_storage()
            file_storage.delete_order_file(file_info.file_path)
        except Exception as storage_error:
            logger.warning(f"Failed to delete order file from storage: {str(storage_error)}")
            # Continue with database deletion even if storage deletion fails

        # Remove file link from order item
        db.delete(file_link)

        # Delete the file record if no other items reference it
        other_links = db.query(OrderItemFile).filter(OrderItemFile.file_id == file_id).count()
        if other_links == 0:
            db.delete(file_info)

        # Update item file count (attachments only, exclude primary file)
        remaining_attachments = db.query(OrderItemFile).filter(
            OrderItemFile.item_id == item_id,
            OrderItemFile.file_id != item.primary_file_id
        ).count()
        item.file_count = remaining_attachments
        item.updated_at = datetime.utcnow()

        db.commit()

        return {
            "message": "File deleted successfully",
            "item_id": item_id,
            "file_id": file_id,
            "remaining_files": remaining_attachments
        }

    except HTTPException:
        raise
    except Exception as e:
        db.rollback()
        logger.error(f"Failed to delete order item file: {str(e)}")
        raise HTTPException(status_code=500, detail=f"Failed to delete file: {str(e)}")

@app.post("/orders/{order_id}/items/{item_id}/awb/attach-month", response_model=dict)
def attach_awb_month_to_item(
    order_id: int,
    item_id: int,
    month: str = Form(...),
    include_bill: bool = Form(False),  # DEPRECATED: Monthly bills should be uploaded via OCR Orders / AWB monthly pipeline
    monthly_bill_pdf: UploadFile = File(None),  # DEPRECATED: Monthly bills should be uploaded via OCR Orders / AWB monthly pipeline
    debug: bool = Query(False, description="If true, return detailed diagnostics including sample invoice keys and prefix statistics"),
    db: Session = Depends(get_db)
):
    """Attach AWB invoices from a specific month to an order item

    Args:
        order_id: ID of the order
        item_id: ID of the order item
        month: Month in YYYY-MM format (e.g., "2025-10")
        include_bill: (DEPRECATED) No longer used. Monthly bills should be uploaded via "Upload Files" button.
        monthly_bill_pdf: (DEPRECATED) No longer used. Monthly bills should be uploaded via "Upload Files" button.
        debug: If true, return detailed diagnostics including sample invoice keys

    Returns:
        JSON with statistics: success, order_id, item_id, invoices_found, added_files, skipped_duplicates, message,
        and optionally debug info with sample keys and prefix statistics
    """
    try:
        logger.warning(
            "[DEPRECATED_API] /orders/{order_id}/items/{item_id}/awb/attach-month is deprecated; "
            "prefer /api/awb/process-monthly and the AWB Orders pipeline."
        )
        # Validate month format
        if not month or '-' not in month:
            raise HTTPException(status_code=400, detail="Month must be in YYYY-MM format")

        try:
            year, mm = month.split('-')
            int(year)
            int(mm)
            if int(mm) < 1 or int(mm) > 12:
                raise ValueError("Invalid month")
        except (ValueError, IndexError):
            raise HTTPException(status_code=400, detail="Month must be in YYYY-MM format (e.g., 2025-10)")

        # Verify order exists and is DRAFT
        order = db.query(OcrOrder).filter(OcrOrder.order_id == order_id).first()
        if not order:
            raise HTTPException(status_code=404, detail="Order not found")

        if order.status != OrderStatus.DRAFT:
            raise HTTPException(status_code=400, detail="Can only attach files to orders in DRAFT status")

        # Verify item exists and belongs to the order
        item = db.query(OcrOrderItem).filter(
            OcrOrderItem.item_id == item_id,
            OcrOrderItem.order_id == order_id
        ).first()
        if not item:
            raise HTTPException(status_code=404, detail="Order item not found")

        # Verify item's document type is AIRWAY_BILL
        doc_type = db.query(DocumentType).filter(DocumentType.doc_type_id == item.doc_type_id).first()
        if not doc_type or doc_type.type_code != "AIRWAY_BILL":
            raise HTTPException(status_code=400, detail="Order item must be of type AIRWAY_BILL to attach monthly invoices")

        # Get S3 manager
        s3_manager = get_s3_manager()
        if not s3_manager:
            raise HTTPException(status_code=500, detail="S3 storage not available")

        # Build existing file keys for deduplication
        existing_file_links = db.query(OrderItemFile).filter(OrderItemFile.item_id == item_id).all()
        existing_keys = set()
        for link in existing_file_links:
            file_info = link.file
            if file_info.s3_key:
                existing_keys.add(file_info.s3_key)
            elif file_info.file_path:
                # For older file records with just file_path
                existing_keys.add(file_info.file_path)

        added_files = 0
        skipped_duplicates = 0

        # NOTE: Monthly bill PDF upload is now handled via "Upload Files" button for consistency
        # The include_bill and monthly_bill_pdf parameters are deprecated and no longer processed here

        # Get invoices for the month from S3
        invoices = s3_manager.list_awb_invoices_for_month(month, debug=debug)
        invoices_found = len(invoices)

        # Enhanced logging for diagnostics
        logger.info(f"📋 Attach-month request: order_id={order_id}, item_id={item_id}, month={month}, "
                   f"invoices_found={invoices_found}, existing_files={len(existing_keys)}, debug={debug}")

        # Collect debug information if requested
        debug_info = None
        if debug:
            debug_info = {
                "s3_bucket": s3_manager.bucket_name,
                "month_format": month,
                "min_file_size_threshold": int(os.getenv('AWB_S3_MIN_FILE_SIZE_BYTES', '10240')),
                "existing_files_count": len(existing_keys),
                "existing_file_keys_sample": list(existing_keys)[:5]  # Show first 5 for inspection
            }

        # If no invoices found, log detailed diagnostics
        if invoices_found == 0:
            logger.warning(f"⚠️  No invoices found for month {month}. Debugging info:")
            logger.warning(f"   - S3 bucket: {s3_manager.bucket_name}")
            logger.warning(f"   - Month format: {month}")
            logger.warning(f"   - Min file size threshold: {os.getenv('AWB_S3_MIN_FILE_SIZE_BYTES', '10240')} bytes")
            logger.warning(f"   - Existing attached files: {len(existing_keys)}")
            logger.warning(f"   - This usually means: files in S3 are too small (<10KB) or don't exist in expected prefixes")
            logger.warning(f"   - Tip: Try /api/awb/trigger-sync?month={month}&force=true to rescan and repair")

        # Attach each invoice to the item
        for invoice in invoices:
            full_key = invoice['full_key']
            invoice_size = invoice.get('size', 0)
            prefix_source = invoice.get('prefix_source', 'unknown')

            # Check if already attached (deduplication)
            if full_key in existing_keys:
                skipped_duplicates += 1
                logger.debug(f"⊘ Invoice already attached, skipping: {full_key}")
                continue

            try:
                # Create File record
                file_name = invoice['key']  # Just the filename without path
                logger.debug(f"📎 Attaching invoice: {file_name} ({invoice_size} bytes) from {prefix_source}")

                invoice_file = DBFile(
                    file_name=file_name,
                    file_path=full_key,  # Full S3 key as the path
                    file_type="pdf",
                    file_size=invoice['size'],
                    mime_type="application/pdf",
                    s3_bucket=s3_manager.bucket_name,
                    s3_key=full_key,
                    source_system="onedrive"
                )
                db.add(invoice_file)
                db.flush()  # Get file_id

                # Link file to order item
                order_item_file = OrderItemFile(
                    item_id=item_id,
                    file_id=invoice_file.file_id,
                    upload_order=item.file_count + added_files + 1
                )
                db.add(order_item_file)
                added_files += 1
                logger.debug(f"✅ Successfully linked invoice to item: {file_name}")

            except Exception as e:
                logger.error(f"❌ Failed to attach invoice {file_name}: {str(e)}")
                # Continue with next invoice instead of failing entire operation
                continue

        # Update item file count and timestamps
        item.file_count += added_files
        item.updated_at = datetime.utcnow()

        # Update order timestamp
        order.updated_at = datetime.utcnow()

        db.commit()

        # Log summary
        logger.info(f"✅ Attach-month completed: month={month}, found={invoices_found}, "
                   f"added={added_files}, duplicates_skipped={skipped_duplicates}")

        response = {
            "success": True,
            "order_id": order_id,
            "item_id": item_id,
            "invoices_found": invoices_found,
            "added_files": added_files,
            "skipped_duplicates": skipped_duplicates,
            "message": f"Attached {added_files} files from {month} ({skipped_duplicates} duplicates skipped)"
        }

        # Add debug information if requested
        if debug and debug_info:
            response["debug"] = debug_info
            # Also include sample invoice keys if found
            if invoices:
                response["debug"]["sample_invoices"] = [
                    {"key": inv['key'], "size": inv['size'], "prefix": inv.get('prefix_source')}
                    for inv in invoices[:3]  # Show first 3
                ]

        return response

    except HTTPException:
        raise
    except Exception as e:
        db.rollback()
        logger.error(f"Failed to attach AWB month to item: {str(e)}")
        raise HTTPException(status_code=500, detail=f"Failed to attach files: {str(e)}")

@app.post("/orders/{order_id}/mapping-file", response_model=dict)
def upload_mapping_file(
    order_id: int,
    mapping_file: UploadFile = File(...),
    db: Session = Depends(get_db)
):
    """Deprecated: order-level mapping uploads are no longer supported."""
    raise HTTPException(
        status_code=410,
        detail="Order-level mapping file uploads have been removed. Configure mapping per order item instead.",
    )

@app.get("/orders/{order_id}/mapping-headers", response_model=dict)
def get_mapping_file_headers(order_id: int, db: Session = Depends(get_db)):
    """Deprecated: order-level mapping headers are no longer available."""
    raise HTTPException(
        status_code=410,
        detail="Order-level mapping headers endpoint has been removed. Configure mapping per order item instead.",
    )

@app.delete("/orders/{order_id}/mapping-file", response_model=dict)
def delete_mapping_file(order_id: int, db: Session = Depends(get_db)):
    """Deprecated: order-level mapping file removal is no longer supported."""
    raise HTTPException(
        status_code=410,
        detail="Order-level mapping file workflow has been removed.",
    )

@app.get("/mapping/master-csv/preview", response_model=dict)
def preview_master_csv(path: str = Query(..., description="OneDrive path to master CSV/Excel"), limit: int = Query(10, ge=1, le=100)):
    """Preview headers and sample rows from a master CSV/Excel stored in OneDrive.

    Lightweight connectivity check used by the UI to help select join keys.
    """
    try:
        processor = OrderProcessor()
        # Reuse internal helper to fetch DataFrame from OneDrive; raises on errors
        df = processor._get_master_csv_dataframe(path)  # pylint: disable=protected-access
        headers = list(df.columns)
        sample = df.head(limit).to_dict(orient="records")
        return {
            "path": path,
            "headers": headers,
            "row_count": int(len(df)),
            "sample": sample,
        }
    except HTTPException:
        raise
    except Exception as exc:
        logger.error("Failed to preview master CSV at %s: %s", path, exc)
        raise HTTPException(status_code=400, detail=f"Failed to preview master CSV: {exc}")

@app.get("/orders/{order_id}/items/{item_id}/download/json")
def download_order_item_json(order_id: int, item_id: int, db: Session = Depends(get_db)):
    """Download OCR result JSON file for a specific order item.

    If an aggregated JSON (including all files for the item) exists, it is preferred.
    Otherwise, falls back to the legacy primary-only JSON stored in ocr_result_json_path.
    """
    try:
        # Verify order and item exist
        order = db.query(OcrOrder).filter(OcrOrder.order_id == order_id).first()
        if not order:
            raise HTTPException(status_code=404, detail="Order not found")

        item = db.query(OcrOrderItem).filter(
            OcrOrderItem.item_id == item_id,
            OcrOrderItem.order_id == order_id
        ).first()
        if not item:
            raise HTTPException(status_code=404, detail="Order item not found")

        # Use existing S3 download infrastructure
        s3_manager = get_s3_manager()
        if not s3_manager:
            raise HTTPException(status_code=500, detail="S3 storage not available")

        file_content = None

        # Prefer aggregated JSON (all files) if available for this item
        try:
            aggregated_key = f"results/orders/{item_id // 1000}/items/{item_id}/item_{item_id}_results.json"
            aggregated_stored_path = f"{s3_manager.upload_prefix}{aggregated_key}"
            file_content = s3_manager.download_file_by_stored_path(aggregated_stored_path)
        except Exception as e:
            logger.warning(f"Failed to load aggregated JSON for item {item_id}: {e}")

        # Fallback to legacy primary-only JSON if aggregated not found
        if not file_content:
            if not item.ocr_result_json_path:
                raise HTTPException(status_code=404, detail="JSON result file not found for this item")

            file_content = s3_manager.download_file_by_stored_path(item.ocr_result_json_path)
            if not file_content:
                raise HTTPException(status_code=404, detail=f"File not found in S3: {item.ocr_result_json_path}")

        # Generate filename for download (combined primary + attachments)
        filename = f"order_{order_id}_item_{item_id}_results.json"

        # Create temporary file to serve
        import tempfile
        import os
        with tempfile.NamedTemporaryFile(delete=False, suffix=".json") as temp_file:
            temp_file.write(file_content)
            temp_file_path = temp_file.name

        # Return file response
        response = FileResponse(
            path=temp_file_path,
            filename=filename,
            media_type="application/json",
        )

        response.headers["X-File-Source"] = "S3"
        return response

    except HTTPException:
        raise
    except Exception as e:
        logger.error(f"Failed to download order item JSON {item_id}: {str(e)}")
        raise HTTPException(status_code=500, detail=f"Failed to download JSON file: {str(e)}")


@app.get("/orders/{order_id}/items/{item_id}/download/csv")
def download_order_item_csv(order_id: int, item_id: int, db: Session = Depends(get_db)):
    """Download OCR result CSV file for a specific order item (using deep flattening).

    If an aggregated JSON (including all files for the item) exists, it is preferred.
    Otherwise, falls back to the legacy primary-only JSON stored in ocr_result_json_path.
    """
    try:
        # Verify order and item exist
        order = db.query(OcrOrder).filter(OcrOrder.order_id == order_id).first()
        if not order:
            raise HTTPException(status_code=404, detail="Order not found")

        item = db.query(OcrOrderItem).filter(
            OcrOrderItem.item_id == item_id,
            OcrOrderItem.order_id == order_id
        ).first()
        if not item:
            raise HTTPException(status_code=404, detail="Order item not found")

        # Use existing S3 download infrastructure to get JSON
        s3_manager = get_s3_manager()
        if not s3_manager:
            raise HTTPException(status_code=500, detail="S3 storage not available")

        json_content = None

        # Prefer aggregated JSON (all files) if available for this item
        try:
            aggregated_key = f"results/orders/{item_id // 1000}/items/{item_id}/item_{item_id}_results.json"
            aggregated_stored_path = f"{s3_manager.upload_prefix}{aggregated_key}"
            json_content = s3_manager.download_file_by_stored_path(aggregated_stored_path)
        except Exception as e:
            logger.warning(f"Failed to load aggregated JSON for CSV of item {item_id}: {e}")

        # Fallback to legacy primary-only JSON if aggregated not found
        if not json_content:
            if not item.ocr_result_json_path:
                raise HTTPException(status_code=404, detail="JSON result file not found for this item")

            json_content = s3_manager.download_file_by_stored_path(item.ocr_result_json_path)
            if not json_content:
                raise HTTPException(status_code=404, detail=f"JSON file not found in S3: {item.ocr_result_json_path}")

        # Parse JSON content
        try:
            json_data = json.loads(json_content.decode('utf-8'))
        except json.JSONDecodeError as e:
            raise HTTPException(status_code=400, detail=f"Invalid JSON format: {str(e)}")

        # Generate CSV using deep flattening
        csv_content = convert_json_to_csv(json_data)
        if not csv_content:
            raise HTTPException(status_code=500, detail="Failed to convert JSON to CSV")

        # Apply Excel formula escaping
        escaped_csv_content = escape_excel_formulas_in_csv(csv_content)

        # Generate filename for download (combined primary + attachments)
        filename = f"order_{order_id}_item_{item_id}_results.csv"

        # Create temporary file with UTF-8 BOM
        import tempfile
        import os
        file_content_with_bom = b'\xef\xbb\xbf' + escaped_csv_content.encode('utf-8')

        with tempfile.NamedTemporaryFile(delete=False, suffix=".csv") as temp_file:
            temp_file.write(file_content_with_bom)
            temp_file_path = temp_file.name

        # Return file response with UTF-8 charset
        response = FileResponse(
            path=temp_file_path,
            filename=filename,
            media_type="text/csv; charset=utf-8",
        )

        response.headers["X-File-Source"] = "freshly_generated"
        return response

    except HTTPException:
        raise
    except Exception as e:
        logger.error(f"Failed to download order item CSV {item_id}: {str(e)}")
        raise HTTPException(status_code=500, detail=f"Failed to download CSV file: {str(e)}")


# ========== PRIMARY CSV HEADERS ENDPOINT ==========
@app.get("/orders/{order_id}/items/{item_id}/primary/csv/headers")
def get_primary_csv_headers(order_id: int, item_id: int, db: Session = Depends(get_db)):
    """Return header names derived ONLY from the primary file's JSON.

    Rationale: the persisted item-level CSV (ocr_result_csv_path) may already
    contain merged attachment columns (e.g. with an "attachment_" prefix). Those
    should not appear in the UI's join-key dropdown, which is meant to select a
    column from the primary file to join with attachment CSVs. To avoid mixing in
    attachment fields, we always deep‑flatten the primary JSON here.
    """
    try:
        # Verify order and item exist
        order = db.query(OcrOrder).filter(OcrOrder.order_id == order_id).first()
        if not order:
            raise HTTPException(status_code=404, detail="Order not found")

        item = db.query(OcrOrderItem).filter(
            OcrOrderItem.item_id == item_id,
            OcrOrderItem.order_id == order_id
        ).first()
        if not item:
            raise HTTPException(status_code=404, detail="Order item not found")

        # Check if item is completed
        if item.status != OrderItemStatus.COMPLETED:
            raise HTTPException(status_code=400, detail="Order item is not completed yet")

        # Always read from the PRIMARY JSON result to avoid polluted headers
        s3_manager = get_s3_manager()
        if not s3_manager:
            raise HTTPException(status_code=500, detail="S3 storage not available")

        if not item.ocr_result_json_path:
            raise HTTPException(status_code=404, detail="Primary JSON result not found")

        json_content = s3_manager.download_file_by_stored_path(item.ocr_result_json_path)
        if not json_content:
            raise HTTPException(status_code=404, detail="Primary JSON result not found")

        # Parse JSON and extract headers using deep flattening
        json_data = json.loads(json_content.decode('utf-8'))

        # Apply deep flattening to get all possible headers (primary only)
        from utils.excel_converter import deep_flatten_json_universal
        flattened_data = deep_flatten_json_universal(json_data)

        headers: List[str] = []
        if flattened_data:
            # Collect all unique field names from flattened records
            all_fields = set()
            for record in flattened_data:
                all_fields.update(record.keys())

            # Filter out internal keys and sort
            headers = sorted([k for k in all_fields if not k.startswith('__')])

        logger.info(f"Got headers from PRIMARY JSON for item {item_id}: {headers}")

        return {
            "item_id": item_id,
            "headers": headers,
            "total_headers": len(headers),
            "source": "json"
        }

    except json.JSONDecodeError as e:
        logger.error(f"Failed to parse JSON headers for item {item_id}: {str(e)}")
        raise HTTPException(status_code=500, detail="Invalid JSON format in primary result")
    except HTTPException:
        raise
    except Exception as e:
        logger.error(f"Failed to get primary CSV headers for item {item_id}: {str(e)}")
        raise HTTPException(status_code=500, detail=f"Failed to get headers: {str(e)}")


# ========== PER-ATTACHMENT CSV DOWNLOAD ENDPOINT ==========
@app.get("/orders/{order_id}/items/{item_id}/files/{file_id}/download/csv")
def download_attachment_csv(order_id: int, item_id: int, file_id: int, db: Session = Depends(get_db)):
    """Download CSV result for a specific attachment file"""
    try:
        # Verify order exists
        order = db.query(OcrOrder).filter(OcrOrder.order_id == order_id).first()
        if not order:
            raise HTTPException(status_code=404, detail="Order not found")

        # Verify item exists and belongs to order
        item = db.query(OcrOrderItem).filter(
            OcrOrderItem.item_id == item_id,
            OcrOrderItem.order_id == order_id
        ).first()
        if not item:
            raise HTTPException(status_code=404, detail="Order item not found")

        # Verify file exists and is linked to the item (but not primary)
        file_link = db.query(OrderItemFile).filter(
            OrderItemFile.item_id == item_id,
            OrderItemFile.file_id == file_id
        ).first()
        if not file_link:
            raise HTTPException(status_code=404, detail="File not linked to this item")

        # Check that this is not the primary file
        if file_id == item.primary_file_id:
            raise HTTPException(status_code=400, detail="Use the item CSV download for primary file results. This endpoint is for attachments only.")

        # Build the stored path for attachment JSON
        stored_path = f"upload/results/orders/{item_id // 1000}/items/{item_id}/files/file_{file_id}_result.json"

        # Download JSON content from S3
        s3_manager = get_s3_manager()
        if not s3_manager:
            raise HTTPException(status_code=500, detail="S3 storage not available")

        file_content = s3_manager.download_file_by_stored_path(stored_path)
        if not file_content:
            raise HTTPException(status_code=404, detail="Attachment JSON result not found")

        # Parse JSON and convert to CSV
        json_data = json.loads(file_content.decode('utf-8'))

        # Convert JSON to single CSV row
        csv_content = convert_json_to_csv(json_data)
        if not csv_content:
            raise HTTPException(status_code=500, detail="Failed to convert JSON to CSV")

        # Apply Excel formula escaping and add UTF-8 BOM
        escaped_csv_content = escape_excel_formulas_in_csv(csv_content)
        file_content_with_bom = b'\xef\xbb\xbf' + escaped_csv_content.encode('utf-8')

        # Generate filename
        filename = f"order_{order_id}_item_{item_id}_attachment_{file_id}_result.csv"

        # Create temporary file
        import tempfile
        import os
        with tempfile.NamedTemporaryFile(delete=False, suffix=".csv") as temp_file:
            temp_file.write(file_content_with_bom)
            temp_file_path = temp_file.name

        # Return file response
        response = FileResponse(
            path=temp_file_path,
            filename=filename,
            media_type="text/csv; charset=utf-8",
        )

        response.headers["X-File-Source"] = "S3"
        return response

    except json.JSONDecodeError as e:
        logger.error(f"Failed to parse JSON from attachment {file_id}: {str(e)}")
        raise HTTPException(status_code=500, detail="Invalid JSON format in attachment result")
    except HTTPException:
        raise
    except Exception as e:
        logger.error(f"Failed to download attachment CSV for file {file_id}: {str(e)}")
        raise HTTPException(status_code=500, detail=f"Failed to download CSV: {str(e)}")


# ========== CSV MERGE BY JOIN KEY ENDPOINT ==========
@app.post("/orders/{order_id}/items/{item_id}/merge/csv")
def merge_csv_by_join_key(
    order_id: int,
    item_id: int,
    join_key: str = Form(..., description="Column name on the primary dataset to join on"),
    join_type: str = Form("left", description="Join mode when merging attachments (left, inner, right, outer)"),
    attachment_join_keys: Optional[str] = Form(
        None,
        description="JSON object mapping attachment identifiers to join key definitions",
    ),
    db: Session = Depends(get_db),
):
    """Merge primary CSV with attachment CSVs using specified join key"""
    try:
        # Verify order and item exist
        order = db.query(OcrOrder).filter(OcrOrder.order_id == order_id).first()
        if not order:
            raise HTTPException(status_code=404, detail="Order not found")

        item = db.query(OcrOrderItem).filter(
            OcrOrderItem.item_id == item_id,
            OcrOrderItem.order_id == order_id
        ).first()
        if not item:
            raise HTTPException(status_code=404, detail="Order item not found")

        # Check if item is completed
        if item.status != OrderItemStatus.COMPLETED:
            raise HTTPException(status_code=400, detail="Order item is not completed yet")

        # Load primary JSON
        if not item.ocr_result_json_path:
            raise HTTPException(status_code=404, detail="Primary JSON result not found")

        s3_manager = get_s3_manager()
        if not s3_manager:
            raise HTTPException(status_code=500, detail="S3 storage not available")

        primary_content = s3_manager.download_file_by_stored_path(item.ocr_result_json_path)
        if not primary_content:
            raise HTTPException(status_code=404, detail="Primary JSON result not found")

        primary_data = json.loads(primary_content.decode('utf-8'))

        allowed_join_types = {"left", "inner", "right", "outer"}
        normalized_join_type = (join_type or "left").lower()
        if normalized_join_type not in allowed_join_types:
            raise HTTPException(
                status_code=400,
                detail=f"Unsupported join_type '{join_type}'. Allowed values: {', '.join(sorted(allowed_join_types))}",
            )

        attachment_join_config: Dict[str, Any] = {}
        if attachment_join_keys:
            try:
                parsed_config = json.loads(attachment_join_keys)
                if not isinstance(parsed_config, dict):
                    raise ValueError("attachment_join_keys must be a JSON object")
                attachment_join_config = parsed_config
            except (json.JSONDecodeError, ValueError) as exc:
                raise HTTPException(
                    status_code=400,
                    detail=f"Failed to parse attachment_join_keys: {exc}",
                )

        def _resolve_attachment_config(identifier: str, index: int) -> Dict[str, Any]:
            config_value: Any = None
            if identifier in attachment_join_config:
                config_value = attachment_join_config[identifier]
            elif str(index) in attachment_join_config:
                config_value = attachment_join_config[str(index)]
            elif "__default__" in attachment_join_config:
                config_value = attachment_join_config["__default__"]

            resolved_primary = join_key
            resolved_attachment = join_key
            resolved_join_type = normalized_join_type

            if isinstance(config_value, str):
                resolved_attachment = config_value
            elif isinstance(config_value, dict):
                if "primary_key" in config_value and config_value["primary_key"]:
                    resolved_primary = str(config_value["primary_key"])
                if "attachment_key" in config_value and config_value["attachment_key"]:
                    resolved_attachment = str(config_value["attachment_key"])
                elif "join_key" in config_value and config_value["join_key"]:
                    resolved_attachment = str(config_value["join_key"])

                custom_join_type = config_value.get("join_type")
                if isinstance(custom_join_type, str):
                    lowered = custom_join_type.lower()
                    if lowered in allowed_join_types:
                        resolved_join_type = lowered
                    else:
                        logger.warning(
                            f"Ignoring unsupported join_type '{custom_join_type}' for attachment {identifier}"
                        )
            elif config_value is not None:
                logger.warning(
                    f"Attachment join configuration for {identifier} must be a string or object; got {type(config_value).__name__}"
                )

            return {
                "primary_key": resolved_primary,
                "attachment_key": resolved_attachment,
                "join_type": resolved_join_type,
            }

        # Get all attachment files for this item
        attachments = db.query(OrderItemFile).filter(
            OrderItemFile.item_id == item_id,
            OrderItemFile.file_id != item.primary_file_id
        ).all()

        # If no attachments, return primary CSV directly
        if not attachments:
            logger.info(f"No attachments found for item {item_id}, returning primary CSV")
            return _generate_primary_csv_response(primary_data, order_id, item_id)

        # Load attachment data
        attachment_contexts = []
        for idx, file_link in enumerate(attachments):
            try:
                attachment_path = f"upload/results/orders/{item_id // 1000}/items/{item_id}/files/file_{file_link.file_id}_result.json"
                attachment_content = s3_manager.download_file_by_stored_path(attachment_path)
                if attachment_content:
                    attachment_data = json.loads(attachment_content.decode('utf-8'))
                    join_config = _resolve_attachment_config(str(file_link.file_id), idx)
                    attachment_contexts.append(
                        {
                            "data": attachment_data,
                            "file_id": file_link.file_id,
                            "primary_key": join_config["primary_key"],
                            "attachment_key": join_config["attachment_key"],
                            "join_type": join_config["join_type"],
                            "label": f"file_{file_link.file_id}",
                        }
                    )
                    logger.info(
                        "Loaded attachment %s for merging using primary_key=%s, attachment_key=%s, join_type=%s",
                        file_link.file_id,
                        join_config["primary_key"],
                        join_config["attachment_key"],
                        join_config["join_type"],
                    )
            except Exception as e:
                logger.warning(f"Failed to load attachment {file_link.file_id}: {e}")

        # Merge data
        merged_data = _merge_json_by_join_key(
            primary_data,
            attachment_contexts,
            join_key,
            join_type=normalized_join_type,
        )

        # Safety improvement: fallback to primary CSV if merge returns empty data
        if not merged_data:
            logger.warning(f"Merge returned no data for item {item_id}, falling back to primary CSV")
            return _generate_primary_csv_response(primary_data, order_id, item_id)

        # Load template for column order if available
        column_order = None
        try:
            if item.document_type and item.document_type.template_json_path:
                file_storage = get_file_storage()
                template_content = file_storage.download_file(item.document_type.template_json_path)
                if template_content:
                    template_data = json.loads(template_content.decode('utf-8'))
                    from utils.template_service import validate_template_payload
                    validate_template_payload(template_data)
                    column_order = template_data.get('column_order')
                    logger.info(f"Loaded template with column_order for item {item_id}")
        except Exception as e:
            logger.warning(f"Failed to load template for item {item_id}: {e}. Using default column order.")

        # Generate CSV content and save it
        csv_content = _render_merged_csv(merged_data, column_order)

        # Save to S3 or local storage
        if is_s3_enabled():
            # Save to S3: key is relative; S3StorageManager will prefix upload/ automatically
            s3_key = f"results/orders/{item_id // 1000}/items/{item_id}/item_{item_id}_merged_by_{join_key}.csv"
            s3_manager = get_s3_manager()
            ok = s3_manager.upload_file(csv_content.encode('utf-8'), s3_key)
            if not ok:
                raise HTTPException(status_code=500, detail="Failed to upload merged CSV to S3")
            # Persist full S3 URI (consistent with rest of pipeline)
            file_path = f"s3://{s3_manager.bucket_name}/{s3_manager.upload_prefix}{s3_key}"
            logger.info(f"Saved merged CSV to S3: {file_path}")
        else:
            # Local storage path (from env)
            base_dir = os.getenv("LOCAL_UPLOAD_DIR")
            if not base_dir:
                raise HTTPException(status_code=500, detail="LOCAL_UPLOAD_DIR not set for local storage")
            local_dir = os.path.join(base_dir, f"orders/{order_id}/items/{item_id}")
            os.makedirs(local_dir, exist_ok=True)
            local_path = os.path.join(local_dir, f"item_{item_id}_merged_by_{join_key}.csv")
            # Optional: add BOM for Excel friendliness
            with open(local_path, 'wb') as f:
                f.write(b'\xef\xbb\xbf' + csv_content.encode('utf-8'))
            file_path = local_path
            logger.info(f"Saved merged CSV to local: {local_path}")

        # Update database
        item.ocr_result_csv_path = file_path
        db.commit()
        logger.info(f"Updated item {item_id} ocr_result_csv_path to: {file_path}")

        # Generate response with CSV and overwrite header
        response = _generate_merged_csv_response(merged_data, order_id, item_id, join_key, column_order)
        response.headers["X-Merged-Saved"] = "true"
        return response

    except json.JSONDecodeError as e:
        logger.error(f"Failed to parse JSON for merge: {str(e)}")
        raise HTTPException(status_code=500, detail="Invalid JSON format")
    except HTTPException:
        raise
    except Exception as e:
        logger.error(f"Failed to merge CSV for item {item_id}: {str(e)}")
        raise HTTPException(status_code=500, detail=f"Failed to merge CSV: {str(e)}")


# ========== CSV UTILITY FUNCTIONS ==========
def convert_json_to_csv(json_data: Union[dict, list]) -> str:
    """Convert JSON data to CSV with deep flattening"""
    if not json_data:
        return ""

    # Filter out internal keys
    if isinstance(json_data, dict):
        filtered_data = {k: v for k, v in json_data.items() if not k.startswith('__')}
    else:
        filtered_data = json_data

    # Import the deep flattening function
    from utils.excel_converter import deep_flatten_json_universal

    # Apply deep flattening
    flattened_data = deep_flatten_json_universal(filtered_data)

    if not flattened_data:
        return ""

    # Get all unique field names across all flattened records
    all_fields = set()
    for record in flattened_data:
        all_fields.update(record.keys())

    # Sort fields consistently
    sorted_fields = sorted(all_fields)

    import csv
    from io import StringIO

    output = StringIO()
    writer = csv.DictWriter(output, fieldnames=sorted_fields)
    writer.writeheader()

    # Write all flattened records
    for record in flattened_data:
        # Convert arrays to strings with pipe separator
        processed_record = {}
        for key, value in record.items():
            if isinstance(value, list):
                # Convert array to pipe-separated string
                processed_record[key] = "|".join(str(v) for v in value)
            elif isinstance(value, dict):
                # Convert dict to JSON string representation
                processed_record[key] = json.dumps(value, ensure_ascii=False)
            else:
                processed_record[key] = value

        writer.writerow(processed_record)

    return output.getvalue()


def escape_excel_formulas_in_csv(csv_content: str) -> str:
    """Escape Excel formulas in CSV content"""
    lines = csv_content.split('\n')
    escaped_lines = []

    for line in lines:
        if line.strip():
            fields = []
            import csv
            reader = csv.reader([line])
            for row in reader:
                for field in row:
                    # Escape = and @ prefixes and leading +/-
                    if isinstance(field, str):
                        if field.startswith('=') or field.startswith('@') or field.startswith('+') or field.startswith('-'):
                            # Wrap in quotes and escape if needed
                            field = f"'{field}"
                        fields.append(field)
                    else:
                        fields.append(str(field))

            # Write escaped line
            escaped_line = ','.join(fields)
            escaped_lines.append(escaped_line)

    return '\n'.join(escaped_lines)


def _is_empty_value(v):
    """Check if a value is considered empty (None/NaN, empty string/whitespace, string形式的null/none/nan, empty list/dict)"""
    import pandas as pd

    if v is None or (isinstance(v, float) and pd.isna(v)):
        return True
    if isinstance(v, str):
        s = v.strip().lower()
        return s == "" or s in {"null", "none", "nan", "n/a", "-", "—"}
    if isinstance(v, (list, dict)):
        return len(v) == 0
    return False


def _series_coalesce(s1, s2):
    """Coalesce two series: prefer non-empty values from primary (s1), then secondary (s2)"""
    # Convert empty values to None, then use combine_first for primary-first priority
    s1c = s1.where(~s1.apply(_is_empty_value), None)
    s2c = s2.where(~s2.apply(_is_empty_value), None)
    return s1c.combine_first(s2c)


def _merge_json_by_join_key(
    primary_data: dict,
    attachment_data_list: list,
    join_key: str,
    *,
    join_type: str = "left",
) -> list:
    """Merge primary and attachment JSON data using join-based approach with per-attachment configuration."""
    try:
        from utils.excel_converter import deep_flatten_json_universal
        import pandas as pd

        # Filter out internal columns starting with __
        def filter_internal_columns(data: dict) -> dict:
            return {k: v for k, v in data.items() if not k.startswith('__')}

        def dataframe_to_rows(df: pd.DataFrame) -> list:
            rows = []
            for _, row in df.iterrows():
                row_dict = {}
                for col, val in row.items():
                    if pd.isna(val):
                        row_dict[col] = None
                    elif isinstance(val, list):
                        row_dict[col] = "|".join(str(v) for v in val)
                    elif isinstance(val, dict):
                        row_dict[col] = json.dumps(val, ensure_ascii=False)
                    else:
                        row_dict[col] = val
                rows.append(row_dict)
            return rows

        allowed_join_types = {"left", "inner", "right", "outer"}
        default_join_type = (join_type or "left").lower()
        if default_join_type not in allowed_join_types:
            logger.warning(
                "Invalid join_type '%s' supplied to _merge_json_by_join_key; defaulting to 'left'",
                join_type,
            )
            default_join_type = "left"

        primary_data_filtered = filter_internal_columns(primary_data)
        flattened_primary = deep_flatten_json_universal(primary_data_filtered)
        if not flattened_primary:
            return []

        # Create primary DataFrame
        df_primary = pd.DataFrame(flattened_primary)

        # Check if join key exists in primary data
        if join_key not in df_primary.columns:
            logger.warning(f"Join key '{join_key}' not found in primary data columns: {list(df_primary.columns)}")
            return []

        # Normalize attachment payloads so each entry has metadata
        attachment_contexts = []
        for idx, entry in enumerate(attachment_data_list or []):
            if isinstance(entry, dict) and "data" in entry:
                attachment_data = entry.get("data")
                primary_key = entry.get("primary_key") or join_key
                attachment_key = entry.get("attachment_key") or primary_key
                context_join_type = entry.get("join_type") or default_join_type
                label = entry.get("label") or f"attachment_{entry.get('file_id', idx + 1)}"
            else:
                attachment_data = entry
                primary_key = join_key
                attachment_key = join_key
                context_join_type = default_join_type
                label = f"attachment_{idx + 1}"

            if attachment_data is None:
                logger.warning(f"Attachment {label} has no data, skipping")
                continue

            normalized_join_type = (
                context_join_type.lower() if isinstance(context_join_type, str) else default_join_type
            )
            if normalized_join_type not in allowed_join_types:
                logger.warning(
                    "Invalid join_type '%s' supplied for %s; defaulting to '%s'",
                    context_join_type,
                    label,
                    default_join_type,
                )
                normalized_join_type = default_join_type

            attachment_contexts.append(
                {
                    "data": attachment_data,
                    "primary_key": primary_key,
                    "attachment_key": attachment_key,
                    "join_type": normalized_join_type,
                    "label": label,
                }
            )

        # No attachments - return primary dataset as rows
        if not attachment_contexts:
            return dataframe_to_rows(df_primary)

        df_current = df_primary.copy()

        for context in attachment_contexts:
            attachment_data = context["data"]
            left_key = context["primary_key"]
            right_key = context["attachment_key"]
            current_join_type = context["join_type"]
            label = context["label"]

            attachment_data_filtered = filter_internal_columns(attachment_data)
            flattened_attachment = deep_flatten_json_universal(attachment_data_filtered)
            if not flattened_attachment:
                logger.warning(f"{label} has no valid data after flattening, skipping")
                continue

            df_attachment = pd.DataFrame(flattened_attachment)

            if left_key not in df_current.columns:
                logger.warning(
                    "Join key '%s' not present in primary dataset when processing %s; skipping attachment",
                    left_key,
                    label,
                )
                continue
            if right_key not in df_attachment.columns:
                logger.warning(
                    "Join key '%s' not present in %s; skipping attachment",
                    right_key,
                    label,
                )
                continue

            overlap = (set(df_current.columns) & set(df_attachment.columns)) - {left_key, right_key}
            suffix = f"__{label}"
            rename_map = {col: f"{col}{suffix}" for col in overlap}
            df_attachment_renamed = df_attachment.rename(columns=rename_map)

            df_merged = pd.merge(
                df_current,
                df_attachment_renamed,
                left_on=left_key,
                right_on=right_key,
                how=current_join_type,
            )

            if left_key != right_key and right_key in df_merged.columns:
                df_merged.drop(columns=[right_key], inplace=True)

            for original_col, renamed_col in rename_map.items():
                if renamed_col in df_merged.columns:
                    df_merged[original_col] = _series_coalesce(df_merged[original_col], df_merged[renamed_col])
                    df_merged.drop(columns=[renamed_col], inplace=True)

            df_current = df_merged

        return dataframe_to_rows(df_current)

    except Exception as e:
        logger.error(f"Error in join-based merging: {str(e)}")
        return []


def _generate_primary_csv_response(primary_data: dict, order_id: int, item_id: int) -> FileResponse:
    """Generate CSV response for primary data only"""
    csv_content = convert_json_to_csv(primary_data)
    escaped_content = escape_excel_formulas_in_csv(csv_content)
    file_content_with_bom = b'\xef\xbb\xbf' + escaped_content.encode('utf-8')

    import tempfile
    import os
    filename = f"order_{order_id}_item_{item_id}_primary.csv"

    with tempfile.NamedTemporaryFile(delete=False, suffix=".csv") as temp_file:
        temp_file.write(file_content_with_bom)
        temp_file_path = temp_file.name

    response = FileResponse(
        path=temp_file_path,
        filename=filename,
        media_type="text/csv; charset=utf-8",
    )

    response.headers["X-File-Source"] = "S3"
    return response


def _generate_merged_csv_response(merged_data: list, order_id: int, item_id: int, join_key: str, column_order: list = None) -> FileResponse:
    """Generate CSV response for merged data with optional template column order"""
    import tempfile
    import os

    # Use the extracted function to generate CSV content
    csv_content = _render_merged_csv(merged_data, column_order)
    file_content_with_bom = csv_content.encode('utf-8')

    filename = f"order_{order_id}_item_{item_id}_merged_by_{join_key}.csv"

    with tempfile.NamedTemporaryFile(delete=False, suffix=".csv") as temp_file:
        temp_file.write(file_content_with_bom)
        temp_file_path = temp_file.name

    response = FileResponse(
        path=temp_file_path,
        filename=filename,
        media_type="text/csv; charset=utf-8",
    )

    response.headers["X-File-Source"] = "S3"
    return response


def _render_merged_csv(merged_data: list, column_order: list = None) -> str:
    """Generate CSV string for merged data with optional template column order"""
    import csv
    from io import StringIO

    output = StringIO()

    if merged_data:
        # Determine all unique columns and filter out internal columns
        all_columns = set()
        for row in merged_data:
            # Filter out internal columns starting with __
            filtered_row = {k: v for k, v in row.items() if not k.startswith('__')}
            all_columns.update(filtered_row.keys())

        if column_order:
            # Use template column order with conflict resolution
            ordered_fields = []

            # First pass: add _1 versions of ordered columns
            for col in column_order:
                col_1 = f"{col}_1"
                if col_1 in all_columns:
                    ordered_fields.append(col_1)

            # Second pass: add _2 versions of ordered columns
            for col in column_order:
                col_2 = f"{col}_2"
                if col_2 in all_columns:
                    ordered_fields.append(col_2)

            # Third pass: add original versions of ordered columns
            for col in column_order:
                if col in all_columns and col not in ordered_fields:
                    ordered_fields.append(col)

            # Add remaining columns that aren't in column_order (alphabetical order)
            remaining_columns = sorted(all_columns - set(ordered_fields))
            final_columns = ordered_fields + remaining_columns
        else:
            # Default alphabetical ordering
            final_columns = sorted(all_columns)

        writer = csv.DictWriter(output, fieldnames=final_columns)
        writer.writeheader()

        # Write rows with internal column filtering
        for row in merged_data:
            filtered_row = {k: v for k, v in row.items() if not k.startswith('__')}
            writer.writerow(filtered_row)

    csv_content = output.getvalue()
    escaped_content = escape_excel_formulas_in_csv(csv_content)
    file_content_with_bom = b'\xef\xbb\xbf' + escaped_content.encode('utf-8')
    return file_content_with_bom.decode('utf-8')


# NOTE: get_suggested_mapping_keys endpoint removed - mapping key recommendation functionality no longer needed


# NOTE: update_default_mapping_keys endpoint removed - mapping key recommendation functionality no longer needed


# =============================================================================
# Auto-Mapping Configuration APIs (Removed)
# =============================================================================

@app.get("/companies/{company_id}/document-types/{doc_type_id}/auto-mapping-config", response_model=dict)
def get_auto_mapping_config(company_id: int, doc_type_id: int):
    raise HTTPException(status_code=410, detail="Auto-mapping configuration has been removed. Use Mapping Templates & Defaults.")


@app.put("/companies/{company_id}/document-types/{doc_type_id}/auto-mapping-config", response_model=dict)
def update_auto_mapping_config(company_id: int, doc_type_id: int):
    raise HTTPException(status_code=410, detail="Auto-mapping configuration has been removed. Use Mapping Templates & Defaults.")


@app.post("/companies/{company_id}/document-types/{doc_type_id}/test-auto-mapping", response_model=dict)
def test_auto_mapping_config(company_id: int, doc_type_id: int):
    """Deprecated: removed."""
    raise HTTPException(status_code=410, detail="Auto-mapping test endpoint has been removed.")


@app.get("/orders/{order_id}/download/mapped-csv")
def download_order_mapped_csv(order_id: int, db: Session = Depends(get_db)):
    """Download final mapped CSV results for order"""
    try:
        # Verify order exists and is completed
        order = db.query(OcrOrder).filter(OcrOrder.order_id == order_id).first()
        if not order:
            raise HTTPException(status_code=404, detail="Order not found")

        if order.status != OrderStatus.COMPLETED:
            raise HTTPException(status_code=400, detail="Order must be completed to download mapped results")

        # Check if mapped CSV exists
        if not order.final_report_paths or 'mapped_csv' not in order.final_report_paths:
            raise HTTPException(status_code=404, detail="No mapped CSV results found for this order")

        mapped_csv_path = order.final_report_paths['mapped_csv']
        if not mapped_csv_path:
            raise HTTPException(status_code=404, detail="Mapped CSV path is empty")

        # Download file from storage
        file_storage = get_file_storage()
        file_content = file_storage.download_file(mapped_csv_path)

        if not file_content:
            raise HTTPException(status_code=500, detail="Failed to download mapped CSV file from storage")

        # Create response with UTF-8 BOM to ensure proper Chinese character encoding
        utf8_bom = b'\xef\xbb\xbf'
        file_content_with_bom = utf8_bom + file_content
        filename = f"order_{order_id}_mapped_results.csv"
        response = Response(
            content=file_content_with_bom,
            media_type="text/csv; charset=utf-8",
            headers={"Content-Disposition": f"attachment; filename={filename}"}
        )

        response.headers["X-File-Source"] = "S3"
        return response

    except HTTPException:
        raise
    except Exception as e:
        logger.error(f"Failed to download order mapped CSV {order_id}: {str(e)}")
        raise HTTPException(status_code=500, detail=f"Failed to download mapped CSV file: {str(e)}")


@app.get("/orders/{order_id}/download/special-csv")
def download_order_special_csv(order_id: int, db: Session = Depends(get_db)):
    """Download special CSV generated from template for an order"""

    try:
        order = db.query(OcrOrder).filter(OcrOrder.order_id == order_id).first()
        if not order:
            raise HTTPException(status_code=404, detail="Order not found")

        if order.status != OrderStatus.COMPLETED:
            raise HTTPException(status_code=400, detail="Order must be completed to download special CSV")

        if not order.final_report_paths or 'special_csv' not in order.final_report_paths:
            raise HTTPException(status_code=404, detail="No special CSV available for this order")

        special_csv_path = order.final_report_paths['special_csv']
        if not special_csv_path:
            raise HTTPException(status_code=404, detail="Special CSV path is empty")

        file_storage = get_file_storage()
        file_content = file_storage.download_file(special_csv_path)

        if not file_content:
            raise HTTPException(status_code=500, detail="Failed to download special CSV from storage")

        # Create response with UTF-8 BOM to ensure proper Chinese character encoding
        utf8_bom = b'\xef\xbb\xbf'
        file_content_with_bom = utf8_bom + file_content
        filename = f"order_{order_id}_special.csv"
        response = Response(
            content=file_content_with_bom,
            media_type="text/csv; charset=utf-8",
            headers={"Content-Disposition": f"attachment; filename={filename}"}
        )
        response.headers["X-File-Source"] = "S3"
        return response

    except HTTPException:
        raise
    except Exception as e:
        logger.error(f"Failed to download order special CSV {order_id}: {str(e)}")
        raise HTTPException(status_code=500, detail=f"Failed to download special CSV file: {str(e)}")


@app.get("/orders/{order_id}/download/mapped-excel")
def download_order_mapped_excel(order_id: int, db: Session = Depends(get_db)):
    """Download final mapped Excel results for order"""
    try:
        # Verify order exists and is completed
        order = db.query(OcrOrder).filter(OcrOrder.order_id == order_id).first()
        if not order:
            raise HTTPException(status_code=404, detail="Order not found")

        if order.status != OrderStatus.COMPLETED:
            raise HTTPException(status_code=400, detail="Order must be completed to download mapped results")

        # Check if mapped Excel exists
        if not order.final_report_paths or 'mapped_excel' not in order.final_report_paths:
            raise HTTPException(status_code=404, detail="No mapped Excel results found for this order")

        mapped_excel_path = order.final_report_paths['mapped_excel']
        if not mapped_excel_path:
            raise HTTPException(status_code=404, detail="Mapped Excel path is empty")

        # Download file from storage
        file_storage = get_file_storage()
        file_content = file_storage.download_file(mapped_excel_path)

        if not file_content:
            raise HTTPException(status_code=500, detail="Failed to download mapped Excel file from storage")

        # Create response
        filename = f"order_{order_id}_mapped_results.xlsx"
        response = Response(
            content=file_content,
            media_type="application/vnd.openxmlformats-officedocument.spreadsheetml.sheet",
            headers={"Content-Disposition": f"attachment; filename={filename}"}
        )

        response.headers["X-File-Source"] = "S3"
        return response

    except HTTPException:
        raise
    except Exception as e:
        logger.error(f"Failed to download order mapped Excel {order_id}: {str(e)}")
        raise HTTPException(status_code=500, detail=f"Failed to download mapped Excel file: {str(e)}")


# ========================================
# 映射历史和版本管理 API 端点 (Phase 3.1)
# ========================================

@app.get("/orders/{order_id}/mapping-history")
def get_mapping_history(
    order_id: int,
    item_id: Optional[int] = Query(None, description="Item ID for item-level history, omit for order-level"),
    limit: int = Query(50, ge=1, le=200, description="Maximum number of records to return"),
    db: Session = Depends(get_db)
):
    """获取映射历史记录"""
    try:
        from utils.mapping_history_manager import MappingHistoryManager

        # 验证order是否存在
        order = db.query(OcrOrder).filter(OcrOrder.order_id == order_id).first()
        if not order:
            raise HTTPException(status_code=404, detail="Order not found")

        # 如果指定了item_id，验证item是否存在且属于该order
        if item_id is not None:
            item = db.query(OcrOrderItem).filter(
                OcrOrderItem.item_id == item_id,
                OcrOrderItem.order_id == order_id
            ).first()
            if not item:
                raise HTTPException(status_code=404, detail="Order item not found")

        history_manager = MappingHistoryManager(db)
        history_records = history_manager.get_mapping_history(order_id, item_id, limit)

        return {
            "order_id": order_id,
            "item_id": item_id,
            "total_records": len(history_records),
            "history": history_records
        }

    except HTTPException:
        raise
    except Exception as e:
        logger.error(f"Failed to get mapping history for order {order_id}: {str(e)}")
        raise HTTPException(status_code=500, detail=f"Failed to get mapping history: {str(e)}")


@app.get("/orders/{order_id}/mapping-history/{version}")
def get_mapping_version(
    order_id: int,
    version: int,
    item_id: Optional[int] = Query(None, description="Item ID for item-level history, omit for order-level"),
    db: Session = Depends(get_db)
):
    """获取特定版本的映射配置"""
    try:
        from utils.mapping_history_manager import MappingHistoryManager

        # 验证order是否存在
        order = db.query(OcrOrder).filter(OcrOrder.order_id == order_id).first()
        if not order:
            raise HTTPException(status_code=404, detail="Order not found")

        history_manager = MappingHistoryManager(db)
        version_data = history_manager.get_mapping_version(order_id, version, item_id)

        if not version_data:
            raise HTTPException(status_code=404, detail=f"Version {version} not found")

        return version_data

    except HTTPException:
        raise
    except Exception as e:
        logger.error(f"Failed to get mapping version {version} for order {order_id}: {str(e)}")
        raise HTTPException(status_code=500, detail=f"Failed to get mapping version: {str(e)}")


@app.get("/orders/{order_id}/mapping-diff/{version1}/{version2}")
def compare_mapping_versions(
    order_id: int,
    version1: int,
    version2: int,
    item_id: Optional[int] = Query(None, description="Item ID for item-level comparison, omit for order-level"),
    db: Session = Depends(get_db)
):
    """比较两个版本的映射配置差异"""
    try:
        from utils.mapping_history_manager import MappingHistoryManager

        # 验证order是否存在
        order = db.query(OcrOrder).filter(OcrOrder.order_id == order_id).first()
        if not order:
            raise HTTPException(status_code=404, detail="Order not found")

        history_manager = MappingHistoryManager(db)
        diff_result = history_manager.compare_versions(order_id, version1, version2, item_id)

        return diff_result

    except HTTPException:
        raise
    except ValueError as e:
        raise HTTPException(status_code=404, detail=str(e))
    except Exception as e:
        logger.error(f"Failed to compare versions {version1} and {version2} for order {order_id}: {str(e)}")
        raise HTTPException(status_code=500, detail=f"Failed to compare versions: {str(e)}")


class MappingRollbackRequest(BaseModel):
    target_version: int
    rollback_reason: Optional[str] = None
    created_by: Optional[str] = None


@app.post("/orders/{order_id}/mapping-rollback")
def rollback_mapping_to_version(
    order_id: int,
    request: MappingRollbackRequest,
    item_id: Optional[int] = Query(None, description="Item ID for item-level rollback, omit for order-level"),
    db: Session = Depends(get_db)
):
    """回滚映射配置到指定版本"""
    try:
        from utils.mapping_history_manager import MappingHistoryManager

        # 验证order是否存在
        order = db.query(OcrOrder).filter(OcrOrder.order_id == order_id).first()
        if not order:
            raise HTTPException(status_code=404, detail="Order not found")

        # 验证order状态 - 只允许在特定状态下回滚
        if order.status not in [OrderStatus.DRAFT, OrderStatus.OCR_COMPLETED, OrderStatus.COMPLETED, OrderStatus.MAPPING]:
            raise HTTPException(
                status_code=400,
                detail=f"Cannot rollback mapping in current order status: {order.status}"
            )

        history_manager = MappingHistoryManager(db)
        success = history_manager.rollback_to_version(
            order_id=order_id,
            target_version=request.target_version,
            item_id=item_id,
            created_by=request.created_by,
            rollback_reason=request.rollback_reason
        )

        if success:
            # 刷新order数据
            db.refresh(order)
            return {
                "success": True,
                "message": f"Successfully rolled back to version {request.target_version}",
                "current_mapping_keys": order.mapping_keys
            }
        else:
            raise HTTPException(status_code=500, detail="Rollback operation failed")

    except HTTPException:
        raise
    except Exception as e:
        logger.error(f"Failed to rollback order {order_id} to version {request.target_version}: {str(e)}")
        raise HTTPException(status_code=500, detail=f"Failed to rollback mapping: {str(e)}")


# =============================================================================
# Order Management APIs (Lock/Unlock, Restart OCR/Mapping)
# =============================================================================

@app.post("/orders/{order_id}/lock")
def lock_order(order_id: int, db: Session = Depends(get_db)):
    """Lock an order to prevent further modifications"""
    try:
        order = db.query(OcrOrder).filter(OcrOrder.order_id == order_id).first()
        if not order:
            raise HTTPException(status_code=404, detail="Order not found")

        if order.status == OrderStatus.LOCKED:
            return {"message": "Order is already locked", "status": "LOCKED"}

        order.status = OrderStatus.LOCKED
        order.updated_at = datetime.utcnow()
        db.commit()

        logger.info(f"Order {order_id} locked successfully")
        return {"message": "Order locked successfully", "status": "LOCKED"}

    except HTTPException:
        raise
    except Exception as e:
        logger.error(f"Failed to lock order {order_id}: {str(e)}")
        raise HTTPException(status_code=500, detail=f"Failed to lock order: {str(e)}")


@app.post("/orders/{order_id}/unlock")
def unlock_order(order_id: int, db: Session = Depends(get_db)):
    """Unlock an order to allow modifications"""
    try:
        order = db.query(OcrOrder).filter(OcrOrder.order_id == order_id).first()
        if not order:
            raise HTTPException(status_code=404, detail="Order not found")

        if order.status != OrderStatus.LOCKED:
            return {"message": "Order is not locked", "status": order.status.value}

        # Determine appropriate status based on order completion
        if order.completed_items == order.total_items and order.total_items > 0:
            new_status = OrderStatus.COMPLETED
        elif order.completed_items > 0:
            new_status = OrderStatus.OCR_COMPLETED
        else:
            new_status = OrderStatus.DRAFT

        order.status = new_status
        order.updated_at = datetime.utcnow()
        db.commit()

        logger.info(f"Order {order_id} unlocked successfully, status set to {new_status.value}")
        return {"message": "Order unlocked successfully", "status": new_status.value}

    except HTTPException:
        raise
    except Exception as e:
        logger.error(f"Failed to unlock order {order_id}: {str(e)}")
        raise HTTPException(status_code=500, detail=f"Failed to unlock order: {str(e)}")


@app.post("/orders/{order_id}/restart-ocr")
def restart_ocr_processing(order_id: int, db: Session = Depends(get_db)):
    """Restart OCR processing for an order"""
    try:
        order = db.query(OcrOrder).filter(OcrOrder.order_id == order_id).first()
        if not order:
            raise HTTPException(status_code=404, detail="Order not found")

        if order.status == OrderStatus.LOCKED:
            raise HTTPException(status_code=400, detail="Cannot restart OCR for locked order")

        # Reset order status and counters
        order.status = OrderStatus.PROCESSING
        order.completed_items = 0
        order.failed_items = 0
        order.error_message = None
        order.updated_at = datetime.utcnow()

        # Reset all order items to PENDING
        for item in order.items:
            item.status = OrderItemStatus.PENDING
            item.ocr_result_json_path = None
            item.ocr_result_csv_path = None
            item.error_message = None
            item.processing_started_at = None
            item.processing_completed_at = None
            item.processing_time_seconds = None
            item.updated_at = datetime.utcnow()

        db.commit()

        # Trigger OCR processing
        from utils.order_processor import OrderProcessor
        processor = OrderProcessor()

        # Process asynchronously
        import asyncio
        def run_async():
            loop = asyncio.new_event_loop()
            asyncio.set_event_loop(loop)
            loop.run_until_complete(processor.process_order(order_id))
            loop.close()

        import threading
        thread = threading.Thread(target=run_async)
        thread.start()

        logger.info(f"Order {order_id} OCR processing restarted")
        return {"message": "OCR processing restarted successfully", "order_id": order_id, "status": "PROCESSING"}

    except HTTPException:
        raise
    except Exception as e:
        logger.error(f"Failed to restart OCR for order {order_id}: {str(e)}")
        raise HTTPException(status_code=500, detail=f"Failed to restart OCR: {str(e)}")


@app.post("/orders/{order_id}/restart-mapping")
def restart_mapping_processing(order_id: int, db: Session = Depends(get_db)):
    """Restart mapping processing for an order"""
    try:
        order = db.query(OcrOrder).filter(OcrOrder.order_id == order_id).first()
        if not order:
            raise HTTPException(status_code=404, detail="Order not found")

        if order.status == OrderStatus.LOCKED:
            raise HTTPException(status_code=400, detail="Cannot restart mapping for locked order")

        if order.status not in [OrderStatus.OCR_COMPLETED, OrderStatus.MAPPING, OrderStatus.COMPLETED, OrderStatus.FAILED]:
            raise HTTPException(
                status_code=400,
                detail=f"Cannot restart mapping for order in {order.status} status"
            )

        # Reset mapping-related fields
        order.status = OrderStatus.MAPPING
        order.final_report_paths = None
        order.error_message = None
        order.updated_at = datetime.utcnow()

        db.commit()

        # Enhanced logging: Log order details before processing
        logger.info(f"🚀 Order {order_id} mapping processing restarted - Enhanced logging enabled")
        logger.info(
            "   Order details: name='%s', status='%s', primary_doc_type_id=%s",
            order.order_name,
            order.status,
            order.primary_doc_type_id,
        )
        logger.info("   Final reports before restart: %s", order.final_report_paths)

        item_config_count = db.query(OcrOrderItem).filter(
            OcrOrderItem.order_id == order_id,
            OcrOrderItem.mapping_config.isnot(None)
        ).count()
        logger.info("   Items with stored mapping config: %s", item_config_count)

        # Log template details if available
        if order.primary_doc_type_id:
            template_path = order.primary_doc_type.template_json_path if order.primary_doc_type else None
            logger.info(f"   Template path: {template_path}")
            logger.info(f"   Document type: {order.primary_doc_type.type_name if order.primary_doc_type else 'Unknown'}")

        # Trigger mapping processing
        from utils.order_processor import OrderProcessor
        processor = OrderProcessor()
        logger.info(f"   OrderProcessor initialized, starting async processing...")

        # Process asynchronously with enhanced logging
        import asyncio
        def run_async():
            logger.info(f"   Starting async processing for order {order_id}")
            loop = asyncio.new_event_loop()
            asyncio.set_event_loop(loop)
            try:
                loop.run_until_complete(processor.process_order_mapping_only(order_id))
                logger.info(f"   Async processing completed for order {order_id}")
            except Exception as e:
                logger.error(f"   Async processing failed for order {order_id}: {str(e)}")
                raise
            finally:
                loop.close()

        import threading
        thread = threading.Thread(target=run_async)
        thread.start()

        logger.info(f"✅ Order {order_id} mapping processing restarted - background thread started")
        return {"message": "Mapping processing restarted successfully", "order_id": order_id, "status": "MAPPING"}

    except HTTPException:
        raise
    except Exception as e:
        logger.error(f"Failed to restart mapping for order {order_id}: {str(e)}")
        raise HTTPException(status_code=500, detail=f"Failed to restart mapping: {str(e)}")


@app.get("/orders/{order_id}/mapping-statistics")
def get_mapping_statistics(
    order_id: int,
    item_id: Optional[int] = Query(None, description="Item ID for item-level statistics, omit for order-level"),
    db: Session = Depends(get_db)
):
    """获取映射历史统计信息"""
    try:
        from utils.mapping_history_manager import MappingHistoryManager

        # 验证order是否存在
        order = db.query(OcrOrder).filter(OcrOrder.order_id == order_id).first()
        if not order:
            raise HTTPException(status_code=404, detail="Order not found")

        history_manager = MappingHistoryManager(db)
        statistics = history_manager.get_mapping_statistics(order_id, item_id)

        return {
            "order_id": order_id,
            "item_id": item_id,
            "statistics": statistics
        }

    except HTTPException:
        raise
    except Exception as e:
        logger.error(f"Failed to get mapping statistics for order {order_id}: {str(e)}")
        raise HTTPException(status_code=500, detail=f"Failed to get mapping statistics: {str(e)}")


# 映射键更新时自动创建历史记录的辅助函数
def create_mapping_history_on_update(
    db: Session,
    order_id: int,
    new_mapping_keys: List[str],
    operation_type: str = "UPDATE",
    item_id: Optional[int] = None,
    operation_reason: Optional[str] = None,
    created_by: Optional[str] = None
):
    """在映射键更新时自动创建历史记录"""
    try:
        from utils.mapping_history_manager import MappingHistoryManager, MappingOperation

        history_manager = MappingHistoryManager(db)

        # 将字符串转换为MappingOperation枚举
        if operation_type == "UPDATE":
            op_type = MappingOperation.UPDATE
        elif operation_type == "CREATE":
            op_type = MappingOperation.CREATE
        elif operation_type == "APPLY_RECOMMENDATION":
            op_type = MappingOperation.APPLY_RECOMMENDATION
        else:
            op_type = MappingOperation.UPDATE

        history_manager.create_mapping_version(
            order_id=order_id,
            mapping_keys=new_mapping_keys,
            operation_type=op_type,
            item_id=item_id,
            operation_reason=operation_reason,
            created_by=created_by
        )

        logger.info(f"Created mapping history record for order {order_id}, operation: {operation_type}")

    except Exception as e:
        logger.error(f"Failed to create mapping history record: {str(e)}")
        # 不抛出异常，避免影响主要的映射更新操作


# =======================
# 批量映射管理 API 端点
# =======================

class BulkMappingPreviewRequest(BaseModel):
    target_orders: Optional[List[int]] = None
    new_mapping_keys: Optional[List[str]] = None
    filter_criteria: Optional[dict] = None
    operation_type: str = "update"


class BulkMappingUpdateRequest(BaseModel):
    target_orders: Optional[List[int]] = None
    new_mapping_keys: Optional[List[str]] = None
    filter_criteria: Optional[dict] = None
    operation_type: str = "update"
    operation_reason: Optional[str] = None
    created_by: Optional[str] = None
    confirm_changes: bool = False


class BulkRollbackRequest(BaseModel):
    target_orders: List[int]
    target_version: Optional[int] = None
    rollback_to_date: Optional[str] = None
    created_by: Optional[str] = None
    rollback_reason: Optional[str] = None
    confirm_rollback: bool = False


@app.post("/mapping/bulk/preview")
async def preview_bulk_mapping_update(request: BulkMappingPreviewRequest, db: Session = Depends(get_db)):
    """预览批量映射更新"""
    try:
        from utils.bulk_mapping_manager import BulkMappingManager

        bulk_manager = BulkMappingManager(db)

        preview_result = bulk_manager.preview_bulk_mapping_update(
            target_orders=request.target_orders,
            new_mapping_keys=request.new_mapping_keys,
            filter_criteria=request.filter_criteria,
            operation_type=request.operation_type
        )

        return {
            "success": True,
            "preview": {
                "affected_orders": preview_result.affected_orders,
                "summary": preview_result.summary,
                "warnings": preview_result.warnings,
                "errors": preview_result.errors
            }
        }

    except Exception as e:
        logger.error(f"Failed to preview bulk mapping update: {str(e)}")
        raise HTTPException(status_code=500, detail=f"Failed to preview bulk mapping update: {str(e)}")


@app.post("/mapping/bulk/execute")
async def execute_bulk_mapping_update(request: BulkMappingUpdateRequest, db: Session = Depends(get_db)):
    """执行批量映射更新"""
    try:
        from utils.bulk_mapping_manager import BulkMappingManager

        bulk_manager = BulkMappingManager(db)

        result = bulk_manager.execute_bulk_mapping_update(
            target_orders=request.target_orders,
            new_mapping_keys=request.new_mapping_keys,
            filter_criteria=request.filter_criteria,
            operation_type=request.operation_type,
            operation_reason=request.operation_reason,
            created_by=request.created_by,
            confirm_changes=request.confirm_changes
        )

        return result

    except Exception as e:
        logger.error(f"Failed to execute bulk mapping update: {str(e)}")
        raise HTTPException(status_code=500, detail=f"Failed to execute bulk mapping update: {str(e)}")


@app.post("/mapping/bulk/rollback")
async def bulk_rollback_orders(request: BulkRollbackRequest, db: Session = Depends(get_db)):
    """批量回滚订单映射"""
    try:
        from utils.bulk_mapping_manager import BulkMappingManager

        bulk_manager = BulkMappingManager(db)

        result = bulk_manager.bulk_rollback_orders(
            target_orders=request.target_orders,
            target_version=request.target_version,
            rollback_to_date=request.rollback_to_date,
            created_by=request.created_by,
            rollback_reason=request.rollback_reason,
            confirm_rollback=request.confirm_rollback
        )

        return result

    except Exception as e:
        logger.error(f"Failed to execute bulk rollback: {str(e)}")
        raise HTTPException(status_code=500, detail=f"Failed to execute bulk rollback: {str(e)}")


@app.get("/mapping/bulk/candidates")
async def get_bulk_operation_candidates(
    operation_type: str = Query("all", description="操作类型"),
    include_completed_only: bool = Query(True, description="只包含已完成的订单"),
    min_items: int = Query(1, description="最小项目数量"),
    db: Session = Depends(get_db)
):
    """获取适合批量操作的订单候选列表"""
    try:
        from utils.bulk_mapping_manager import BulkMappingManager

        bulk_manager = BulkMappingManager(db)

        candidates = bulk_manager.get_bulk_operation_candidates(
            operation_type=operation_type,
            include_completed_only=include_completed_only,
            min_items=min_items
        )

        return {
            "success": True,
            "total_candidates": len(candidates),
            "candidates": candidates
        }

    except Exception as e:
        logger.error(f"Failed to get bulk operation candidates: {str(e)}")
        raise HTTPException(status_code=500, detail=f"Failed to get bulk operation candidates: {str(e)}")


# =======================
# 智能路径管理 API 端点
# =======================

class PathGenerationRequest(BaseModel):
    category: str
    company_id: int
    doc_type_id: Optional[int] = None
    identifier: Optional[str] = None
    filename: Optional[str] = None
    template: Optional[str] = "BASIC"
    conflict_strategy: Optional[str] = "AUTO_RENAME"
    custom_vars: Optional[dict] = None


class PathValidationRequest(BaseModel):
    path: str


class PathMigrationRequest(BaseModel):
    source_pattern: str
    target_template: str
    dry_run: bool = True


@app.post("/paths/generate")
async def generate_smart_path(request: PathGenerationRequest, db: Session = Depends(get_db)):
    """生成智能路径"""
    try:
        from utils.smart_path_manager import SmartPathManager, PathContext, PathTemplate, PathConflictStrategy

        path_manager = SmartPathManager(db)

        # 获取公司和文档类型信息
        from sqlalchemy import text as sql_text
        company_query = sql_text("SELECT company_code FROM companies WHERE company_id = :company_id")
        company_result = db.execute(company_query, {"company_id": request.company_id})
        company_row = company_result.fetchone()
        if not company_row:
            raise HTTPException(status_code=404, detail="Company not found")
        company_code = company_row[0]

        doc_type_code = None
        if request.doc_type_id:
            doc_type_query = sql_text("SELECT type_code FROM document_types WHERE doc_type_id = :doc_type_id")
            doc_type_result = db.execute(doc_type_query, {"doc_type_id": request.doc_type_id})
            doc_type_row = doc_type_result.fetchone()
            if doc_type_row:
                doc_type_code = doc_type_row[0]

        # 创建路径上下文
        context = PathContext(
            category=request.category,
            company_id=request.company_id,
            company_code=company_code,
            doc_type_id=request.doc_type_id,
            doc_type_code=doc_type_code,
            identifier=request.identifier,
            filename=request.filename,
            custom_vars=request.custom_vars
        )

        # 解析模板和冲突策略
        template = getattr(PathTemplate, request.template, PathTemplate.BASIC)
        conflict_strategy = getattr(PathConflictStrategy, request.conflict_strategy, PathConflictStrategy.AUTO_RENAME)

        # 生成路径
        generated_path = path_manager.generate_smart_path(context, template, conflict_strategy)

        return {
            "success": True,
            "generated_path": generated_path,
            "context": {
                "category": context.category,
                "company_code": context.company_code,
                "doc_type_code": context.doc_type_code,
                "template": request.template,
                "conflict_strategy": request.conflict_strategy
            }
        }

    except Exception as e:
        logger.error(f"Failed to generate smart path: {str(e)}")
        raise HTTPException(status_code=500, detail=f"Failed to generate smart path: {str(e)}")


@app.post("/paths/validate")
async def validate_path_structure(request: PathValidationRequest, db: Session = Depends(get_db)):
    """验证路径结构"""
    try:
        from utils.smart_path_manager import SmartPathManager

        path_manager = SmartPathManager(db)
        validation_result = path_manager.validate_path_structure(request.path)

        return {
            "success": True,
            "validation": {
                "is_valid": validation_result.is_valid,
                "path": validation_result.path,
                "conflicts": validation_result.conflicts,
                "suggestions": validation_result.suggestions,
                "metadata": validation_result.metadata
            }
        }

    except Exception as e:
        logger.error(f"Failed to validate path: {str(e)}")
        raise HTTPException(status_code=500, detail=f"Failed to validate path: {str(e)}")


@app.post("/paths/migrate")
async def migrate_legacy_paths(request: PathMigrationRequest, db: Session = Depends(get_db)):
    """迁移历史路径"""
    try:
        from utils.smart_path_manager import SmartPathManager, PathTemplate

        path_manager = SmartPathManager(db)

        # 解析目标模板
        target_template = getattr(PathTemplate, request.target_template, PathTemplate.BASIC)

        # 执行迁移
        migration_result = path_manager.migrate_legacy_paths(
            source_pattern=request.source_pattern,
            target_template=target_template,
            dry_run=request.dry_run
        )

        return {
            "success": True,
            "migration": migration_result
        }

    except Exception as e:
        logger.error(f"Failed to migrate paths: {str(e)}")
        raise HTTPException(status_code=500, detail=f"Failed to migrate paths: {str(e)}")


@app.get("/paths/analytics")
async def get_path_analytics(
    category: Optional[str] = Query(None, description="路径类别过滤"),
    db: Session = Depends(get_db)
):
    """获取路径分析统计"""
    try:
        from utils.smart_path_manager import SmartPathManager

        path_manager = SmartPathManager(db)
        analytics = path_manager.get_path_analytics(category)

        return {
            "success": True,
            "analytics": analytics
        }

    except Exception as e:
        logger.error(f"Failed to get path analytics: {str(e)}")
        raise HTTPException(status_code=500, detail=f"Failed to get path analytics: {str(e)}")


@app.get("/paths/templates")
async def get_available_templates():
    """获取可用的路径模板"""
    try:
        from utils.smart_path_manager import PathTemplate, PathConflictStrategy

        templates = []
        for template in PathTemplate:
            templates.append({
                "name": template.name,
                "value": template.value,
                "description": f"Template: {template.value}"
            })

        strategies = []
        for strategy in PathConflictStrategy:
            strategies.append({
                "name": strategy.name,
                "value": strategy.value,
                "description": f"Strategy: {strategy.value}"
            })

        return {
            "success": True,
            "templates": templates,
            "conflict_strategies": strategies
        }

    except Exception as e:
        logger.error(f"Failed to get templates: {str(e)}")
        raise HTTPException(status_code=500, detail=f"Failed to get templates: {str(e)}")


# =======================
# 高级映射分析工具 API 端点
# =======================

class AnalysisRequest(BaseModel):
    analysis_period_days: int = 30
    include_historical: bool = True
    focus_areas: Optional[List[str]] = None


@app.post("/analysis/comprehensive")
async def generate_comprehensive_analysis(request: AnalysisRequest, db: Session = Depends(get_db)):
    """生成综合映射分析报告"""
    try:
        from utils.advanced_mapping_analyzer import AdvancedMappingAnalyzer

        analyzer = AdvancedMappingAnalyzer(db)

        report = analyzer.generate_comprehensive_analysis(
            analysis_period_days=request.analysis_period_days,
            include_historical=request.include_historical,
            focus_areas=request.focus_areas
        )

        return {
            "success": True,
            "report": {
                "report_id": report.report_id,
                "generated_at": report.generated_at.isoformat(),
                "analysis_period": {
                    "start": report.analysis_period[0].isoformat(),
                    "end": report.analysis_period[1].isoformat()
                },
                "summary": report.summary,
                "insights": [
                    {
                        "type": insight.insight_type,
                        "title": insight.title,
                        "description": insight.description,
                        "impact_score": insight.impact_score,
                        "recommendations": insight.recommendations,
                        "affected_items": insight.affected_items,
                        "data_points": insight.data_points
                    }
                    for insight in report.insights
                ],
                "patterns": [
                    {
                        "mapping_key": pattern.mapping_key,
                        "frequency": pattern.frequency,
                        "companies": list(pattern.companies),
                        "doc_types": list(pattern.doc_types),
                        "success_rate": pattern.success_rate,
                        "avg_processing_time": pattern.avg_processing_time,
                        "last_used": pattern.last_used.isoformat() if pattern.last_used else None
                    }
                    for pattern in report.patterns
                ],
                "recommendations": report.recommendations,
                "metrics": report.metrics
            }
        }

    except Exception as e:
        logger.error(f"Failed to generate comprehensive analysis: {str(e)}")
        raise HTTPException(status_code=500, detail=f"Failed to generate comprehensive analysis: {str(e)}")


@app.get("/analysis/trends")
async def analyze_mapping_trends(
    days_back: int = Query(90, description="分析天数"),
    db: Session = Depends(get_db)
):
    """分析映射使用趋势"""
    try:
        from utils.advanced_mapping_analyzer import AdvancedMappingAnalyzer

        analyzer = AdvancedMappingAnalyzer(db)
        trends = analyzer.analyze_mapping_trends(days_back=days_back)

        return {
            "success": True,
            "trends": trends
        }

    except Exception as e:
        logger.error(f"Failed to analyze mapping trends: {str(e)}")
        raise HTTPException(status_code=500, detail=f"Failed to analyze mapping trends: {str(e)}")


@app.get("/analysis/optimization")
async def get_optimization_suggestions(db: Session = Depends(get_db)):
    """获取优化建议"""
    try:
        from utils.advanced_mapping_analyzer import AdvancedMappingAnalyzer

        analyzer = AdvancedMappingAnalyzer(db)
        suggestions = analyzer.generate_optimization_suggestions()

        return {
            "success": True,
            "suggestions": suggestions
        }

    except Exception as e:
        logger.error(f"Failed to get optimization suggestions: {str(e)}")
        raise HTTPException(status_code=500, detail=f"Failed to get optimization suggestions: {str(e)}")


@app.get("/analysis/dashboard")
async def get_analysis_dashboard(db: Session = Depends(get_db)):
    """获取分析仪表板数据"""
    try:
        from utils.advanced_mapping_analyzer import AdvancedMappingAnalyzer

        analyzer = AdvancedMappingAnalyzer(db)

        # 获取最近7天的快速分析
        quick_analysis = analyzer.generate_comprehensive_analysis(analysis_period_days=7)
        trends = analyzer.analyze_mapping_trends(days_back=30)
        suggestions = analyzer.generate_optimization_suggestions()

        dashboard_data = {
            "quick_metrics": quick_analysis.summary,
            "recent_insights": quick_analysis.insights[:3],  # 只显示前3个洞察
            "trends_summary": {
                "total_days": len(trends.get('daily_stats', [])),
                "trend_analysis": trends.get('trend_analysis', {}),
                "recent_performance": trends.get('daily_stats', [])[-7:] if trends.get('daily_stats') else []
            },
            "priority_suggestions": [s for s in suggestions if s.get('priority') == 'high'][:3],
            "top_patterns": [
                {
                    "mapping_key": pattern.mapping_key,
                    "frequency": pattern.frequency,
                    "success_rate": pattern.success_rate
                }
                for pattern in quick_analysis.patterns[:5]
            ]
        }

        return {
            "success": True,
            "dashboard": dashboard_data
        }

    except Exception as e:
        logger.error(f"Failed to get analysis dashboard: {str(e)}")
        raise HTTPException(status_code=500, detail=f"Failed to get analysis dashboard: {str(e)}")


# ===== AWB Processing Endpoints =====

async def trigger_onedrive_sync_async():
    """Trigger OneDrive sync in background (fire-and-forget)"""
    try:
        onedrive_enabled = os.getenv('ONEDRIVE_SYNC_ENABLED', 'false').lower() == 'true'
        if not onedrive_enabled:
            logger.warning("⚠️ OneDrive sync is disabled")
            return

        from scripts.onedrive_ingest import run_onedrive_sync as sync_func
        logger.info("🔄 Triggering OneDrive sync in background...")
        sync_func()
        logger.info("✅ OneDrive sync completed")

    except Exception as e:
        logger.error(f"❌ Error in OneDrive sync: {e}", exc_info=True)

@app.post("/api/awb/process-monthly")
async def process_monthly_awb(
    company_id: int = Form(...),
    month: str = Form(...),
    monthly_bill_pdf: UploadFile = File(None),
    summary_pdf: UploadFile = File(None),
    employees_csv: UploadFile = File(None),
    background_tasks: BackgroundTasks = None,
    db: Session = Depends(get_db)
):
    """Process monthly AWB files - Orders-based pipeline with S3 invoice discovery"""
    try:
        # Validate inputs
        if not month or '-' not in month:
            raise HTTPException(status_code=400, detail="Month must be in YYYY-MM format")

        # Verify company exists
        company = db.query(Company).filter(Company.company_id == company_id).first()
        if not company:
            raise HTTPException(status_code=404, detail=f"Company {company_id} not found")

        # Use monthly_bill_pdf if provided, fallback to summary_pdf for backward compat
        bill_pdf = monthly_bill_pdf or summary_pdf
        if not bill_pdf:
            raise HTTPException(status_code=400, detail="Either monthly_bill_pdf or summary_pdf is required")

        # Find or create AIRWAY_BILL document type
        doc_type = db.query(DocumentType).filter(
            DocumentType.type_code == "AIRWAY_BILL"
        ).first()
        if not doc_type:
            raise HTTPException(status_code=404, detail="Document type AIRWAY_BILL not found. Please create it in admin.")

        # Create OCR Order
        order = OcrOrder(
            order_name=f"AWB {month}",
            status=OrderStatus.DRAFT,
            primary_doc_type_id=doc_type.doc_type_id
        )
        db.add(order)
        db.commit()
        db.refresh(order)
        order_id = order.order_id
        logger.info(f"✅ Created OCR Order {order_id} for AWB {month}")

        # Upload bill PDF to S3
        s3_manager = get_s3_manager()
        if not s3_manager:
            raise HTTPException(status_code=500, detail="S3 storage not available")

        timestamp = datetime.utcnow().strftime('%Y%m%d_%H%M%S')
        bill_content = await bill_pdf.read()
        bill_s3_key = f"upload/awb/monthly/{month}/summary_{timestamp}.pdf"

        s3_manager.put_object(bill_s3_key, bill_content, content_type='application/pdf')
        logger.info(f"✅ Uploaded monthly bill PDF: {bill_s3_key}")

        # Create File record for bill
        file_record = File(
            file_name=bill_pdf.filename or f"summary_{timestamp}.pdf",
            file_path=bill_s3_key,
            file_type="pdf",
            file_size=len(bill_content),
            mime_type="application/pdf",
            s3_bucket=s3_manager.bucket_name,
            s3_key=bill_s3_key,
            source_system="upload"
        )
        db.add(file_record)
        db.commit()
        db.refresh(file_record)

        # Create order item for bill
        bill_item = OcrOrderItem(
            order_id=order_id,
            company_id=company_id,
            doc_type_id=doc_type.doc_type_id,
            item_name=f"Monthly Bill {month}",
            status=OrderItemStatus.PENDING,
            file_count=1
        )
        db.add(bill_item)
        db.commit()
        db.refresh(bill_item)

        # Attach bill file to item
        order_item_file = OrderItemFile(
            item_id=bill_item.item_id,
            file_id=file_record.file_id,
            upload_order=1
        )
        db.add(order_item_file)
        db.commit()
        logger.info(f"✅ Created order item for bill with file attachment")

        # Discover invoice PDFs from S3
        invoices = []
        if s3_manager.list_awb_invoices_for_month:
            invoices = s3_manager.list_awb_invoices_for_month(month)
            logger.info(f"🔍 Found {len(invoices)} invoice PDFs for month {month}")

            # Create order items for each invoice and attach files (no re-upload)
            for idx, invoice in enumerate(invoices, 1):
                try:
                    # Get invoice size and create File record (referencing existing S3 file)
                    invoice_file = File(
                        file_name=invoice["key"],
                        file_path=invoice["full_key"],
                        file_type="pdf",
                        file_size=invoice["size"],
                        mime_type="application/pdf",
                        s3_bucket=s3_manager.bucket_name,
                        s3_key=invoice["full_key"],
                        source_system="onedrive",
                        source_path=f"onedrive://{invoice['full_key']}"
                    )
                    db.add(invoice_file)
                    db.commit()
                    db.refresh(invoice_file)

                    # Create order item for invoice
                    invoice_item = OcrOrderItem(
                        order_id=order_id,
                        company_id=company_id,
                        doc_type_id=doc_type.doc_type_id,
                        item_name=invoice["key"],
                        status=OrderItemStatus.PENDING,
                        file_count=1
                    )
                    db.add(invoice_item)
                    db.commit()
                    db.refresh(invoice_item)

                    # Attach invoice file to item
                    invoice_order_item_file = OrderItemFile(
                        item_id=invoice_item.item_id,
                        file_id=invoice_file.file_id,
                        upload_order=idx
                    )
                    db.add(invoice_order_item_file)
                    db.commit()
                    logger.info(f"✅ Attached invoice {idx}: {invoice['key']}")

                except Exception as e:
                    logger.warning(f"⚠️ Failed to process invoice {invoice['key']}: {e}")
                    continue

        # Update order with total items
        order.total_items = 1 + len(invoices)
        db.commit()

        # Submit order to processing pipeline if we have invoices or bill
        if 1 + len(invoices) > 0:
            order.status = OrderStatus.PROCESSING
            db.commit()
            logger.info(f"✅ Order {order_id} submitted to processing pipeline")

        # If no invoices found, trigger OneDrive sync as fallback
        if len(invoices) == 0:
            logger.info(f"ℹ️ No invoices found in S3 for {month}, triggering OneDrive sync as fallback...")
            # Fire-and-forget OneDrive sync
            if background_tasks:
                background_tasks.add_task(trigger_onedrive_sync_async)
            message = f"Order {order_id} created. No invoices found in S3; OneDrive sync triggered. Invoices will attach as they appear."
        else:
            message = f"Order {order_id} created with {len(invoices)} invoices from S3"

        return {
            "success": True,
            "order_id": order_id,
            "invoices_found": len(invoices),
            "message": message
        }

    except HTTPException:
        raise
    except Exception as e:
        logger.error(f"❌ Error in AWB processing: {str(e)}", exc_info=True)
        raise HTTPException(status_code=500, detail=str(e))


def process_awb_background(batch_id: int, month: str, summary_s3_key: str, employees_s3_key: str, s3_prefix: str):
    """Background task for AWB processing"""
    db = SessionLocal()
    try:
        from utils.awb_processor import AWBProcessor
        from sqlalchemy.orm import sessionmaker
        from db.database import engine

        SessionLocal = sessionmaker(autocommit=False, autoflush=False, bind=engine)
        db = SessionLocal()

        logger.info(f"🔄 Processing AWB batch {batch_id}...")

        # Initialize processor
        processor = AWBProcessor()

        # Process month
        success, output_data, error_msg = processor.process_monthly_awb(
            company_id=None,  # Would need to fetch from batch_id
            month=month,
            summary_pdf_path=summary_s3_key,
            employees_csv_path=employees_s3_key,
            db_session=db
        )

        # Update batch job
        batch_job = db.query(BatchJob).filter(BatchJob.batch_id == batch_id).first()

        if success and output_data:
            # Generate output files
            outputs = processor.generate_outputs(output_data['records'], month)

            # Update batch job with results
            batch_job.status = "completed"
            batch_job.processed_files = output_data['total_count']
            batch_job.successful_files = output_data['total_count']
            batch_job.unmatched_count = output_data['unmatched_count']
            batch_job.json_output_path = outputs.get('json_path')
            batch_job.excel_output_path = outputs.get('excel_path')
            batch_job.csv_output_path = outputs.get('csv_path')

            logger.info(f"✅ Batch {batch_id} completed successfully")
        else:
            batch_job.status = "failed"
            batch_job.error_message = error_msg
            logger.error(f"❌ Batch {batch_id} failed: {error_msg}")

        db.commit()

    except Exception as e:
        logger.error(f"❌ Background processing error for batch {batch_id}: {str(e)}")
        batch_job = db.query(BatchJob).filter(BatchJob.batch_id == batch_id).first()
        if batch_job:
            batch_job.status = "failed"
            batch_job.error_message = str(e)
            db.commit()

    finally:
        db.close()


# ===== OneDrive/AWB Sync Endpoints =====

@app.get("/api/awb/sync-status")
def get_onedrive_sync_status(limit: int = Query(10, ge=1, le=100), db: Session = Depends(get_db)):
    """Get OneDrive sync history"""
    try:
        syncs = db.query(OneDriveSync).order_by(
            OneDriveSync.created_at.desc()
        ).limit(limit).all()

        return {
            "success": True,
            "syncs": [
                {
                    "sync_id": s.sync_id,
                    "last_sync_time": s.last_sync_time.isoformat() if s.last_sync_time else None,
                    "sync_status": s.sync_status,
                    "files_processed": s.files_processed,
                    "files_failed": s.files_failed,
                    "error_message": s.error_message,
                    "created_at": s.created_at.isoformat() if s.created_at else None,
                    "metadata": s.sync_metadata
                }
                for s in syncs
            ]
        }

    except Exception as e:
        logger.error(f"❌ Error fetching sync status: {str(e)}")
        raise HTTPException(status_code=500, detail=str(e))


@app.post("/api/awb/trigger-sync")
def trigger_onedrive_sync(
    background_tasks: BackgroundTasks,
    month: Optional[str] = Query(None, description="Optional month in YYYY-MM format (e.g., 2025-10)"),
    force: bool = Query(False, description="If true, force rescan of all files and repair corrupted objects"),
    reconcile: bool = Query(False, description="If true, perform filename-based reconciliation instead of incremental sync"),
    scan_processed: bool = Query(True, description="If true, scan OneDrive processed folder during reconciliation"),
    db: Session = Depends(get_db)
):
    """Manually trigger OneDrive sync with optional month, force, and reconciliation parameters

    Args:
        month: Optional month in YYYY-MM format to sync only that month (e.g., "2025-10")
        force: If true, ignore last_sync_time and re-scan all files; also re-upload corrupted objects
        reconcile: If true, perform filename-based reconciliation instead of incremental sync
        scan_processed: If true, scan OneDrive processed folder during reconciliation

    Returns:
        JSON with sync status and details
    """
    try:
        # Check if sync is enabled
        onedrive_enabled = os.getenv('ONEDRIVE_SYNC_ENABLED', 'false').lower() == 'true'

        if not onedrive_enabled:
            return {
                "success": False,
                "message": "OneDrive sync is disabled (ONEDRIVE_SYNC_ENABLED not set to 'true')"
            }

        # Validate month format if provided
        if month:
            try:
                parts = month.split('-')
                if len(parts) != 2:
                    raise ValueError("Invalid format")
                int(parts[0])  # year
                int(parts[1])  # month
                if int(parts[1]) < 1 or int(parts[1]) > 12:
                    raise ValueError("Invalid month")
            except (ValueError, IndexError):
                raise HTTPException(status_code=400, detail="Month must be in YYYY-MM format (e.g., 2025-10)")

        # Queue background task with parameters
        from scripts.onedrive_ingest import run_onedrive_sync as sync_func
        logger.info(f"📡 Endpoint received params: month={month}, force={force}, reconcile={reconcile}, scan_processed={scan_processed}")
        background_tasks.add_task(sync_func, month=month, force=force, reconcile=reconcile, scan_processed=scan_processed)

        message = f"OneDrive sync triggered"
        details = []
        if month:
            details.append(f"month={month}")
        if force:
            details.append("force=true")
        if reconcile:
            details.append("reconcile=true")
        if not scan_processed:
            details.append("scan_processed=false")
        if details:
            message += f" ({', '.join(details)})"
        message += " in background"

        return {
            "success": True,
            "message": message,
            "month": month,
            "force": force,
            "reconcile": reconcile,
            "scan_processed": scan_processed
        }

    except HTTPException:
        raise
    except Exception as e:
        logger.error(f"❌ Error triggering sync: {str(e)}")
        raise HTTPException(status_code=500, detail=str(e))


if __name__ == "__main__":
    import uvicorn

    # Use the secure configuration loader
    try:
        app_config = config_loader.get_app_config()
        port = app_config["port"]
        logger.info(f"🚀 Starting application on port {port}")
    except Exception as e:
        logger.error(f"Failed to load application config: {e}")
        port = 8000  # Fallback port

    # Start with a single worker to reduce memory usage
    uvicorn.run(
        app,
        host="0.0.0.0",
        port=port,
        workers=1,
    )
