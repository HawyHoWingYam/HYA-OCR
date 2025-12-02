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
    ScheduleMode,
    ScheduledFileStatus,
    ScheduleRunStatus,
)
from main import extract_text_from_pdf, extract_text_from_image
from utils.prompt_schema_manager import load_prompt_and_schema
from utils.order_processor import escape_excel_formulas
from utils.onedrive_client import (
    build_client_from_env,
    normalise_onedrive_path,
    join_onedrive_path,
)

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


def _append_rows_to_excel_on_onedrive(
    onedrive_client,
    excel_path: str,
    ocr_json: Dict[str, Any],
    max_retries: int = 2,
) -> int:
    """Append flattened OCR JSON rows to Excel file on OneDrive, handling lock/retry.

    Uses the same deep-flattening strategy as json_to_csv/deep_flatten_json_universal so
    that structure is driven by the document schema and Gemini output, not by a
    per-schedule Excel configuration.

    Returns the 1-based Excel row index of the first appended row, or -1 on failure.
    """
    import io

    try:
        import openpyxl  # type: ignore
    except Exception as exc:  # pragma: no cover - environment guard
        logger.error("❌ openpyxl is required for Excel append but not installed: %s", exc)
        return -1

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
                # Detect OneDrive/Graph file-lock situations (HTTP 423) more robustly.
                is_locked = False
                status_code = getattr(getattr(exc, "response", None), "status_code", None)
                if status_code == 423:
                    is_locked = True
                else:
                    message = str(exc).lower()
                    if (
                        "resourcelocked" in message
                        or " 423 " in message
                        or "423 client error" in message
                    ):
                        is_locked = True

                if is_locked:
                    delay = 60
                    logger.info(
                        "⏳ Excel file appears locked (423). Waiting %s seconds before retrying",
                        delay,
                    )
                else:
                    delay = 5 * (attempt + 1)
                    logger.info(
                        "⏳ Waiting %s seconds before retrying Excel append", delay
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


def process_schedule(schedule_id: int) -> None:
    """Process a single OCR schedule if its OneDrive roots and config are valid."""
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

        # Use UTC for storage/ordering, but derive month and logs based on HK local time.
        now = _utcnow()
        month_str = _get_current_month_str(now)

        run = OcrScheduleRun(
            schedule_id=schedule.schedule_id,
            status=ScheduleRunStatus.RUNNING,
            started_at=now,
            month_str=month_str,
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

        files_discovered = 0
        files_processed = 0
        files_failed = 0
        run_status = ScheduleRunStatus.RUNNING
        error_message: Optional[str] = None
        metadata_updates: Dict[str, Any] = {}
        run_notes: List[str] = []

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

            month_structure = ensure_month_structure(
                onedrive_client,
                schedule,
                month_str,
            )
            material_month = month_structure.material_folder
            failed_folder = month_structure.failed_folder
            history_month = month_structure.history_folder
            output_excel_path = month_structure.output_excel_path

            max_files = getattr(schedule, "max_files_per_cycle", None)
            try:
                max_files_int = int(max_files) if max_files is not None else 10
            except Exception:
                max_files_int = 10
            metadata_updates.update(
                {
                    "max_files": max_files_int,
                    "material_folder_path": month_structure.material_folder_path,
                    "history_folder_path": month_structure.history_folder_path,
                    "failed_folder_path": month_structure.failed_folder_path,
                    "month_folder_path": month_structure.month_folder_path,
                    "output_excel_path": month_structure.output_excel_path,
                }
            )

            ocr_prompt, ocr_schema = _load_prompt_and_schema_for_schedule(schedule)

            candidates = _discover_candidates(
                db=db,
                schedule=schedule,
                month_str=month_str,
                onedrive_client=onedrive_client,
                material_month_folder=material_month,
                material_month_path=month_structure.material_folder_path,
                max_files=max_files_int,
            )

            files_discovered = len(candidates)
            if not candidates:
                logger.info("ℹ️ No OCR candidates found for schedule %s", schedule_id)
                run_status = ScheduleRunStatus.SUCCESS
                run_notes.append("No OCR candidates found")
                return

            logger.info(
                "🔄 Processing %s OCR files for schedule %s (month %s)",
                len(candidates),
                schedule_id,
                month_str,
            )

            for file_item, row in candidates:
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
                    files_processed += 1
                else:
                    files_failed += 1

            run_status = ScheduleRunStatus.SUCCESS
            logger.info(
                "✅ Schedule %s run %s completed: processed=%s failed=%s",
                schedule.schedule_id,
                run.run_id,
                files_processed,
                files_failed,
            )

        except Exception as exc:
            files_failed = max(files_failed, 0)
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
            run.files_discovered = files_discovered
            run.files_processed = files_processed
            run.files_failed = files_failed
            run.error_message = error_message
            run.finished_at = finished_at
            if run.started_at:
                # Both timestamps are stored as naive UTC; direct subtraction
                # yields a duration in seconds.
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
