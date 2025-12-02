"""OneDrive integration client using O365 library"""
import logging
import os
from datetime import datetime, timedelta, timezone
from typing import List, Optional
from O365 import Account
from O365.drive import File as O365File, Folder as O365Folder

logger = logging.getLogger(__name__)


class OneDriveClient:
    """Client for OneDrive operations"""

    def __init__(
        self,
        client_id: str,
        client_secret: str,
        tenant_id: str,
        target_user_upn: Optional[str] = None,
        scopes: Optional[List[str]] = None
    ):
        """Initialize OneDrive client

        Args:
            client_id: Azure AD application client ID
            client_secret: Azure AD application client secret
            tenant_id: Azure AD tenant ID
            target_user_upn: Target user UPN for app-only access (e.g., user@domain.com)
            scopes: OAuth scopes (default: Files.ReadWrite.All)
        """
        self.client_id = client_id
        self.client_secret = client_secret
        self.tenant_id = tenant_id
        self.target_user_upn = target_user_upn

        if scopes is None:
            scopes = ['https://graph.microsoft.com/Files.ReadWrite.All']

        self.scopes = scopes
        self.account = None
        self.storage = None
        self.drive = None

    def connect(self) -> bool:
        """Authenticate and establish connection.

        Returns False on any auth failure instead of logging misleading success.
        """
        try:
            credentials = (self.client_id, self.client_secret)

            # Create account with client credentials flow
            self.account = Account(
                credentials,
                auth_flow_type='credentials',
                tenant_id=self.tenant_id
            )

            # Authenticate (client credentials flow)
            # Some O365 versions may not raise on failure; verify the final auth state explicitly.
            if not self.account.is_authenticated:
                auth_ok = self.account.authenticate()
                if not auth_ok or not self.account.is_authenticated:
                    logger.error("❌ OneDrive authentication failed (check client id/secret and app permissions)")
                    return False

            # Get storage for specific user (app-only) or default (delegated auth)
            if self.target_user_upn:
                # Access specific user's OneDrive using app-only authentication
                self.storage = self.account.storage(resource=self.target_user_upn)
                logger.info(f"✅ Connected to OneDrive (app-only mode for user: {self.target_user_upn})")
            else:
                # Access default drive (delegated auth - requires user context)
                self.storage = self.account.storage()
                logger.info("✅ Connected to OneDrive (default drive)")

            # Obtain default drive; fail fast if unavailable
            self.drive = self.storage.get_default_drive()
            if not self.drive:
                logger.error("❌ Failed to obtain OneDrive drive object (authentication/storage not ready)")
                return False

            logger.info("✅ Successfully obtained drive object")
            return True

        except Exception as e:
            logger.error(f"❌ Failed to connect to OneDrive: {str(e)}")
            return False

    def get_folder(self, folder_path: str) -> Optional[O365Folder]:
        """Get folder by path

        Args:
            folder_path: Folder path (e.g., "/Shared Documents/AWB")

        Returns:
            Folder object or None if not found
        """
        if not self.drive:
            logger.error("❌ Drive not connected. Call connect() first.")
            return None

        try:
            # Remove leading/trailing slashes
            folder_path = normalise_onedrive_path(folder_path or "")
            if folder_path:
                graph_path = f"/{folder_path}"
            else:
                graph_path = "/"

            logger.debug("🔎 Resolving OneDrive folder path %s", graph_path)

            try:
                folder = self.drive.get_item_by_path(graph_path)
            except Exception as exc:
                logger.error(
                    "❌ Graph API error while resolving %s: %s",
                    graph_path,
                    exc,
                )
                raise

            if folder and folder.is_folder:
                logger.info(f"✅ Found folder: {folder_path}")
                return folder
            else:
                logger.warning(f"⚠️ Path not found or not a folder: {folder_path}")
                return None

        except Exception as e:
            logger.error(f"❌ Error getting folder {folder_path}: {str(e)}")
            return None

    def download_file_content(self, file_path: str) -> Optional[bytes]:
        """Download file content by absolute OneDrive path.

        Args:
            file_path: Path to the file inside the drive (e.g., "HYA-OCR/Master Data/TELECOM_USERS.csv")

        Returns:
            File content as bytes, or None if not found/error.
        """

        if not self.drive:
            logger.error("❌ Drive not connected. Call connect() first.")
            return None

        try:
            normalized_path = normalise_onedrive_path(file_path)
            graph_path = f"/{normalized_path}" if normalized_path else "/"
            file_item = self.drive.get_item_by_path(graph_path)
            if not file_item:
                logger.warning("⚠️ File not found at path %s", graph_path)
                return None
            if not hasattr(file_item, "is_file"):
                logger.error(
                    "❌ Unexpected OneDrive response type %s for path %s",
                    type(file_item),
                    graph_path,
                )
                return None
            if not file_item.is_file:
                logger.warning("⚠️ Path is not a file: %s", graph_path)
                return None
            # O365 File.get_content may not be available depending on library version.
            # Prefer the download() API to a temporary directory and read bytes back.
            try:
                # Try modern API first (if available in environment)
                content = file_item.get_content()  # type: ignore[attr-defined]
                logger.info(f"✅ Downloaded OneDrive file via get_content(): {file_path}")
                return content
            except Exception:
                try:
                    import os
                    import tempfile
                    # Download to a temporary directory (O365 saves as <dir>/<filename>)
                    with tempfile.TemporaryDirectory() as tmpdir:
                        ok = file_item.download(to_path=tmpdir)
                        if not ok:
                            logger.warning(f"⚠️ Download API returned False for: {file_path}")
                            return None
                        out_path = os.path.join(tmpdir, getattr(file_item, 'name', 'downloaded_file'))
                        try:
                            with open(out_path, 'rb') as f:
                                data = f.read()
                            logger.info(f"✅ Downloaded OneDrive file via download(): {file_path}")
                            return data
                        except FileNotFoundError:
                            # Some versions may store under provided path without filename
                            # Attempt to locate the only file in directory
                            entries = [p for p in os.listdir(tmpdir) if os.path.isfile(os.path.join(tmpdir, p))]
                            if entries:
                                with open(os.path.join(tmpdir, entries[0]), 'rb') as f:
                                    data = f.read()
                                logger.info(f"✅ Downloaded OneDrive file via download() (fallback locate): {file_path}")
                                return data
                            logger.error(f"❌ Downloaded file not found in temp dir for: {file_path}")
                            return None
                except Exception as exc2:
                    logger.error(f"❌ Error downloading OneDrive file {file_path} via download(): {exc2}")
                    return None
        except Exception as exc:  # pragma: no cover - defensive
            logger.error(f"❌ Error downloading OneDrive file {file_path}: {exc}")
            return None

    def list_new_files(
        self,
        folder: O365Folder,
        since_date: datetime,
        file_extensions: Optional[List[str]] = None
    ) -> List[O365File]:
        """List new files in folder modified after since_date

        Args:
            folder: Folder object
            since_date: Only return files modified after this date
            file_extensions: Filter by extensions (e.g., ['.pdf', '.xlsx'])

        Returns:
            List of new files
        """
        if not folder:
            return []

        try:
            if file_extensions is None:
                file_extensions = ['.pdf']

            # Ensure since_date is timezone-aware (UTC)
            if since_date.tzinfo is None:
                # Make it aware in UTC
                since_date = since_date.replace(tzinfo=timezone.utc)

            new_files = []

            # Get items in folder
            for item in folder.get_items():
                # Check if file and modified after since_date
                if item.is_file:
                    # Check modification time
                    modified = item.modified
                    if modified and modified >= since_date:
                        # Check extension
                        if any(item.name.lower().endswith(ext) for ext in file_extensions):
                            new_files.append(item)
                            logger.info(f"✅ Found new file: {item.name} (modified: {modified})")

            logger.info(f"✅ Found {len(new_files)} new files")
            return new_files

        except Exception as e:
            logger.error(f"❌ Error listing files: {str(e)}")
            return []

    def list_all_documents(
        self,
        folder: O365Folder,
        file_extensions: Optional[List[str]] = None,
        created_month_filter: Optional[str] = None,
    ) -> List[O365File]:
        """List files with specified extensions, optionally filtered by month."""
        if not folder:
            return []

        try:
            file_extensions = [ext.lower() for ext in (file_extensions or ['.pdf'])]
            month_start = None
            month_end = None
            if created_month_filter:
                try:
                    year, month = created_month_filter.split('-')
                    year_int = int(year)
                    month_int = int(month)
                    month_start = datetime(year_int, month_int, 1, tzinfo=timezone.utc)
                    if month_int == 12:
                        month_end = datetime(year_int + 1, 1, 1, tzinfo=timezone.utc)
                    else:
                        month_end = datetime(year_int, month_int + 1, 1, tzinfo=timezone.utc)
                    logger.info(
                        "🔍 Filtering OneDrive files by month: %s (%s to %s)",
                        created_month_filter,
                        month_start,
                        month_end,
                    )
                except (ValueError, IndexError):
                    logger.warning(
                        "⚠️ Invalid month format: %s. Expected YYYY-MM. No month filter applied.",
                        created_month_filter,
                    )
                    month_start = None
                    month_end = None

            matched_files: List[O365File] = []
            for item in folder.get_items():
                if not item.is_file:
                    continue
                name_lower = (item.name or "").lower()
                if not any(name_lower.endswith(ext) for ext in file_extensions):
                    continue

                if month_start and month_end:
                    item_date = item.modified or item.created
                    if not item_date:
                        logger.debug("⊘ Skipping %s - no timestamp", item.name)
                        continue
                    if item_date.tzinfo is None:
                        item_date = item_date.replace(tzinfo=timezone.utc)
                    if not (month_start <= item_date < month_end):
                        logger.debug(
                            "⊘ Skipping %s - date %s not in range %s to %s",
                            item.name,
                            item_date,
                            month_start,
                            month_end,
                        )
                        continue

                matched_files.append(item)
                logger.info("✅ Found OneDrive file: %s", item.name)

            logger.info(
                "✅ Found %s file(s) for extensions %s",
                len(matched_files),
                ",".join(file_extensions),
            )
            return matched_files

        except Exception as e:
            logger.error("❌ Error listing OneDrive files: %s", e)
            return []

    def list_all_pdfs(
        self,
        folder: O365Folder,
        created_month_filter: Optional[str] = None
    ) -> List[O365File]:
        return self.list_all_documents(
            folder,
            file_extensions=['.pdf'],
            created_month_filter=created_month_filter,
        )

    def download_file(self, file_item: O365File, local_path: str) -> bool:
        """Download file to local path

        Args:
            file_item: File object
            local_path: Local directory to download to

        Returns:
            True if successful, False otherwise
        """
        if not file_item:
            return False

        try:
            file_item.download(to_path=local_path)
            logger.info(f"✅ Downloaded file: {file_item.name} to {local_path}")
            return True

        except Exception as e:
            logger.error(f"❌ Error downloading file {file_item.name}: {str(e)}")
            return False

    def move_file(
        self,
        file_item: O365File,
        target_folder: O365Folder,
        new_name: Optional[str] = None
    ) -> bool:
        """Move file to target folder

        Args:
            file_item: File object
            target_folder: Target folder object
            new_name: Optional new filename

        Returns:
            True if successful, False otherwise
        """
        if not file_item or not target_folder:
            return False

        try:
            try:
                # Some O365 versions support move(target_folder, new_name), others only move(target_folder).
                if new_name:
                    file_item.move(target_folder, new_name)
                else:
                    file_item.move(target_folder)
            except TypeError:
                # Fallback: ignore new_name if the underlying API doesn't accept it.
                logger.warning(
                    "⚠️ OneDrive move() does not support rename in this environment; "
                    "moving %s to %s without renaming",
                    getattr(file_item, "name", "<unknown>"),
                    getattr(target_folder, "name", "<unknown>"),
                )
                file_item.move(target_folder)

            logger.info(
                "✅ Moved file: %s to %s",
                getattr(file_item, "name", "<unknown>"),
                getattr(target_folder, "name", "<unknown>"),
            )
            return True

        except Exception as e:
            logger.error(f"❌ Error moving file {getattr(file_item, 'name', '<unknown>')}: {str(e)}")
            return False

    def get_or_create_folder(
        self,
        parent_folder: O365Folder,
        folder_name: str
    ) -> Optional[O365Folder]:
        """Get or create folder in parent

        Args:
            parent_folder: Parent folder
            folder_name: Name of folder to get/create

        Returns:
            Folder object or None on error
        """
        if not parent_folder:
            return None

        try:
            # Try to get existing folder
            for item in parent_folder.get_items():
                if item.is_folder and item.name == folder_name:
                    logger.info(f"✅ Found existing folder: {folder_name}")
                    return item

            # Create new folder if not found
            new_folder = parent_folder.create_child_folder(folder_name)
            logger.info(f"✅ Created new folder: {folder_name}")
            return new_folder

        except Exception as e:
            logger.error(f"❌ Error getting/creating folder {folder_name}: {str(e)}")
            return None

    def ensure_folder_path(self, folder_path: str) -> Optional[O365Folder]:
        """Ensure a nested folder path exists (creating any missing components).

        Args:
            folder_path: Absolute OneDrive path like "HYA-OCR/Shop Invoice".

        Returns:
            Folder object for the deepest component, or None on failure.
        """
        if not self.drive:
            logger.error("❌ Drive not connected. Call connect() first.")
            return None

        normalized = normalise_onedrive_path(folder_path or "")
        if not normalized:
            return self.get_folder("")

        existing = self.get_folder(normalized)
        if existing:
            return existing

        if "/" in normalized:
            parent_path, child_name = normalized.rsplit("/", 1)
        else:
            parent_path, child_name = "", normalized

        parent_folder = self.ensure_folder_path(parent_path) if parent_path else self.get_folder("")
        if not parent_folder or not child_name:
            logger.error(
                "❌ Cannot ensure folder %s because parent %s was unavailable",
                normalized,
                parent_path or "/",
            )
            return None

        return self.get_or_create_folder(parent_folder, child_name)

    def close(self) -> None:
        """Close connection"""
        if self.account:
            try:
                # O365 doesn't require explicit close, but good practice
                logger.info("✅ OneDrive client closed")
            except Exception as e:
                logger.warning(f"⚠️ Error closing client: {str(e)}")


def build_client_from_env() -> Optional[OneDriveClient]:
    """Build a OneDriveClient from standard environment variables.

    This helper centralises how we construct the client so that scripts and
    background jobs can share the same logic.
    """
    client_id = os.getenv("ONEDRIVE_APPLICATION_ID")
    legacy_client_id = os.getenv("ONEDRIVE_CLIENT_ID")
    if client_id and legacy_client_id and client_id != legacy_client_id:
        logger.warning(
            "⚠️ Both ONEDRIVE_APPLICATION_ID and ONEDRIVE_CLIENT_ID are set; using ONEDRIVE_APPLICATION_ID"
        )
    if not client_id and legacy_client_id:
        client_id = legacy_client_id
        logger.info(
            "ℹ️ ONEDRIVE_CLIENT_ID is deprecated; please rename it to ONEDRIVE_APPLICATION_ID"
        )
    client_secret = os.getenv("ONEDRIVE_CLIENT_SECRET")
    tenant_id = os.getenv("ONEDRIVE_TENANT_ID")
    target_user_upn = os.getenv("ONEDRIVE_TARGET_USER_UPN")

    if not client_id or not client_secret or not tenant_id:
        logger.error(
            "❌ Missing OneDrive credentials in environment "
            "(ONEDRIVE_APPLICATION_ID / ONEDRIVE_CLIENT_SECRET / ONEDRIVE_TENANT_ID)"
        )
        return None

    return OneDriveClient(
        client_id=client_id,
        client_secret=client_secret,
        tenant_id=tenant_id,
        target_user_upn=target_user_upn,
    )


def verify_onedrive_connection() -> bool:
    """Attempt to connect to OneDrive using environment credentials."""
    client = build_client_from_env()
    if not client:
        logger.error("❌ OneDrive verification skipped - credentials missing")
        return False

    try:
        if not client.connect():
            logger.error("❌ OneDrive verification failed - unable to connect")
            return False
        logger.info("✅ OneDrive startup verification succeeded")
        return True
    finally:
        try:
            client.close()
        except Exception:
            pass


def normalise_onedrive_path(path: str) -> str:
    """Normalise an internal OneDrive path (no leading/trailing slashes)."""
    return (path or "").strip().strip("/")


def join_onedrive_path(*parts: str) -> str:
    """Join path components into a canonical OneDrive path."""
    cleaned = [normalise_onedrive_path(p) for p in parts if p is not None]
    cleaned = [c for c in cleaned if c]
    return "/".join(cleaned)


def path_basename(path: str) -> str:
    """Return last component of a OneDrive path."""
    return normalise_onedrive_path(path).split("/")[-1] if path else ""
