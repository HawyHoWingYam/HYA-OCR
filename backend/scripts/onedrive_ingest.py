"""OneDrive daily sync script - Downloads new AWB PDFs to S3"""
import logging
import os
import tempfile
from datetime import datetime, timedelta
from pathlib import Path

from sqlalchemy import create_engine
from sqlalchemy.orm import sessionmaker

from utils.onedrive_client import OneDriveClient
from utils.s3_storage import get_s3_manager
from db.models import File, OneDriveSync

# Configure logging
logging.basicConfig(
    level=logging.INFO,
    format='%(asctime)s - %(name)s - %(levelname)s - %(message)s'
)
logger = logging.getLogger(__name__)

# Get configuration
ONEDRIVE_CLIENT_ID = os.getenv('ONEDRIVE_CLIENT_ID')
ONEDRIVE_CLIENT_SECRET = os.getenv('ONEDRIVE_CLIENT_SECRET')
ONEDRIVE_TENANT_ID = os.getenv('ONEDRIVE_TENANT_ID')
ONEDRIVE_TARGET_USER_UPN = os.getenv('ONEDRIVE_TARGET_USER_UPN')  # NEW: Target user for app-only access
ONEDRIVE_SHARED_FOLDER_PATH = os.getenv('ONEDRIVE_SHARED_FOLDER_PATH', 'Documents/AWB')
# Optional: a '|' separated candidate list of alternative paths to try when the primary does not exist
# e.g. "HYA-OCR/AWB|Documents/HYA-OCR/AWB|AWB"
ONEDRIVE_SHARED_FOLDER_CANDIDATES = os.getenv('ONEDRIVE_SHARED_FOLDER_CANDIDATES', '')
ONEDRIVE_PROCESSED_FOLDER = os.getenv('ONEDRIVE_PROCESSED_FOLDER', '已处理')
# Note: S3StorageManager auto-adds 'upload/' prefix, so we avoid double prefix here
AWB_S3_PREFIX = os.getenv('AWB_S3_PREFIX', 'onedrive/airway-bills')

# Database setup
DATABASE_URL = os.getenv('DATABASE_URL')
if not DATABASE_URL:
    raise Exception("DATABASE_URL environment variable not set")
engine = create_engine(DATABASE_URL)
SessionLocal = sessionmaker(autocommit=False, autoflush=False, bind=engine)


def get_db_session():
    """Get database session"""
    return SessionLocal()


def reconcile_by_filename(db, onedrive_client, s3_manager, month: str,
                         scan_processed: bool = True, min_size_bytes: int = None):
    """Reconcile OneDrive and S3 by filename for a given month.

    Uploads missing files and repairs undersized (tiny) files in S3.

    Args:
        db: Database session
        onedrive_client: Connected OneDriveClient instance
        s3_manager: S3StorageManager instance
        month: Month in YYYY-MM format (e.g., "2025-10")
        scan_processed: If True, also scan OneDrive processed folder for files from this month
        min_size_bytes: Minimum file size threshold (uses env var if None)

    Returns:
        dict: Statistics including {missing_uploaded, tiny_repaired, existing_ok, scanned_onedrive_*, errors, min_size}
    """
    try:
        if min_size_bytes is None:
            min_size_bytes = int(os.getenv("AWB_S3_MIN_FILE_SIZE_BYTES", "10240"))

        logger.info(f"🔄 Starting filename-based reconciliation for {month}")

        # Parse month for folder path and S3 path generation
        try:
            year, mm = month.split('-')
            month_str = f"{year}{mm}"
            target_year = int(year)
            target_month = int(mm)
        except (ValueError, IndexError):
            logger.error(f"❌ Invalid month format: {month}. Expected YYYY-MM")
            return {"error": f"Invalid month format: {month}"}

        # Initialize counters
        stats = {
            "scanned_onedrive_month": 0,
            "scanned_onedrive_processed": 0,
            "s3_existing_ok": 0,
            "missing_uploaded": 0,
            "tiny_repaired": 0,
            "errors": [],
            "min_size": min_size_bytes,
        }

        # Resolve OneDrive source folder with graceful fallbacks
        def _resolve_source_folder():
            # Build candidate list: primary -> explicit candidates -> with/without 'Documents/' prefix
            tried = []
            primary = (ONEDRIVE_SHARED_FOLDER_PATH or '').strip('/')
            candidates = [c.strip('/') for c in [primary] if c]
            # Append user-provided candidates (highest precedence after primary)
            if ONEDRIVE_SHARED_FOLDER_CANDIDATES:
                for token in ONEDRIVE_SHARED_FOLDER_CANDIDATES.split('|'):
                    token = token.strip()
                    if token:
                        candidates.append(token.strip('/'))
            # Add automatic variants for each candidate
            variants = []
            for c in candidates:
                variants.append(c)
                if not c.lower().startswith('documents/'):
                    variants.append(f'Documents/{c}')
                else:
                    # Also try without 'Documents/'
                    variants.append(c[len('Documents/'):])
            # Deduplicate while preserving order
            seen = set(); ordered = []
            for v in variants:
                if v and v not in seen:
                    ordered.append(v); seen.add(v)
            # Try each variant until one resolves
            for path in ordered:
                folder = onedrive_client.get_folder(path)
                tried.append(path)
                if folder:
                    logger.info(f"📂 Using OneDrive source folder: {path}")
                    return folder, tried
            return None, tried

        source_folder, tried_paths = _resolve_source_folder()
        if not source_folder:
            logger.error(f"❌ Source folder not found. Tried: {tried_paths}")
            stats["errors"].append(f"Source folder not found. Tried: {tried_paths}")
            return stats

        month_folder = onedrive_client.get_or_create_folder(source_folder, month_str)
        if not month_folder:
            logger.warning(f"⚠️ Could not get/create month folder {month_str}")
            month_folder = source_folder

        # List OneDrive PDFs from month folder
        one_month_pdfs = onedrive_client.list_all_pdfs(month_folder)
        stats["scanned_onedrive_month"] = len(one_month_pdfs)
        logger.info(f"📁 Found {len(one_month_pdfs)} PDFs in OneDrive month folder {month_str}")

        # Optionally scan monthly 'history' subfolder (new behavior)
        one_processed_pdfs = []
        if scan_processed:
            history_sub = onedrive_client.get_or_create_folder(month_folder, 'history') if month_folder else None
            if history_sub:
                one_processed_pdfs = onedrive_client.list_all_pdfs(history_sub, created_month_filter=month)
                stats["scanned_onedrive_processed"] = len(one_processed_pdfs)
                logger.info(f"📁 Found {len(one_processed_pdfs)} PDFs in OneDrive monthly history folder for {month}")

        # Combine candidates
        all_one_candidates = one_month_pdfs + one_processed_pdfs
        logger.info(f"📊 Total OneDrive candidates: {len(all_one_candidates)}")

        # Create S3 filename index for this month
        s3_index = s3_manager.index_awb_month_by_name(month)
        logger.info(f"📊 S3 index has {len(s3_index)} unique filenames")

        # Process each OneDrive candidate
        for file_item in all_one_candidates:
            try:
                filename = file_item.name
                logger.info(f"📄 Processing: {filename}")

                if filename not in s3_index:
                    # File is missing in S3 - upload it
                    logger.info(f"🔴 Missing in S3: {filename}. Will upload.")

                    with tempfile.TemporaryDirectory() as temp_dir:
                        if not onedrive_client.download_file(file_item, temp_dir):
                            raise Exception(f"Failed to download {filename}")

                        # Create canonical S3 path using specified month (without day layer)
                        # Clean prefix: remove leading 'upload/' to avoid double prefix
                        prefix = AWB_S3_PREFIX.lstrip('/')
                        if prefix.startswith('upload/'):
                            prefix = prefix[len('upload/'):]

                        s3_key = f"{prefix}/{target_year:04d}/{target_month:02d}/{filename}"

                        temp_file_path = os.path.join(temp_dir, filename)
                        with open(temp_file_path, 'rb') as file_obj:
                            upload_success = s3_manager.upload_file(file_obj, s3_key, content_type='application/pdf')
                            if not upload_success:
                                raise Exception(f"S3 upload failed for {filename}")

                        # Record in database if not already there
                        source_path = f"onedrive://{source_folder.name}_{file_item.object_id}"
                        existing = db.query(File).filter(File.source_path == source_path).first()

                        if not existing:
                            # Extract created_by safely
                            created_by_str = None
                            if file_item.created_by:
                                created_by = str(file_item.created_by)
                                if '@' in created_by:
                                    created_by_str = created_by.split('@')[0]
                                else:
                                    created_by_str = created_by

                            file_record = File(
                                file_name=filename,
                                file_path=s3_key,
                                file_type='application/pdf',
                                s3_bucket=s3_manager.bucket_name,
                                s3_key=s3_key,
                                source_system='onedrive',
                                source_path=source_path,
                                source_metadata={
                                    'onedrive_id': file_item.object_id,
                                    'modified': file_item.modified.isoformat() if file_item.modified else None,
                                    'created_by': created_by_str,
                                }
                            )
                            db.add(file_record)
                            db.commit()
                            logger.info(f"💾 Recorded in DB: {filename}")

                        logger.info(f"✅ Uploaded missing file: {s3_key}")
                        stats["missing_uploaded"] += 1

                elif s3_index[filename]["any_tiny"]:
                    # File exists but is tiny - repair it (overwrite)
                    logger.info(f"🟡 Tiny file in S3: {filename}. Will repair.")

                    with tempfile.TemporaryDirectory() as temp_dir:
                        if not onedrive_client.download_file(file_item, temp_dir):
                            raise Exception(f"Failed to download {filename}")

                        # Use canonical S3 path for this month (same as for missing files)
                        prefix = AWB_S3_PREFIX.lstrip('/')
                        if prefix.startswith('upload/'):
                            prefix = prefix[len('upload/'):]
                        s3_key_to_repair = f"{prefix}/{target_year:04d}/{target_month:02d}/{filename}"

                        temp_file_path = os.path.join(temp_dir, filename)
                        with open(temp_file_path, 'rb') as file_obj:
                            upload_success = s3_manager.upload_file(file_obj, s3_key_to_repair, content_type='application/pdf')
                            if not upload_success:
                                raise Exception(f"S3 re-upload failed for {filename}")

                        # Update database with current filename and S3 key
                        source_path = f"onedrive://{source_folder.name}_{file_item.object_id}"
                        existing = db.query(File).filter(File.source_path == source_path).first()
                        if existing:
                            existing.file_name = filename
                            existing.file_path = s3_key_to_repair
                            existing.s3_key = s3_key_to_repair
                            db.commit()
                            logger.info(f"💾 Updated DB with current filename: {filename}")

                        logger.info(f"✅ Repaired tiny file: {s3_key_to_repair}")
                        stats["tiny_repaired"] += 1

                else:
                    # File exists in S3 and is OK
                    logger.debug(f"✓ OK in S3: {filename}")
                    stats["s3_existing_ok"] += 1

            except Exception as e:
                error_msg = f"{filename}: {str(e)}"
                logger.error(f"❌ Error processing {error_msg}")
                stats["errors"].append(error_msg)

        logger.info(f"✅ Reconciliation complete: "
                   f"missing_uploaded={stats['missing_uploaded']}, "
                   f"tiny_repaired={stats['tiny_repaired']}, "
                   f"existing_ok={stats['s3_existing_ok']}, "
                   f"errors={len(stats['errors'])}")

        return stats

    except Exception as e:
        logger.error(f"❌ Error during reconciliation: {str(e)}")
        return {"error": str(e), "errors": [str(e)]}


def run_onedrive_sync(month: str = None, force: bool = False, reconcile: bool = False,
                     scan_processed: bool = True):
    """Run OneDrive sync: Download new AWBs to S3 with optional reconciliation.

    Args:
        month: Optional month in format YYYY-MM (e.g., "2025-10") to sync only that month folder
        force: If True, ignore last_sync_time and re-scan all files; also re-upload tiny/corrupted objects
        reconcile: If True, perform filename-based reconciliation instead of incremental sync
        scan_processed: If True, scan OneDrive processed folder during reconciliation
    """
    sync_start = datetime.utcnow()
    db = get_db_session()

    try:
        logger.info(f"🔄 Starting OneDrive sync... (month={month}, force={force}, reconcile={reconcile}, scan_processed={scan_processed}) [types: month={type(month).__name__ if month else 'NoneType'}, force={type(force).__name__}, reconcile={type(reconcile).__name__}]")

        # Initialize clients
        onedrive_client = OneDriveClient(
            client_id=ONEDRIVE_CLIENT_ID,
            client_secret=ONEDRIVE_CLIENT_SECRET,
            tenant_id=ONEDRIVE_TENANT_ID,
            target_user_upn=ONEDRIVE_TARGET_USER_UPN  # NEW: App-only access to target user
        )

        s3_manager = get_s3_manager()
        if not s3_manager:
            raise Exception("S3 storage not available (ensure STORAGE_BACKEND=s3 and S3 env vars are set)")

        # Connect to OneDrive
        if not onedrive_client.connect():
            raise Exception("Failed to connect to OneDrive")

        # Get source and processed folders
        def _resolve_source_folder2():
            tried = []
            primary = (ONEDRIVE_SHARED_FOLDER_PATH or '').strip('/')
            candidates = [c.strip('/') for c in [primary] if c]
            if ONEDRIVE_SHARED_FOLDER_CANDIDATES:
                for token in ONEDRIVE_SHARED_FOLDER_CANDIDATES.split('|'):
                    token = token.strip()
                    if token:
                        candidates.append(token.strip('/'))
            variants = []
            for c in candidates:
                variants.append(c)
                if not c.lower().startswith('documents/'):
                    variants.append(f'Documents/{c}')
                else:
                    variants.append(c[len('Documents/'):])
            seen = set(); ordered = []
            for v in variants:
                if v and v not in seen:
                    ordered.append(v); seen.add(v)
            for path in ordered:
                folder = onedrive_client.get_folder(path)
                tried.append(path)
                if folder:
                    logger.info(f"📂 Using OneDrive source folder: {path}")
                    return folder, tried
            return None, tried

        source_folder, tried_paths = _resolve_source_folder2()
        if not source_folder:
            raise Exception(f"Source folder not found. Tried: {tried_paths}")

        # Determine month folder
        # If month parameter provided, use it; otherwise use current month
        if month:
            # month parameter is in format YYYY-MM, convert to YYYYMM for folder lookup
            try:
                year, mm = month.split('-')
                month_str = f"{year}{mm}"
            except (ValueError, IndexError):
                logger.warning(f"⚠️  Invalid month format '{month}', expected YYYY-MM. Using current month.")
                month_str = datetime.utcnow().strftime('%Y%m')
        else:
            # Auto-detect current month folder (e.g., "202510" for Oct 2025)
            # This avoids needing to manually change env var each month
            month_str = datetime.utcnow().strftime('%Y%m')

        month_folder = onedrive_client.get_or_create_folder(source_folder, month_str)
        if month_folder:
            logger.info(f"📁 Using month folder: {month_str}")
            sync_folder = month_folder
        else:
            logger.warning(f"⚠️  Could not create/find month folder {month_str}, falling back to source folder")
            sync_folder = source_folder

        # Determine a per-month history folder under the chosen sync folder
        # Requirement: after syncing monthly subdirectory files, move them into '<YYYYMM>/history' instead of deleting.
        history_folder = onedrive_client.get_or_create_folder(sync_folder, 'history')

        # Get last sync time from database (for deduplication and incremental sync)
        last_sync = db.query(OneDriveSync).order_by(
            OneDriveSync.created_at.desc()
        ).first()

        # Determine since_date based on force flag and sync history
        if force:
            # Force rescan: use epoch time to re-scan all files
            since_date = datetime(1970, 1, 1, tzinfo=None)
            logger.info(f"🔄 Force rescan enabled: will re-scan all files from epoch")
        elif last_sync:
            # Use last sync time for incremental sync
            since_date = last_sync.last_sync_time
            logger.info(f"📅 Incremental sync: syncing files modified after: {since_date}")
        else:
            # First sync: pull all files from last 30 days
            since_date = datetime.utcnow() - timedelta(days=30)
            logger.info(f"📅 Initial sync: syncing files modified after: {since_date}")

        # List new files from the month folder
        new_files = onedrive_client.list_new_files(
            sync_folder,
            since_date,
            file_extensions=['.pdf']
        )

        if not new_files:
            logger.info("✅ No new files found in incremental scan")

            # Check if we should do auto-reconcile
            auto_reconcile = os.getenv('AUTO_RECONCILE_IF_EMPTY', 'false').lower() == 'true'
            if not reconcile and auto_reconcile:
                logger.info("🔄 AUTO_RECONCILE_IF_EMPTY enabled - performing reconciliation")
                reconcile = True

            # If reconcile mode, run reconciliation instead
            if reconcile:
                # Determine month for reconciliation
                reconcile_month = month or datetime.utcnow().strftime('%Y-%m')
                logger.info(f"🔄 Running filename-based reconciliation for month: {reconcile_month}")

                recon_stats = reconcile_by_filename(
                    db, onedrive_client, s3_manager,
                    reconcile_month,
                    scan_processed=scan_processed
                )

                # Record reconciliation stats
                record_sync(db, 'success', 0, 0, None, sync_metadata={
                    'mode': 'reconcile',
                    'month': reconcile_month,
                    **recon_stats
                })
                db.close()
                return

            # No new files and no reconciliation
            logger.info("✅ No new files to process and reconciliation not requested")
            record_sync(db, 'success', 0, 0, None)
            db.close()
            return

        # Process each file
        files_processed = 0
        files_failed = 0
        errors = []

        for file_item in new_files:
            try:
                logger.info(f"📄 Processing: {file_item.name}")

                # Check for duplicates using file object_id and parent folder info
                source_path = f"onedrive://{source_folder.name}_{file_item.object_id}"
                existing = db.query(File).filter(File.source_path == source_path).first()

                if existing:
                    # Always generate month-only S3 key with current OneDrive filename
                    min_size_bytes = int(os.getenv("AWB_S3_MIN_FILE_SIZE_BYTES", "10240"))

                    # Helper: Compute target year/month for this run
                    def _compute_target_ym():
                        if month:
                            try:
                                y, m = month.split('-')
                                return int(y), int(m)
                            except Exception:
                                pass
                        if file_item.modified:
                            return file_item.modified.year, file_item.modified.month
                        now = datetime.utcnow()
                        return now.year, now.month

                    # Helper: Build canonical month-only key with current filename
                    def _build_target_key(file_name: str) -> str:
                        ty, tm = _compute_target_ym()
                        prefix = AWB_S3_PREFIX.lstrip('/')
                        if prefix.startswith('upload/'):
                            prefix = prefix[len('upload/'):]
                        return f"{prefix}/{ty:04d}/{tm:02d}/{file_name}"

                    # Build target key using current OneDrive filename and computed month
                    target_key = _build_target_key(file_item.name)

                    # Check if S3 object exists and its size
                    info = s3_manager.get_file_info(existing.s3_key)  # returns None if 404
                    object_size = info["size"] if info else None

                    if info is None:
                        # Missing → backfill to target_key
                        logger.info(f"🔴 Missing in S3: {file_item.name}. Will backfill to: {target_key}")
                        with tempfile.TemporaryDirectory() as temp_dir:
                            if not onedrive_client.download_file(file_item, temp_dir):
                                raise Exception(f"Failed to download {file_item.name}")
                            temp_file_path = os.path.join(temp_dir, file_item.name)
                            with open(temp_file_path, 'rb') as f:
                                ok = s3_manager.upload_file(f, target_key, content_type='application/pdf')
                                if not ok:
                                    raise Exception(f"S3 upload failed for {file_item.name} to key {target_key}")
                        # Sync DB to current filename and new path
                        existing.file_name = file_item.name
                        existing.file_path = target_key
                        existing.s3_key = target_key
                        db.commit()
                        logger.info(f"☁️  Re-uploaded missing S3 object to: {target_key}")
                        files_processed += 1
                        continue

                    if object_size < min_size_bytes:
                        # Tiny → repair to target_key (overwrite canonical place)
                        logger.warning(f"🔧 Detected corrupted S3 object (size={object_size} bytes < threshold {min_size_bytes}): {existing.s3_key}")
                        logger.info(f"🔄 Repairing to canonical location: {target_key}")
                        with tempfile.TemporaryDirectory() as temp_dir:
                            if not onedrive_client.download_file(file_item, temp_dir):
                                raise Exception(f"Failed to re-download {file_item.name}")
                            temp_file_path = os.path.join(temp_dir, file_item.name)
                            with open(temp_file_path, 'rb') as f:
                                ok = s3_manager.upload_file(f, target_key, content_type='application/pdf')
                                if not ok:
                                    raise Exception(f"S3 re-upload failed for {file_item.name} to key {target_key}")
                        # Update DB with new location and current filename
                        existing.file_name = file_item.name
                        existing.file_path = target_key
                        existing.s3_key = target_key
                        db.commit()
                        logger.info(f"✅ Repaired tiny object to: {target_key}")
                        files_processed += 1
                        continue

                    # Already processed and OK
                    logger.info(f"⏭️  File already processed (skipping): {file_item.name}")
                    files_processed += 1
                    continue

                # Download to temp location
                with tempfile.TemporaryDirectory() as temp_dir:
                    if not onedrive_client.download_file(file_item, temp_dir):
                        raise Exception(f"Failed to download {file_item.name}")

                    # Upload to S3 with organized path using specified month (without day layer)
                    # Parse specified month or use current month
                    if month:
                        try:
                            year, mm = month.split('-')
                            target_year = int(year)
                            target_month = int(mm)
                        except (ValueError, IndexError):
                            logger.warning(f"⚠️ Invalid month format '{month}', using current month")
                            target_year = datetime.now().year
                            target_month = datetime.now().month
                    else:
                        target_year = datetime.now().year
                        target_month = datetime.now().month

                    # Clean prefix and build S3 key
                    prefix = AWB_S3_PREFIX.lstrip('/')
                    if prefix.startswith('upload/'):
                        prefix = prefix[len('upload/'):]
                    s3_key = f"{prefix}/{target_year:04d}/{target_month:02d}/{file_item.name}"

                    temp_file_path = os.path.join(temp_dir, file_item.name)
                    # Upload file object instead of path string
                    with open(temp_file_path, 'rb') as file_obj:
                        upload_success = s3_manager.upload_file(file_obj, s3_key, content_type='application/pdf')
                        if not upload_success:
                            raise Exception(f"S3 upload failed for {file_item.name} to key {s3_key}")
                    logger.info(f"☁️  Uploaded to S3: {s3_key}")

                    # Record in database
                    # Extract created_by safely
                    created_by_str = None
                    if file_item.created_by:
                        created_by = str(file_item.created_by)  # Convert to string first
                        if '@' in created_by:
                            created_by_str = created_by.split('@')[0]
                        else:
                            created_by_str = created_by

                    file_record = File(
                        file_name=file_item.name,
                        file_path=s3_key,  # Store S3 path
                        file_type='application/pdf',
                        s3_bucket=s3_manager.bucket_name,
                        s3_key=s3_key,
                        source_system='onedrive',
                        source_path=source_path,
                        source_metadata={
                            'onedrive_id': file_item.object_id,
                            'modified': file_item.modified.isoformat() if file_item.modified else None,
                            'created_by': created_by_str,
                        }
                    )
                    db.add(file_record)
                    db.commit()
                    logger.info(f"💾 Recorded in DB: {file_item.name}")

                    # Move file into the monthly 'history' folder instead of deleting
                    if history_folder:
                        move_success = onedrive_client.move_file(file_item, history_folder)
                        if move_success:
                            logger.info(f"✅ Moved to monthly history: {file_item.name}")
                        else:
                            logger.warning(f"⚠️ Failed to move to monthly history: {file_item.name}")

                files_processed += 1

            except Exception as e:
                logger.error(f"❌ Error processing {file_item.name}: {str(e)}")
                files_failed += 1
                errors.append(f"{file_item.name}: {str(e)}")

        # Run reconciliation if requested (even when new files were found)
        recon_stats = None
        if reconcile:
            reconcile_month = month or datetime.utcnow().strftime('%Y-%m')
            logger.info(f"🔄 Running filename-based reconciliation after incremental for month: {reconcile_month}")
            recon_stats = reconcile_by_filename(
                db, onedrive_client, s3_manager,
                reconcile_month,
                scan_processed=scan_processed
            )

        # Record final sync status
        error_message = '\n'.join(errors) if errors else None
        metadata = {'mode': 'incremental' + ('+reconcile' if reconcile else ''), 'month': month}
        if recon_stats:
            metadata.update(recon_stats)
        record_sync(db, 'success', files_processed, files_failed, error_message, sync_metadata=metadata)

        logger.info(f"✅ OneDrive sync completed: {files_processed} processed, {files_failed} failed")

    except Exception as e:
        logger.error(f"❌ OneDrive sync failed: {str(e)}")
        record_sync(db, 'failed', 0, 0, str(e))

    finally:
        db.close()
        onedrive_client.close()


def record_sync(db, status: str, files_processed: int, files_failed: int, error_msg: str = None,
               sync_metadata: dict = None):
    """Record sync status in database"""
    try:
        metadata = {
            's3_prefix': AWB_S3_PREFIX,
            'onedrive_folder': ONEDRIVE_SHARED_FOLDER_PATH
        }
        if sync_metadata:
            metadata.update(sync_metadata)

        sync_record = OneDriveSync(
            last_sync_time=datetime.utcnow(),
            sync_status=status,
            files_processed=files_processed,
            files_failed=files_failed,
            error_message=error_msg,
            sync_metadata=metadata
        )
        db.add(sync_record)
        db.commit()
        logger.info(f"📊 Recorded sync: {status} ({files_processed} processed, {files_failed} failed)")
    except Exception as e:
        logger.error(f"⚠️  Failed to record sync: {str(e)}")


if __name__ == '__main__':
    run_onedrive_sync()
