"""Background OCR schedule runner.

This module implements the recurring logic for:
- Selecting due OCR schedules
- Ensuring monthly OneDrive folder / Excel structure
- Discovering new material files
- Running OCR and appending results to Excel
- Moving processed files to history and tracking status in DB
"""

import json
import logging
import os
import tempfile
from dataclasses import dataclass
from datetime import datetime, time, timedelta
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
from main import extract_text_from_pdf
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


def _utcnow() -> datetime:
    return datetime.utcnow()


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
    return now.strftime("%Y%m")


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

    failed_folder = onedrive_client.get_or_create_folder(material_month, failed_name)
    if not failed_folder:
        raise RuntimeError(
            f"Failed to ensure failed folder {failed_name} under {material_name}"
        )

    history_month = onedrive_client.get_or_create_folder(month_folder, history_name)
    if not history_month:
        raise RuntimeError(
            f"Failed to create/find history folder {history_name} under {month_folder_path}"
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
    failed_folder_path = join_onedrive_path(material_month_path, failed_name)

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
        pdf_items: List[O365File] = onedrive_client.list_all_pdfs(
            material_month_folder,
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
            if row.status in (
                ScheduledFileStatus.COMPLETED,
                ScheduledFileStatus.PROCESSING,
            ):
                continue
            # Allow retries for WAITING_FOR_EXCEL or ERROR
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
    max_retries: int = 3,
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

    last_exc: Optional[Exception] = None
    for attempt in range(max_retries):
        try:
            # Download existing workbook if present; otherwise create new one.
            content = onedrive_client.download_file_content(excel_path)
            if content:
                wb = openpyxl.load_workbook(io.BytesIO(content))
                ws = wb.active
                # Existing header from first row
                existing_headers = [
                    cell.value for cell in next(ws.iter_rows(min_row=1, max_row=1))
                ]
                header_cols = [str(h) for h in existing_headers]
                # NOTE: For now we do NOT auto-extend headers if new keys appear later.
                # This keeps the sheet schema stable once created.
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

            # Upload back to same path
            normalized_path = normalise_onedrive_path(excel_path)
            file_item = onedrive_client.drive.get_item_by_path(normalized_path)
            if file_item:
                # Replace existing content
                file_item.upload(data)
            else:
                # Create a new file under the parent folder
                parent_path = "/".join(normalized_path.split("/")[:-1])
                parent_folder = onedrive_client.get_folder(parent_path)
                if not parent_folder:
                    logger.error(
                        "❌ Failed to resolve parent folder %s when uploading Excel",
                        parent_path,
                    )
                    return -1
                parent_folder.upload_file(
                    data=data,
                    name=normalized_path.split("/")[-1],
                )

            logger.info("✅ Appended row to Excel at %s (row %s)", excel_path, start_row_index)
            return start_row_index

        except Exception as exc:
            last_exc = exc
            logger.warning(
                "⚠️ Excel append attempt %s/%s failed for %s: %s",
                attempt + 1,
                max_retries,
                excel_path,
                exc,
            )
            if attempt < max_retries - 1:
                # Patient retry strategy: 5s, then 10s, etc.
                delay = 5 * (attempt + 1)
                logger.info("⏳ Waiting %s seconds before retrying Excel append", delay)
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
            ocr_result = extract_text_from_pdf(local_path)
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
    try:
        row.ocr_json_path = json.dumps(ocr_result, ensure_ascii=False)
    except Exception:
        # Best-effort; keep going even if we cannot serialize
        row.ocr_json_path = None

    # Excel append (with lock-aware retries)
    excel_row_index = _append_rows_to_excel_on_onedrive(
        onedrive_client,
        output_excel_path,
        ocr_result or {},
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
                run.duration_seconds = int(
                    max(0, (finished_at - run.started_at).total_seconds())
                )
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
