"""Background OCR schedule runner.

This module implements the recurring logic for:
- Selecting due OCR schedules
- Ensuring monthly OneDrive folder / Excel structure
- Discovering new material files
- Running OCR and appending results to Excel
- Moving processed files to history and tracking status in DB
"""

import asyncio
import json
import logging
import os
import random
import re
import tempfile
from dataclasses import dataclass
from datetime import datetime, time, timedelta, timezone
from typing import Any, Dict, List, Optional, Tuple

from sqlalchemy.orm import Session
from sqlalchemy import and_

from db.database import SessionLocal
from db.models import (
    OcrSchedule,
    OcrScheduledFile,
    OcrScheduleRun,
    OrderItemType,
    ScheduleMode,
    ScheduledFileStatus,
    ScheduleRunStatus,
)
from main import extract_text_from_pdf, extract_text_from_image
from utils.prompt_schema_manager import load_prompt_and_schema, PromptSchemaManager
from utils.order_processor import escape_excel_formulas
from utils.onedrive_client import (
    build_client_from_env,
    normalise_onedrive_path,
    join_onedrive_path,
)
from utils.company_doc_type_config_resolver import CompanyDocTypeConfigResolver

logger = logging.getLogger(__name__)


@dataclass
class MonthStructure:
    material_folder: Any
    failed_folder: Any
    history_folder: Any
    output_excel_path: str
    material_folder_path: str
    failed_folder_path: str
    history_folder_path: str
    month_folder_path: str


DEFAULT_MONTH_PATTERN = "{YYYYMM}"
DEFAULT_OUTPUT_PATTERN = "{YYYYMM}.xlsx"
DEFAULT_MATERIAL_SUBFOLDER = "Material"
DEFAULT_HISTORY_SUBFOLDER = "history"
IMAGE_EXTENSIONS = (".png", ".jpg", ".jpeg")
SUPPORTED_DOCUMENT_EXTENSIONS = (".pdf",) + IMAGE_EXTENSIONS
DEFAULT_OCR_PROMPT = os.getenv(
    "OCR_SCHEDULE_PROMPT",
    "You are an OCR assistant. Extract all visible text from the document and return JSON with a single field raw_text containing the extracted text.",
)

_custom_schema = None
_schema_env = os.getenv("OCR_SCHEDULE_RESPONSE_SCHEMA")
if _schema_env:
    try:
        _custom_schema = json.loads(_schema_env)
    except json.JSONDecodeError:
        logger.warning("⚠️ OCR_SCHEDULE_RESPONSE_SCHEMA is not valid JSON; using default schema")

DEFAULT_OCR_RESPONSE_SCHEMA = _custom_schema or {
    "type": "object",
    "properties": {
        "raw_text": {"type": "string"},
    },
    "required": ["raw_text"],
}

_excel_mode_raw = os.getenv("EXCEL_WRITE_MODE")
if _excel_mode_raw:
    EXCEL_WRITE_MODE = _excel_mode_raw.strip().lower()
else:
    EXCEL_WRITE_MODE = "openpyxl"

USE_GRAPH_EXCEL = EXCEL_WRITE_MODE == "graph"
if EXCEL_WRITE_MODE not in ("openpyxl", "graph"):
    logger.warning(
        "⚠️ Unsupported EXCEL_WRITE_MODE '%s'; falling back to openpyxl",
        EXCEL_WRITE_MODE,
    )
    USE_GRAPH_EXCEL = False


def _run_async_callable(async_fn, *args, **kwargs):
    async def runner():
        return await async_fn(*args, **kwargs)

    try:
        return asyncio.run(runner())
    except RuntimeError:
        loop = asyncio.new_event_loop()
        try:
            return loop.run_until_complete(runner())
        finally:
            loop.close()


def _run_gemini_ocr(local_path: str, filename: str, prompt: Optional[str], schema: Optional[dict]) -> dict:
    prompt_to_use = prompt or DEFAULT_OCR_PROMPT
    schema_to_use = schema or DEFAULT_OCR_RESPONSE_SCHEMA
    extension = os.path.splitext(filename)[1].lower()
    if extension == ".pdf":
        raw_result = _run_async_callable(
            extract_text_from_pdf,
            local_path,
            prompt_to_use,
            response_schema=schema_to_use,
        )
    else:
        raw_result = _run_async_callable(
            extract_text_from_image,
            local_path,
            prompt_to_use,
            response_schema=schema_to_use,
        )

    text_payload: Optional[str] = None
    input_tokens: Optional[int] = None
    output_tokens: Optional[int] = None
    processing_time: Optional[float] = None
    status_updates: Dict[str, Any] = {}

    if isinstance(raw_result, dict):
        text_payload = raw_result.get("text")
        input_tokens = raw_result.get("input_tokens")
        output_tokens = raw_result.get("output_tokens")
        processing_time = raw_result.get("processing_time")
        status_updates = raw_result.get("status_updates") or {}
    elif isinstance(raw_result, str):
        text_payload = raw_result

    if text_payload:
        try:
            parsed_data = json.loads(text_payload)
        except json.JSONDecodeError:
            parsed_data = {"raw_text": text_payload}
    else:
        parsed_data = {"raw_text": None}

    return {
        "data": parsed_data,
        "meta": {
            "input_tokens": input_tokens,
            "output_tokens": output_tokens,
            "processing_time": processing_time,
            "status_updates": status_updates,
        },
    }


def _load_prompt_and_schema_for_schedule(schedule: OcrSchedule) -> Tuple[str, Optional[dict]]:
    company_code = getattr(getattr(schedule, "company", None), "company_code", None)
    doc_type_code = getattr(getattr(schedule, "document_type", None), "type_code", None)
    prompt: Optional[str] = None
    schema: Optional[dict] = None

    config = None
    if schedule.company_id and schedule.doc_type_id:
        db: Session = SessionLocal()
        try:
            raw_item_type = getattr(schedule, "default_item_type", None) or getattr(schedule, "item_type", None)
            if isinstance(raw_item_type, OrderItemType):
                resolved_item_type = raw_item_type
            elif isinstance(raw_item_type, str):
                try:
                    resolved_item_type = OrderItemType(raw_item_type)
                except ValueError:
                    resolved_item_type = OrderItemType.SINGLE_SOURCE
            else:
                resolved_item_type = OrderItemType.SINGLE_SOURCE

            config = CompanyDocTypeConfigResolver.get_active_row(
                db,
                schedule.company_id,
                schedule.doc_type_id,
                resolved_item_type,
            )
        except Exception as exc:
            logger.warning(
                "⚠️ Failed to resolve CompanyDocTypeConfig for schedule %s: %s",
                schedule.schedule_id,
                exc,
            )
        finally:
            db.close()

    if config and config.prompt_path and config.schema_path:
        try:
            prompt, schema = _run_async_callable(
                PromptSchemaManager.load_from_paths,
                config.prompt_path,
                config.schema_path,
                cache_key=f"config:{config.config_id}",
            )
            if prompt and schema:
                logger.info(
                    "✅ Loaded prompt/schema from CompanyDocTypeConfig %s for schedule %s",
                    config.config_id,
                    schedule.schedule_id,
                )
                return prompt, schema

            logger.warning(
                "⚠️ Config %s is missing prompt/schema for schedule %s; falling back to legacy manager",
                config.config_id,
                schedule.schedule_id,
            )
        except Exception as exc:
            logger.error(
                "⚠️ Failed to load prompt/schema from config %s for schedule %s: %s",
                config.config_id,
                schedule.schedule_id,
                exc,
            )

    if company_code and doc_type_code:
        try:
            prompt, schema = _run_async_callable(
                load_prompt_and_schema,
                company_code,
                doc_type_code,
            )
            if prompt:
                logger.info(
                    "✅ Loaded prompt/schema for schedule %s (%s/%s)",
                    schedule.schedule_id,
                    company_code,
                    doc_type_code,
                )
                return prompt, schema
            logger.warning(
                "⚠️ Prompt missing for schedule %s (%s/%s); falling back to default",
                schedule.schedule_id,
                company_code,
                doc_type_code,
            )
        except Exception as exc:
            logger.warning(
                "⚠️ Failed to load prompt/schema for schedule %s: %s",
                schedule.schedule_id,
                exc,
            )

    return DEFAULT_OCR_PROMPT, DEFAULT_OCR_RESPONSE_SCHEMA


def _sanitize_path_component(value: Optional[str], default: str) -> str:
    component = (value or "").strip()
    if not component:
        return default
    for ch in ["/", "\\", ":", "*", "?", '"', "<", ">", "|"]:
        component = component.replace(ch, "-")
    component = component.strip()
    return component or default


def _render_pattern_value(pattern: Optional[str], month_str: str, default_template: str) -> str:
    template = pattern or default_template or DEFAULT_MONTH_PATTERN
    if len(month_str) != 6 or not month_str.isdigit():
        raise ValueError(f"Invalid month string '{month_str}'")
    year = month_str[:4]
    month = month_str[4:]
    replacements = {
        "{YYYYMM}": f"{year}{month}",
        "{YYYY}": year,
        "{YY}": year[2:],
        "{MM}": month,
    }
    value = template
    for token, token_value in replacements.items():
        value = value.replace(token, token_value)
    return value


def describe_month_structure(schedule: OcrSchedule, month_str: str) -> Dict[str, str]:
    """Return canonical OneDrive paths for a schedule/month without touching OneDrive."""
    if not month_str or len(month_str) != 6 or not month_str.isdigit():
        raise ValueError("month must be provided in YYYYMM format")

    failed_name = _sanitize_path_component(
        schedule.failed_subfolder_name or "_Failed",
        schedule.failed_subfolder_name or "_Failed",
    )

    if getattr(schedule, "auto_month_folders", False):
        schedule_root = normalise_onedrive_path(schedule.schedule_root_path or "")
        if not schedule_root:
            raise ValueError("schedule_root_path is required when auto_month_folders=true")

        month_folder_name = _sanitize_path_component(
            _render_pattern_value(
                schedule.month_folder_pattern,
                month_str,
                DEFAULT_MONTH_PATTERN,
            ),
            month_str,
        )
        material_name = _sanitize_path_component(
            schedule.material_subfolder_name or DEFAULT_MATERIAL_SUBFOLDER,
            DEFAULT_MATERIAL_SUBFOLDER,
        )
        history_name = _sanitize_path_component(
            schedule.history_subfolder_name or DEFAULT_HISTORY_SUBFOLDER,
            DEFAULT_HISTORY_SUBFOLDER,
        )

        month_folder_path = join_onedrive_path(schedule_root, month_folder_name)
        material_folder_path = join_onedrive_path(month_folder_path, material_name)
        history_folder_path = join_onedrive_path(month_folder_path, history_name)
        failed_folder_path = join_onedrive_path(month_folder_path, failed_name)
        output_excel_name = _sanitize_path_component(
            _render_pattern_value(
                schedule.output_filename_pattern,
                month_str,
                DEFAULT_OUTPUT_PATTERN,
            ),
            f"{month_str}.xlsx",
        )
        output_excel_path = join_onedrive_path(month_folder_path, output_excel_name)
    else:
        material_root = normalise_onedrive_path(schedule.material_root_path or "")
        history_root = normalise_onedrive_path(schedule.history_root_path or "")
        output_root = normalise_onedrive_path(schedule.output_root_path or "")
        if not material_root or not history_root or not output_root:
            raise ValueError("material_root_path, history_root_path, and output_root_path are required")

        month_folder_path = join_onedrive_path(material_root, month_str)
        material_folder_path = month_folder_path
        history_folder_path = join_onedrive_path(history_root, month_str)
        failed_folder_path = join_onedrive_path(month_folder_path, failed_name)
        output_excel_path = join_onedrive_path(output_root, f"{month_str}.xlsx")

    return {
        "auto_month_folders": bool(getattr(schedule, "auto_month_folders", False)),
        "month_folder_path": month_folder_path,
        "material_folder_path": material_folder_path,
        "history_folder_path": history_folder_path,
        "failed_folder_path": failed_folder_path,
        "output_excel_path": output_excel_path,
    }


HK_TZ = timezone(timedelta(hours=8))


def _utcnow() -> datetime:
    """UTC now (naive, treated as UTC for storage/ordering)."""
    return datetime.utcnow()


def _hk_now() -> datetime:
    """Hong Kong local time (UTC+8) for logging and user-facing timestamps."""
    return datetime.now(HK_TZ)


def _parse_time_hhmm(value: Optional[str]) -> Optional[time]:
    """Parse HH:MM string to time, returning None on invalid input."""
    if not value:
        return None
    try:
        parts = value.split(":")
        if len(parts) != 2:
            return None
        hour = int(parts[0])
        minute = int(parts[1])
        return time(hour=hour, minute=minute)
    except Exception:
        return None


def _is_within_window(schedule: OcrSchedule, now: datetime) -> bool:
    """Check whether 'now' is inside the schedule's allowed window."""
    if schedule.schedule_mode != ScheduleMode.WINDOWED_INTERVAL:
        return True

    # Weekday filtering
    if schedule.allowed_weekdays:
        try:
            allowed = {
                int(token)
                for token in schedule.allowed_weekdays.split(",")
                if token.strip()
            }
        except Exception:
            allowed = set()
        if allowed and now.weekday() not in allowed:
            return False

    # Time-of-day window (treated in server local time for simplicity)
    start_t = _parse_time_hhmm(schedule.window_start_time)
    end_t = _parse_time_hhmm(schedule.window_end_time)

    if start_t is None and end_t is None:
        return True

    local_t = now.time()

    if start_t and end_t:
        # Simple inclusive start, exclusive end window
        if start_t <= end_t:
            return start_t <= local_t < end_t
        # Overnight window (e.g. 22:00–06:00)
        return local_t >= start_t or local_t < end_t
    if start_t:
        return local_t >= start_t
    if end_t:
        return local_t < end_t

    return True


def _get_current_month_str(now: datetime) -> str:
    return now.astimezone(HK_TZ).strftime("%Y%m")


def _parse_month_from_folder_name(
    folder_name: str,
    pattern: Optional[str] = None
) -> Optional[str]:
    """Parse YYYYMM string from a folder name based on pattern.

    Args:
        folder_name: The folder name to parse
        pattern: The month_folder_pattern (e.g., "{YYYYMM}", "Invoice-{YYYY}-{MM}")

    Returns:
        YYYYMM string if successfully parsed, None otherwise
    """
    if not folder_name:
        return None

    # Default pattern or explicit {YYYYMM} - expect 6-digit folder name
    if pattern is None or pattern == DEFAULT_MONTH_PATTERN or pattern == "{YYYYMM}":
        if re.match(r"^\d{6}$", folder_name):
            year = int(folder_name[:4])
            month = int(folder_name[4:6])
            if 1 <= month <= 12 and 1900 <= year <= 2100:
                return folder_name
        return None

    # Complex pattern - reverse-engineer to extract year/month
    # Build regex from pattern by replacing tokens with capture groups
    regex_pattern = re.escape(pattern)
    regex_pattern = regex_pattern.replace(r"\{YYYYMM\}", r"(\d{6})")
    regex_pattern = regex_pattern.replace(r"\{YYYY\}", r"(\d{4})")
    regex_pattern = regex_pattern.replace(r"\{YY\}", r"(\d{2})")
    regex_pattern = regex_pattern.replace(r"\{MM\}", r"(\d{2})")
    regex_pattern = f"^{regex_pattern}$"

    match = re.match(regex_pattern, folder_name)
    if not match:
        return None

    # Extract tokens from original pattern to know what was captured
    tokens = re.findall(r"\{(YYYYMM|YYYY|YY|MM)\}", pattern)
    groups = match.groups()

    if len(tokens) != len(groups):
        return None

    year = None
    month = None
    for token, value in zip(tokens, groups):
        if token == "YYYYMM":
            year = int(value[:4])
            month = int(value[4:6])
        elif token == "YYYY":
            year = int(value)
        elif token == "YY":
            year = 2000 + int(value)
        elif token == "MM":
            month = int(value)

    if year is None or month is None:
        return None
    if not (1 <= month <= 12 and 1900 <= year <= 2100):
        return None

    return f"{year:04d}{month:02d}"


def _discover_month_folders_legacy(
    onedrive_client,
    schedule: OcrSchedule,
) -> List[str]:
    """Discover all YYYYMM folders in material_root_path (legacy mode).

    Returns:
        List of YYYYMM strings sorted chronologically
    """
    material_root = getattr(schedule, "material_root_path", None)
    if not material_root:
        logger.warning(
            "⚠️ Schedule %s has no material_root_path set; cannot discover months",
            schedule.schedule_id,
        )
        return []

    parent_folder = onedrive_client.get_folder(material_root)
    if not parent_folder:
        logger.warning(
            "⚠️ Could not access material_root_path %s for schedule %s",
            material_root,
            schedule.schedule_id,
        )
        return []

    child_folders = onedrive_client.list_folders(parent_folder)
    discovered_months: List[str] = []

    for folder in child_folders:
        folder_name = getattr(folder, "name", "") or ""
        month_str = _parse_month_from_folder_name(folder_name, None)
        if month_str:
            discovered_months.append(month_str)
        else:
            logger.debug(
                "⊘ Skipping non-month folder %s in %s",
                folder_name,
                material_root,
            )

    # Sort chronologically (oldest first)
    discovered_months.sort()
    return discovered_months


def _discover_month_folders_auto(
    onedrive_client,
    schedule: OcrSchedule,
) -> List[str]:
    """Discover all month folders in schedule_root_path (auto mode).

    Returns:
        List of YYYYMM strings sorted chronologically
    """
    schedule_root = getattr(schedule, "schedule_root_path", None)
    if not schedule_root:
        logger.warning(
            "⚠️ Schedule %s has no schedule_root_path set; cannot discover months",
            schedule.schedule_id,
        )
        return []

    parent_folder = onedrive_client.get_folder(schedule_root)
    if not parent_folder:
        logger.warning(
            "⚠️ Could not access schedule_root_path %s for schedule %s",
            schedule_root,
            schedule.schedule_id,
        )
        return []

    child_folders = onedrive_client.list_folders(parent_folder)
    month_pattern = getattr(schedule, "month_folder_pattern", None) or DEFAULT_MONTH_PATTERN
    discovered_months: List[str] = []

    for folder in child_folders:
        folder_name = getattr(folder, "name", "") or ""
        month_str = _parse_month_from_folder_name(folder_name, month_pattern)
        if month_str:
            discovered_months.append(month_str)
        else:
            logger.debug(
                "⊘ Skipping non-month folder %s in %s (pattern: %s)",
                folder_name,
                schedule_root,
                month_pattern,
            )

    # Sort chronologically (oldest first)
    discovered_months.sort()
    return discovered_months


def _discover_all_month_folders(
    onedrive_client,
    schedule: OcrSchedule,
) -> List[str]:
    """Discover all processable month folders for a schedule.

    Returns:
        List of YYYYMM strings sorted chronologically
    """
    if getattr(schedule, "auto_month_folders", False):
        months = _discover_month_folders_auto(onedrive_client, schedule)
    else:
        months = _discover_month_folders_legacy(onedrive_client, schedule)

    if months:
        logger.info(
            "🔍 Discovered %s month folder(s) for schedule %s: %s",
            len(months),
            schedule.schedule_id,
            ", ".join(months),
        )
    else:
        logger.info(
            "ℹ️ No month folders discovered for schedule %s",
            schedule.schedule_id,
        )

    return months


def ensure_month_structure(
    onedrive_client,
    schedule: OcrSchedule,
    month_str: str,
) -> MonthStructure:
    """Ensure per-month folder structure exists for a schedule."""
    if getattr(schedule, "auto_month_folders", False):
        return _ensure_auto_month_structure(onedrive_client, schedule, month_str)
    return _ensure_legacy_month_structure(onedrive_client, schedule, month_str)


def _ensure_legacy_month_structure(
    onedrive_client,
    schedule: OcrSchedule,
    month_str: str,
) -> MonthStructure:
    material_root = normalise_onedrive_path(schedule.material_root_path or "")
    history_root = normalise_onedrive_path(schedule.history_root_path or "")
    output_root = normalise_onedrive_path(schedule.output_root_path or "")

    if not material_root or not history_root or not output_root:
        raise RuntimeError(
            f"OCR schedule {schedule.schedule_id} has incomplete OneDrive roots"
        )

    logger.info(
        "🔍 schedule %s resolving material root path '%s'",
        schedule.schedule_id,
        material_root,
    )
    material_parent = onedrive_client.get_folder(material_root)
    if not material_parent:
        raise RuntimeError(
            f"Material root folder not found for schedule {schedule.schedule_id}: {material_root}"
        )

    logger.info(
        "🔍 schedule %s resolving history root path '%s'",
        schedule.schedule_id,
        history_root,
    )
    history_parent = onedrive_client.get_folder(history_root)
    if not history_parent:
        raise RuntimeError(
            f"History root folder not found for schedule {schedule.schedule_id}: {history_root}"
        )

    logger.info(
        "🔍 schedule %s resolving output root path '%s'",
        schedule.schedule_id,
        output_root,
    )
    output_parent = onedrive_client.get_folder(output_root)
    if not output_parent:
        raise RuntimeError(
            f"Output root folder not found for schedule {schedule.schedule_id}: {output_root}"
        )

    material_month = onedrive_client.get_or_create_folder(material_parent, month_str)
    if not material_month:
        raise RuntimeError(
            f"Failed to get/create material month folder {month_str} under {material_root}"
        )

    failed_name = schedule.failed_subfolder_name or "_Failed"
    failed_folder = onedrive_client.get_or_create_folder(material_month, failed_name)
    if not failed_folder:
        raise RuntimeError(
            f"Failed to get/create failed folder '{failed_name}' under material month {month_str}"
        )

    history_month = onedrive_client.get_or_create_folder(history_parent, month_str)
    if not history_month:
        raise RuntimeError(
            f"Failed to get/create history month folder {month_str} under {history_root}"
        )

    output_excel_path = join_onedrive_path(output_root, f"{month_str}.xlsx")
    material_month_path = join_onedrive_path(material_root, month_str)
    history_month_path = join_onedrive_path(history_root, month_str)
    failed_folder_path = join_onedrive_path(material_month_path, failed_name)

    return MonthStructure(
        material_folder=material_month,
        failed_folder=failed_folder,
        history_folder=history_month,
        output_excel_path=output_excel_path,
        material_folder_path=material_month_path,
        failed_folder_path=failed_folder_path,
        history_folder_path=history_month_path,
        month_folder_path=material_month_path,
    )


def _ensure_auto_month_structure(
    onedrive_client,
    schedule: OcrSchedule,
    month_str: str,
) -> MonthStructure:
    schedule_root = normalise_onedrive_path(schedule.schedule_root_path or "")
    if not schedule_root:
        raise RuntimeError(
            f"Auto-folder schedule {schedule.schedule_id} missing schedule_root_path"
        )

    schedule_root_folder = onedrive_client.ensure_folder_path(schedule_root)
    if not schedule_root_folder:
        raise RuntimeError(
            f"Schedule root folder not found or cannot be created: {schedule_root}"
        )

    month_folder_name_raw = _render_pattern_value(
        schedule.month_folder_pattern,
        month_str,
        DEFAULT_MONTH_PATTERN,
    )
    month_folder_name = _sanitize_path_component(
        month_folder_name_raw,
        default=month_str,
    )
    month_folder = onedrive_client.get_or_create_folder(
        schedule_root_folder,
        month_folder_name,
    )
    if not month_folder:
        raise RuntimeError(
            f"Failed to create/find month folder {month_folder_name} under {schedule_root}"
        )

    month_folder_path = join_onedrive_path(schedule_root, month_folder_name)
    material_name = _sanitize_path_component(
        schedule.material_subfolder_name or DEFAULT_MATERIAL_SUBFOLDER,
        DEFAULT_MATERIAL_SUBFOLDER,
    )
    history_name = _sanitize_path_component(
        schedule.history_subfolder_name or DEFAULT_HISTORY_SUBFOLDER,
        DEFAULT_HISTORY_SUBFOLDER,
    )
    failed_name = _sanitize_path_component(
        schedule.failed_subfolder_name or "_Failed",
        schedule.failed_subfolder_name or "_Failed",
    )

    material_month = onedrive_client.get_or_create_folder(month_folder, material_name)
    if not material_month:
        raise RuntimeError(
            f"Failed to create/find material folder {material_name} under {month_folder_path}"
        )

    history_month = onedrive_client.get_or_create_folder(month_folder, history_name)
    if not history_month:
        raise RuntimeError(
            f"Failed to create/find history folder {history_name} under {month_folder_path}"
        )

    failed_folder = onedrive_client.get_or_create_folder(month_folder, failed_name)
    if not failed_folder:
        raise RuntimeError(
            f"Failed to ensure failed folder {failed_name} under {month_folder_path}"
        )

    output_filename_raw = _render_pattern_value(
        schedule.output_filename_pattern,
        month_str,
        DEFAULT_OUTPUT_PATTERN,
    )
    output_filename = _sanitize_path_component(
        output_filename_raw,
        default=f"{month_str}.xlsx",
    )
    output_excel_path = join_onedrive_path(month_folder_path, output_filename)

    material_month_path = join_onedrive_path(month_folder_path, material_name)
    history_month_path = join_onedrive_path(month_folder_path, history_name)
    failed_folder_path = join_onedrive_path(month_folder_path, failed_name)

    return MonthStructure(
        material_folder=material_month,
        failed_folder=failed_folder,
        history_folder=history_month,
        output_excel_path=output_excel_path,
        material_folder_path=material_month_path,
        failed_folder_path=failed_folder_path,
        history_folder_path=history_month_path,
        month_folder_path=month_folder_path,
    )


def _discover_candidates(
    db: Session,
    schedule: OcrSchedule,
    month_str: str,
    onedrive_client,
    material_month_folder,
    material_month_path: str,
    max_files: int,
) -> List[Tuple[Any, OcrScheduledFile]]:
    """Discover new or retryable files in the monthly material folder."""
    candidates: List[Tuple[Any, OcrScheduledFile]] = []

    # For now, reuse list_all_pdfs and then filter on name.
    # Month filtering is implicit via the folder name (YYYYMM).
    from utils.onedrive_client import O365File  # type: ignore

    try:
        pdf_items: List[O365File] = onedrive_client.list_all_documents(
            material_month_folder,
            file_extensions=list(SUPPORTED_DOCUMENT_EXTENSIONS),
            created_month_filter=None,
        )
    except Exception as exc:
        logger.error(
            "❌ Failed to list PDFs in material folder for schedule %s: %s",
            schedule.schedule_id,
            exc,
        )
        return []

    for item in pdf_items:
        name = getattr(item, "name", None) or ""
        if not name or name.startswith("~$"):
            # Skip temporary lock files
            continue

        onedrive_path = join_onedrive_path(material_month_path, name)

        row: Optional[OcrScheduledFile] = (
            db.query(OcrScheduledFile)
            .filter(
                and_(
                    OcrScheduledFile.schedule_id == schedule.schedule_id,
                    OcrScheduledFile.onedrive_path == onedrive_path,
                )
            )
            .one_or_none()
        )

        if row:
            # If we see a file in the material folder but the last known status
            # is COMPLETED or (stuck) PROCESSING, we interpret this as either:
            #   - user intentionally moved it back from history to material, or
            #   - a previous run crashed mid-processing.
            # In both cases, reset the row to PENDING so it can be picked up
            # again in this cycle.
            if row.status in (
                ScheduledFileStatus.COMPLETED,
                ScheduledFileStatus.PROCESSING,
            ):
                status_label = (
                    row.status.value if hasattr(row.status, "value") else str(row.status)
                )
                logger.info(
                    "🔁 Found %s file back in material for %s; resetting status to PENDING for reprocessing",
                    status_label,
                    onedrive_path,
                )
                row.status = ScheduledFileStatus.PENDING
                row.error_message = None
                # Keep attempt_count as historical info; it will be incremented
                # when processing starts again.
            # WAITING_FOR_EXCEL / ERROR / PENDING will naturally be retried.
        else:
            row = OcrScheduledFile(
                schedule_id=schedule.schedule_id,
                month_str=month_str,
                onedrive_path=onedrive_path,
                filename=name,
                status=ScheduledFileStatus.PENDING,
                attempt_count=0,
            )
            db.add(row)
            db.flush()

        candidates.append((item, row))

        if max_files and len(candidates) >= max_files:
            break

    db.commit()
    return candidates


def _column_index_to_letter(index: int) -> str:
    """Convert a 1-based column index to Excel column letters (A, B, ..., AA, AB, ...)."""
    if index <= 0:
        raise ValueError("index must be positive")
    result = ""
    while index > 0:
        index, remainder = divmod(index - 1, 26)
        result = chr(65 + remainder) + result
    return result


def _append_rows_to_excel_via_graph(
    onedrive_client,
    excel_path: str,
    flattened_rows: List[Dict[str, Any]],
    new_headers: List[str],
) -> int:
    """Attempt to append rows to an existing Excel workbook using Microsoft Graph.

    Returns the 1-based row index of the first appended row, or -1 if the Graph
    path cannot be used (in which case the caller may fall back to openpyxl).
    """
    from urllib.parse import quote

    normalized_path = normalise_onedrive_path(excel_path)

    # Only proceed if the client exposes the workbook helpers.
    if not hasattr(onedrive_client, "create_workbook_session") or not hasattr(
        onedrive_client, "graph_request"
    ):
        logger.info(
            "ℹ️ OneDrive client does not expose Graph helpers; skipping Graph Excel append",
        )
        return -1

    session_id: Optional[str] = None
    try:
        session_id = onedrive_client.create_workbook_session(excel_path, persist_changes=True)
        if not session_id:
            logger.error("❌ Failed to create Excel workbook session for %s", excel_path)
            return -1

        # 1) Resolve first worksheet (equivalent to wb.active)
        ws_url = f"/me/drive/root:/{normalized_path}:/workbook/worksheets"
        resp_ws = onedrive_client.graph_request("GET", ws_url, session_id=session_id)
        if resp_ws.status_code != 200:
            logger.error(
                "❌ Failed to list worksheets for %s via Graph: %s %s",
                excel_path,
                resp_ws.status_code,
                resp_ws.text,
            )
            return -1

        ws_payload = resp_ws.json()
        worksheets = ws_payload.get("value") or []
        if not worksheets:
            logger.error("❌ Workbook %s has no worksheets; cannot append via Graph", excel_path)
            return -1

        first_sheet = worksheets[0]
        sheet_identifier = first_sheet.get("id") or first_sheet.get("name")
        if not sheet_identifier:
            logger.error("❌ Could not determine worksheet identifier for %s", excel_path)
            return -1

        sheet_id_escaped = quote(str(sheet_identifier), safe="")

        # 2) Inspect used range to derive header and append position
        used_url = (
            f"/me/drive/root:/{normalized_path}:/workbook/worksheets('{sheet_id_escaped}')"
            "/usedRange(valuesOnly=true)"
        )
        resp_range = onedrive_client.graph_request("GET", used_url, session_id=session_id)
        if resp_range.status_code == 404:
            # Treat as empty workbook; keep behaviour simple by delegating to openpyxl.
            logger.info(
                "ℹ️ Workbook %s has no used range yet; will fall back to openpyxl for header creation",
                excel_path,
            )
            return -1
        if resp_range.status_code != 200:
            logger.error(
                "❌ Failed to query usedRange for %s via Graph: %s %s",
                excel_path,
                resp_range.status_code,
                resp_range.text,
            )
            return -1

        range_payload = resp_range.json()
        values = range_payload.get("values") or []
        row_count = int(range_payload.get("rowCount", len(values) or 0))
        row_index = int(range_payload.get("rowIndex", 0))

        if row_count == 0 or not values:
            # No data or header yet – let openpyxl create the initial structure for now.
            logger.info(
                "ℹ️ Workbook %s appears empty; using openpyxl path for initial header/data",
                excel_path,
            )
            return -1

        existing_header_row = values[0] or []
        existing_headers = [str(v) if v is not None else "" for v in existing_header_row]
        header_cols = existing_headers

        nonempty_existing = {h for h in header_cols if h and str(h).strip()}
        overlap = nonempty_existing.intersection(new_headers)
        if row_count == 1 and not overlap:
            # In openpyxl path we would rewrite the header entirely; keep that
            # behaviour by delegating to openpyxl in this corner case.
            logger.info(
                "ℹ️ Workbook %s has a single non-overlapping header row; "
                "falling back to openpyxl to rewrite header",
                excel_path,
            )
            return -1

        col_count = len(header_cols)
        if col_count == 0:
            logger.warning(
                "⚠️ Workbook %s usedRange header has zero columns; cannot append via Graph",
                excel_path,
            )
            return -1

        # Compute start row index (1-based) for append, mirroring ws.max_row + 1.
        start_row_index = row_index + row_count + 1

        # Build values matrix aligned to existing header columns.
        values_to_append: List[List[Any]] = []
        for record in flattened_rows:
            row_values: List[Any] = []
            for col_name in header_cols:
                raw_value = record.get(col_name)
                safe_value = escape_excel_formulas(raw_value)
                row_values.append(safe_value)
            values_to_append.append(row_values)
        if not values_to_append:
            logger.warning("⚠️ No rows to append via Graph for %s", excel_path)
            return -1

        last_row_index = start_row_index + len(values_to_append) - 1
        last_col_letter = _column_index_to_letter(col_count)
        address = f"A{start_row_index}:{last_col_letter}{last_row_index}"

        range_url = (
            f"/me/drive/root:/{normalized_path}:/workbook/worksheets('{sheet_id_escaped}')"
            f"/range(address='{address}')"
        )
        resp_update = onedrive_client.graph_request(
            "PATCH",
            range_url,
            session_id=session_id,
            json={"values": values_to_append},
        )
        if resp_update.status_code not in (200, 201):
            logger.error(
                "❌ Graph range update failed for %s: %s %s",
                excel_path,
                resp_update.status_code,
                resp_update.text,
            )
            return -1

        logger.info(
            "✅ Appended %s row(s) to Excel via Graph at %s (start row %s)",
            len(values_to_append),
            excel_path,
            start_row_index,
        )
        return start_row_index
    except Exception as exc:
        logger.exception("❌ Unexpected error during Graph Excel append for %s: %s", excel_path, exc)
        return -1
    finally:
        if session_id:
            try:
                onedrive_client.close_workbook_session(excel_path, session_id)
            except Exception as exc:
                logger.warning(
                    "⚠️ Failed to close Excel workbook session for %s: %s",
                    excel_path,
                    exc,
                )


def _append_rows_to_excel_on_onedrive(
    onedrive_client,
    excel_path: str,
    ocr_json: Dict[str, Any],
    max_retries: int = 3,
) -> int:
    """Append flattened OCR JSON rows to Excel file on OneDrive, handling lock/retry.

    Uses the same deep-flattening strategy as json_to_csv/deep_flatten_json_universal so
    that structure is driven by the document schema and Gemini output, not by a
    per-schedule Excel configuration.

    Returns the 1-based Excel row index of the first appended row, or -1 on failure.
    """
    # Reuse the same deep flattening logic used for consolidated CSV/Excel
    from utils.excel_converter import deep_flatten_json_universal

    flattened_rows: List[Dict[str, Any]] = deep_flatten_json_universal(ocr_json)
    if not flattened_rows:
        logger.warning("⚠️ No data found in OCR JSON to append to Excel")
        return -1

    # Determine header set for new data
    new_headers: List[str] = []
    seen = set()
    for row in flattened_rows:
        for key in row.keys():
            if key not in seen:
                seen.add(key)
                new_headers.append(str(key))

    logger.info(
        "🧮 Preparing Excel append for %s: %s flattened row(s), keys=%s",
        excel_path,
        len(flattened_rows),
        ", ".join(new_headers),
    )

    # First try the Graph-based workbook API if enabled; fall back to openpyxl
    # for unsupported cases or errors.
    if USE_GRAPH_EXCEL:
        graph_row_index = _append_rows_to_excel_via_graph(
            onedrive_client,
            excel_path,
            flattened_rows,
            new_headers,
        )
        if graph_row_index > 0:
            return graph_row_index
        logger.info(
            "ℹ️ Graph Excel append did not succeed or was unsupported for %s; falling back to openpyxl",
            excel_path,
        )

    import io

    try:
        import openpyxl  # type: ignore
    except Exception as exc:  # pragma: no cover - environment guard
        logger.error("❌ openpyxl is required for Excel append but not installed: %s", exc)
        return -1

    last_exc: Optional[Exception] = None
    for attempt in range(max_retries):
        try:
            normalized_path = normalise_onedrive_path(excel_path)
            parent_path = "/".join(normalized_path.split("/")[:-1])
            filename_only = normalized_path.split("/")[-1]

            parent_folder = onedrive_client.get_folder(parent_path)
            if not parent_folder:
                logger.error(
                    "❌ Failed to resolve parent folder %s when preparing Excel append",
                    parent_path,
                )
                return -1

            existing_file = None
            try:
                for item in parent_folder.get_items():
                    if getattr(item, "is_file", False) and getattr(item, "name", "") == filename_only:
                        existing_file = item
                        break
            except Exception as list_exc:
                logger.warning(
                    "⚠️ Failed to list items under %s while locating Excel: %s",
                    parent_path,
                    list_exc,
                )

            content: Optional[bytes] = None
            if existing_file is not None:
                try:
                    try:
                        content = existing_file.get_content()  # type: ignore[attr-defined]
                        logger.info("✅ Downloaded existing Excel via get_content(): %s", excel_path)
                    except Exception:
                        import tempfile as _temp
                        import os as _os2

                        with _temp.TemporaryDirectory() as tmpdir:
                            ok = existing_file.download(to_path=tmpdir)
                            if ok:
                                local_name = getattr(existing_file, "name", filename_only) or filename_only
                                local_path = _os2.path.join(tmpdir, local_name)
                                try:
                                    with open(local_path, "rb") as f:
                                        content = f.read()
                                    logger.info("✅ Downloaded existing Excel via download(): %s", excel_path)
                                except FileNotFoundError:
                                    logger.warning(
                                        "⚠️ Downloaded Excel not found in temp dir for %s",
                                        excel_path,
                                    )
                            else:
                                logger.warning(
                                    "⚠️ existing_file.download returned False for %s",
                                    excel_path,
                                )
                except Exception as dl_exc:
                    logger.error(
                        "❌ Error downloading existing Excel file %s: %s",
                        excel_path,
                        dl_exc,
                    )

            # Download existing workbook if present; otherwise create new one.
            if content:
                wb = openpyxl.load_workbook(io.BytesIO(content))
                ws = wb.active
                # Existing header from first row
                existing_headers = [
                    cell.value for cell in next(ws.iter_rows(min_row=1, max_row=1))
                ]
                header_cols = [str(h) for h in existing_headers]

                # If this workbook only has an initial header row and that header
                # has no overlap with the new OCR keys (e.g. manually created
                # template with completely different columns), treat it as
                # effectively empty and rewrite the header to match OCR keys.
                nonempty_existing = {h for h in header_cols if h and str(h).strip()}
                overlap = nonempty_existing.intersection(new_headers)
                if ws.max_row == 1 and not overlap:
                    logger.info(
                        "⚙️ Existing Excel header for %s has no overlap with OCR keys; "
                        "rewriting header to %s",
                        excel_path,
                        ", ".join(new_headers),
                    )
                    ws.delete_rows(1)
                    header_cols = new_headers
                    ws.append(header_cols)
                    start_row_index = 2
                else:
                    # NOTE: We still do NOT auto-extend headers if new keys appear later.
                    # This keeps the sheet schema stable once created with OCR keys.
                    start_row_index = ws.max_row + 1
            else:
                wb = openpyxl.Workbook()
                ws = wb.active
                # First-time header: union of keys across flattened rows
                header_cols = new_headers
                ws.append(header_cols)
                start_row_index = 2

            # Append one Excel row per flattened record
            for record in flattened_rows:
                row_values: List[Any] = []
                for col_name in header_cols:
                    raw_value = record.get(col_name)
                    safe_value = escape_excel_formulas(raw_value)
                    row_values.append(safe_value)
                ws.append(row_values)

            # Save workbook back to bytes
            bio = io.BytesIO()
            wb.save(bio)
            bio.seek(0)
            data = bio.read()

            # Upload back to same path (simple overwrite via folder.upload_file)
            normalized_path = normalise_onedrive_path(excel_path)
            parent_path = "/".join(normalized_path.split("/")[:-1])
            filename_only = normalized_path.split("/")[-1]

            parent_folder = onedrive_client.get_folder(parent_path)
            if not parent_folder:
                logger.error(
                    "❌ Failed to resolve parent folder %s when uploading Excel",
                    parent_path,
                )
                return -1

            import tempfile
            import os as _os

            with tempfile.NamedTemporaryFile(delete=False) as tmp_file:
                tmp_file.write(data)
                tmp_path = tmp_file.name
            try:
                parent_folder.upload_file(tmp_path, item_name=filename_only)
                logger.info("📤 Uploaded refreshed Excel file to %s", excel_path)
            except Exception as exc:
                # Let the outer retry loop decide how to handle lock vs. other errors.
                logger.error("❌ Failed to upload Excel file %s: %s", excel_path, exc)
                raise
            finally:
                try:
                    _os.unlink(tmp_path)
                except OSError:
                    pass

            logger.info("✅ Appended row to Excel at %s (row %s)", excel_path, start_row_index)
            return start_row_index

        except Exception as exc:
            last_exc = exc
            logger.exception(
                "⚠️ Excel append attempt %s/%s failed for %s",
                attempt + 1,
                max_retries,
                excel_path,
            )
            if attempt < max_retries - 1:
                # Detect OneDrive/Graph error situations more robustly.
                is_locked = False
                is_conflict = False  # 409 eTag conflict
                status_code = getattr(getattr(exc, "response", None), "status_code", None)
                if status_code == 423:
                    is_locked = True
                elif status_code == 409:
                    is_conflict = True
                else:
                    message = str(exc).lower()
                    if (
                        "resourcelocked" in message
                        or " 423 " in message
                        or "423 client error" in message
                    ):
                        is_locked = True
                    elif (
                        "409" in message
                        or "conflict" in message
                        or "etag" in message
                    ):
                        is_conflict = True

                if is_locked:
                    delay = 60 + random.uniform(0, 10)  # 60-70s with jitter
                    logger.info(
                        "⏳ Excel file appears locked (423). Waiting %.1f seconds before retrying",
                        delay,
                    )
                elif is_conflict:
                    delay = 2 + random.uniform(0, 3)  # 2-5s with jitter, short delay as we re-download
                    logger.info(
                        "⏳ Excel file has eTag conflict (409). Will re-download and retry in %.1f seconds",
                        delay,
                    )
                else:
                    delay = 5 * (attempt + 1) + random.uniform(0, 5)  # Progressive delay with jitter
                    logger.info(
                        "⏳ Waiting %.1f seconds before retrying Excel append", delay
                    )

                from time import sleep

                sleep(delay)

    if last_exc:
        logger.error("❌ All Excel append attempts failed for %s: %s", excel_path, last_exc)
    return -1


def _process_single_file(
    db: Session,
    schedule: OcrSchedule,
    onedrive_client,
    file_item,
    row: OcrScheduledFile,
    month_str: str,
    history_month_folder,
    failed_folder,
    output_excel_path: str,
    ocr_prompt: Optional[str],
    ocr_schema: Optional[dict],
) -> bool:
    """Run full pipeline for a single file. Returns True if completed."""
    now = _utcnow()

    # Lock row
    row = (
        db.query(OcrScheduledFile)
        .filter(OcrScheduledFile.id == row.id)
        .with_for_update()
        .one()
    )
    if row.status in (
        ScheduledFileStatus.PROCESSING,
        ScheduledFileStatus.COMPLETED,
    ):
        return False

    row.status = ScheduledFileStatus.PROCESSING
    row.attempt_count = (row.attempt_count or 0) + 1
    row.error_message = None
    db.commit()

    # Download file to temp dir
    filename = row.filename or getattr(file_item, "name", "file.pdf")
    try:
        with tempfile.TemporaryDirectory() as tmpdir:
            ok = onedrive_client.download_file(file_item, tmpdir)
            if not ok:
                raise RuntimeError("Download from OneDrive failed")

            local_path = os.path.join(tmpdir, filename)

            # Raw OCR -> JSON
            ocr_result = _run_gemini_ocr(local_path, filename, ocr_prompt, ocr_schema)
    except Exception as exc:
        logger.error(
            "❌ OCR failed for schedule %s file %s: %s",
            schedule.schedule_id,
            filename,
            exc,
        )
        # Move to failed folder
        try:
            onedrive_client.move_file(file_item, failed_folder)
        except Exception as move_exc:
            logger.error(
                "❌ Additionally failed to move file %s to failed folder: %s",
                filename,
                move_exc,
            )
        row.status = ScheduledFileStatus.ERROR
        row.error_message = f"OCR failed: {exc}"
        db.commit()
        return False

    # Persist OCR JSON if desired (optional; for now just store inline as JSON string)
    data_for_excel = ocr_result.get("data") if isinstance(ocr_result, dict) else None
    try:
        row.ocr_json_path = json.dumps(data_for_excel or {}, ensure_ascii=False)
    except Exception:
        row.ocr_json_path = None

    # Excel append (with lock-aware retries)
    excel_row_index = _append_rows_to_excel_on_onedrive(
        onedrive_client,
        output_excel_path,
        data_for_excel or {},
    )
    if excel_row_index <= 0:
        row.status = ScheduledFileStatus.WAITING_FOR_EXCEL
        row.error_message = (
            row.error_message or "Excel locked or write failed; will retry later"
        )
        db.commit()
        return False

    # Move file to history/YYYYMM
    from O365.drive import File as O365File, Folder as O365Folder  # type: ignore

    try:
        existing_names = set()
        for item in history_month_folder.get_items():
            if getattr(item, "is_file", False):
                existing_names.add(getattr(item, "name", ""))

        target_name = filename
        if target_name in existing_names:
            # Add timestamp suffix to avoid collision
            stem, dot, suffix = target_name.partition(".")
            ts = now.strftime("%Y%m%d_%H%M%S")
            target_name = f"{stem}_{ts}.{suffix}" if dot else f"{stem}_{ts}"

        onedrive_client.move_file(file_item, history_month_folder, new_name=target_name)
    except Exception as exc:
        logger.error(
            "❌ Failed to move file %s to history for schedule %s: %s",
            filename,
            schedule.schedule_id,
            exc,
        )
        # We still consider OCR+Excel successful; leave file in place for manual intervention.

    # Finalize
    row.status = ScheduledFileStatus.COMPLETED
    row.output_excel_path = output_excel_path
    row.excel_row_index = excel_row_index
    db.commit()
    return True


def _process_single_month(
    db: Session,
    schedule: OcrSchedule,
    onedrive_client,
    month_str: str,
    ocr_prompt: Optional[str],
    ocr_schema: Optional[dict],
) -> Dict[str, Any]:
    """Process OCR for a single month folder.

    Args:
        db: Database session
        schedule: OCR schedule configuration
        onedrive_client: Connected OneDrive client
        month_str: Month in YYYYMM format
        ocr_prompt: OCR prompt to use
        ocr_schema: OCR response schema to use

    Returns:
        Dictionary with processing results:
        {
            'month_str': str,
            'files_discovered': int,
            'files_processed': int,
            'files_failed': int,
            'error_message': Optional[str],
            'metadata': dict,
        }
    """
    result: Dict[str, Any] = {
        "month_str": month_str,
        "files_discovered": 0,
        "files_processed": 0,
        "files_failed": 0,
        "error_message": None,
        "metadata": {},
    }

    try:
        # Ensure month folder structure exists
        month_structure = ensure_month_structure(
            onedrive_client,
            schedule,
            month_str,
        )
        material_month = month_structure.material_folder
        failed_folder = month_structure.failed_folder
        history_month = month_structure.history_folder
        output_excel_path = month_structure.output_excel_path

        # Get max files limit
        max_files = getattr(schedule, "max_files_per_cycle", None)
        try:
            max_files_int = int(max_files) if max_files is not None else 10
        except Exception:
            max_files_int = 10

        result["metadata"] = {
            "max_files": max_files_int,
            "material_folder_path": month_structure.material_folder_path,
            "history_folder_path": month_structure.history_folder_path,
            "failed_folder_path": month_structure.failed_folder_path,
            "month_folder_path": month_structure.month_folder_path,
            "output_excel_path": month_structure.output_excel_path,
        }

        # Discover candidate files
        candidates = _discover_candidates(
            db=db,
            schedule=schedule,
            month_str=month_str,
            onedrive_client=onedrive_client,
            material_month_folder=material_month,
            material_month_path=month_structure.material_folder_path,
            max_files=max_files_int,
        )

        result["files_discovered"] = len(candidates)

        if not candidates:
            logger.info(
                "ℹ️ No OCR candidates found for schedule %s month %s",
                schedule.schedule_id,
                month_str,
            )
            return result

        logger.info(
            "🔄 Processing %s OCR files for schedule %s (month %s)",
            len(candidates),
            schedule.schedule_id,
            month_str,
        )

        # Process each file with file-level error isolation
        for file_item, row in candidates:
            try:
                success = _process_single_file(
                    db=db,
                    schedule=schedule,
                    onedrive_client=onedrive_client,
                    file_item=file_item,
                    row=row,
                    month_str=month_str,
                    history_month_folder=history_month,
                    failed_folder=failed_folder,
                    output_excel_path=output_excel_path,
                    ocr_prompt=ocr_prompt,
                    ocr_schema=ocr_schema,
                )
                if success:
                    result["files_processed"] += 1
                else:
                    result["files_failed"] += 1
            except Exception as file_exc:
                # File-level error isolation: single file failure doesn't affect others
                result["files_failed"] += 1
                filename = getattr(file_item, "name", "unknown")
                logger.error(
                    "❌ Unexpected error processing file %s in schedule %s month %s: %s",
                    filename,
                    schedule.schedule_id,
                    month_str,
                    file_exc,
                )
                # Try to update database record status
                try:
                    row.status = ScheduledFileStatus.ERROR
                    row.error_message = f"Unexpected error: {str(file_exc)[:200]}"
                    db.commit()
                except Exception:
                    pass  # Database update failure should not block other files

        logger.info(
            "✅ Completed month %s for schedule %s: discovered=%s processed=%s failed=%s",
            month_str,
            schedule.schedule_id,
            result["files_discovered"],
            result["files_processed"],
            result["files_failed"],
        )

    except Exception as exc:
        result["error_message"] = str(exc)
        logger.error(
            "❌ Error processing month %s for schedule %s: %s",
            month_str,
            schedule.schedule_id,
            exc,
        )

    return result


def process_schedule(schedule_id: int) -> None:
    """Process a single OCR schedule across all month folders with content.

    This function discovers all YYYYMM-format month folders and processes
    each one that contains files, implementing three levels of error isolation:
    1. Schedule level - failures don't affect other schedules
    2. Month level - one month's failure doesn't affect other months
    3. File level - one file's failure doesn't affect other files
    """
    db: Session = SessionLocal()
    try:
        schedule = db.query(OcrSchedule).filter(
            OcrSchedule.schedule_id == schedule_id
        ).one_or_none()
        if not schedule:
            logger.warning("⚠️ OCR schedule %s not found", schedule_id)
            return
        if not schedule.enabled:
            logger.info("ℹ️ OCR schedule %s is disabled; skipping", schedule_id)
            return

        now = _utcnow()

        # Create run record with month_str initially None (will be set later)
        run = OcrScheduleRun(
            schedule_id=schedule.schedule_id,
            status=ScheduleRunStatus.RUNNING,
            started_at=now,
            month_str=None,  # Will be set based on months processed
        )
        db.add(run)
        db.commit()
        db.refresh(run)
        logger.info(
            "📝 Created schedule run %s for schedule %s at %s",
            run.run_id,
            schedule.schedule_id,
            run.started_at,
        )

        # Aggregated counters across all months
        total_files_discovered = 0
        total_files_processed = 0
        total_files_failed = 0
        run_status = ScheduleRunStatus.RUNNING
        error_message: Optional[str] = None
        metadata_updates: Dict[str, Any] = {}
        run_notes: List[str] = []
        months_processed: List[str] = []
        month_results: List[Dict[str, Any]] = []

        try:
            onedrive_client = build_client_from_env()
            if not onedrive_client:
                logger.error(
                    "❌ Cannot build OneDrive client from env; aborting schedule %s",
                    schedule_id,
                )
                raise RuntimeError("Missing OneDrive credentials")
            if not onedrive_client.connect():
                logger.error(
                    "❌ Failed to connect to OneDrive for schedule %s", schedule_id
                )
                raise RuntimeError("Failed to connect to OneDrive")

            # Load OCR prompt and schema once for all months
            ocr_prompt, ocr_schema = _load_prompt_and_schema_for_schedule(schedule)

            # Discover all month folders
            discovered_months = _discover_all_month_folders(onedrive_client, schedule)

            if not discovered_months:
                logger.info(
                    "ℹ️ No month folders found for schedule %s",
                    schedule_id,
                )
                run_status = ScheduleRunStatus.SUCCESS
                run_notes.append("No month folders found")
                return

            logger.info(
                "🔄 Discovered %s month folder(s) for schedule %s: %s",
                len(discovered_months),
                schedule_id,
                ", ".join(discovered_months),
            )

            metadata_updates["discovered_months"] = discovered_months

            # Process each month with month-level error isolation
            for month_str in discovered_months:
                logger.info(
                    "📅 Processing month %s for schedule %s",
                    month_str,
                    schedule_id,
                )

                try:
                    month_result = _process_single_month(
                        db=db,
                        schedule=schedule,
                        onedrive_client=onedrive_client,
                        month_str=month_str,
                        ocr_prompt=ocr_prompt,
                        ocr_schema=ocr_schema,
                    )

                    # Aggregate counts
                    total_files_discovered += month_result.get("files_discovered", 0)
                    total_files_processed += month_result.get("files_processed", 0)
                    total_files_failed += month_result.get("files_failed", 0)

                    # Track months that actually had files
                    if month_result.get("files_discovered", 0) > 0:
                        months_processed.append(month_str)

                    month_results.append(month_result)

                    logger.info(
                        "✅ Completed month %s: discovered=%s processed=%s failed=%s",
                        month_str,
                        month_result.get("files_discovered", 0),
                        month_result.get("files_processed", 0),
                        month_result.get("files_failed", 0),
                    )

                except Exception as month_exc:
                    # Month-level error isolation: one month's failure doesn't affect others
                    logger.error(
                        "❌ Failed to process month %s for schedule %s: %s",
                        month_str,
                        schedule_id,
                        month_exc,
                    )
                    total_files_failed += 1
                    run_notes.append(f"Month {month_str} failed: {str(month_exc)[:100]}")
                    month_results.append({
                        "month_str": month_str,
                        "files_discovered": 0,
                        "files_processed": 0,
                        "files_failed": 0,
                        "error_message": str(month_exc),
                    })

            # Save month processing results to metadata
            metadata_updates["month_results"] = month_results
            metadata_updates["months_processed"] = months_processed

            run_status = ScheduleRunStatus.SUCCESS
            logger.info(
                "✅ Schedule %s run %s completed: %s month(s) processed, "
                "total discovered=%s processed=%s failed=%s",
                schedule.schedule_id,
                run.run_id,
                len(months_processed),
                total_files_discovered,
                total_files_processed,
                total_files_failed,
            )

        except Exception as exc:
            total_files_failed = max(total_files_failed, 1)
            error_message = str(exc)
            run_status = ScheduleRunStatus.FAILED
            logger.error(
                "❌ Schedule %s run %s failed: %s",
                schedule.schedule_id,
                run.run_id,
                exc,
            )
            raise
        finally:
            finished_at = _utcnow()
            run.status = run_status
            run.files_discovered = total_files_discovered
            run.files_processed = total_files_processed
            run.files_failed = total_files_failed
            run.error_message = error_message
            run.finished_at = finished_at

            # Set month_str: single month uses the field, multiple months use metadata
            if len(months_processed) == 1:
                run.month_str = months_processed[0]
            else:
                run.month_str = None  # Multiple months stored in metadata_payload

            if run.started_at:
                try:
                    run.duration_seconds = int(
                        max(0, (finished_at - run.started_at).total_seconds())
                    )
                except Exception:
                    run.duration_seconds = None

            metadata_payload = run.metadata_payload or {}
            if metadata_updates:
                metadata_payload = {**metadata_payload, **metadata_updates}
            if run_notes:
                metadata_payload = {**metadata_payload, "notes": run_notes}
            run.metadata_payload = metadata_payload or None
            db.commit()
    finally:
        try:
            db.close()
        except Exception:
            pass


def run_ocr_schedules() -> None:
    """Entry point for APScheduler.

    This function selects all due schedules and processes each in turn.
    """
    db: Session = SessionLocal()
    try:
        now = _utcnow()

        due_schedules: List[OcrSchedule] = (
            db.query(OcrSchedule)
            .filter(
                and_(
                    OcrSchedule.enabled.is_(True),
                    OcrSchedule.next_run_at <= now,
                )
            )
            .all()
        )

        if not due_schedules:
            return

        logger.info("🔄 Found %s due OCR schedules", len(due_schedules))

        for schedule in due_schedules:
            if schedule.start_at and now < schedule.start_at:
                continue
            if not _is_within_window(schedule, now):
                logger.info(
                    "⏸️ Schedule %s is outside configured window; skipping this cycle",
                    schedule.schedule_id,
                )
                # Push next_run_at forward to avoid hammering
                schedule.next_run_at = now + timedelta(
                    seconds=schedule.interval_seconds or 300
                )
                db.commit()
                continue

            # Advance next_run_at before processing to avoid re-selection
            schedule.last_run_at = now
            schedule.next_run_at = now + timedelta(
                seconds=schedule.interval_seconds or 300
            )
            db.commit()

            try:
                process_schedule(schedule.schedule_id)
            except Exception as exc:
                logger.error(
                    "❌ Error while processing OCR schedule %s: %s",
                    schedule.schedule_id,
                    exc,
                )
    finally:
        try:
            db.close()
        except Exception:
            pass
