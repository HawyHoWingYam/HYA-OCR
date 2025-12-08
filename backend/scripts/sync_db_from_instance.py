#!/usr/bin/env python3
"""
Sync local PostgreSQL database from remote instance (e.g. AWS Aurora/RDS).

This script will overwrite the LOCAL database so that its schema and/or data
match the remote instance as closely as possible.

By default it:
  - Uses the remote instance as *source* (read-only).
  - Uses the local PostgreSQL as *target* (destructive overwrite).
  - Runs `pg_dump` against the instance and `pg_restore` into local.
  - Syncs BOTH schema and data for the whole database.

You can restrict scope using:
  - `--schema-only`   : only schema (no data).
  - `--data-only`     : only data (no schema).
  - `--table/-t NAME` : limit to one or more tables.

Connection configuration
------------------------
Instance (source) DB – choose ONE of:
  1) Single URL:
       - Env: INSTANCE_DATABASE_URL=postgresql://user:pass@host:5432/dbname
       - CLI: --instance-url postgresql://...
     (If your password has special characters, either URL-encode it or use #2.)
  2) Discrete fields (recommended for complex passwords):
       - Env:
           INSTANCE_DB_HOST=...
           INSTANCE_DB_PORT=5432
           INSTANCE_DB_NAME=document_processing_platform
           INSTANCE_DB_USER=dbmasteruser
           INSTANCE_DB_PASSWORD=...
       - or CLI flags: --instance-host/--instance-port/--instance-db/--instance-user/--instance-password

Local (target) DB – choose ONE of:
  - Env: LOCAL_DATABASE_URL=postgresql://...
  - Env: DATABASE_URL=postgresql://...   (loaded from backend/backend.env if present)
  - CLI: --local-url postgresql://...

Examples
--------
  # Dry-run (no changes), using env vars for both DBs
  python backend/scripts/sync_db_from_instance.py --dry-run

  # Actual full sync (schema + data) with interactive confirmation
  python backend/scripts/sync_db_from_instance.py

  # Schema-only sync (no data changes)
  python backend/scripts/sync_db_from_instance.py --schema-only

  # Data-only sync for specific config/reference tables
  python backend/scripts/sync_db_from_instance.py --data-only \\
      -t company_document_configs -t company_doc_mapping_defaults

  # Non-interactive (unsafe; for automation)
  python backend/scripts/sync_db_from_instance.py --yes
"""

from __future__ import annotations

import argparse
import logging
import os
import shutil
import subprocess
import sys
import tempfile
from pathlib import Path
from typing import Dict, Optional
from urllib.parse import urlparse

from dotenv import load_dotenv


logger = logging.getLogger(__name__)


def load_backend_env() -> None:
    """
    Load backend/backend.env so DATABASE_URL and related vars are available
    even when running this script directly.
    """
    backend_dir = Path(__file__).resolve().parent.parent
    env_path = backend_dir / "backend.env"
    if env_path.exists():
        load_dotenv(env_path, override=False)
        logger.info("Loaded environment file: %s", env_path)
    else:
        logger.debug("No backend.env found at %s", env_path)


def ensure_pg_tools_available() -> None:
    """Verify that required PostgreSQL client tools are available on PATH."""
    missing = [cmd for cmd in ("pg_dump", "pg_restore", "psql") if shutil.which(cmd) is None]
    if missing:
        raise SystemExit(
            f"Required PostgreSQL client tools not found on PATH: {', '.join(missing)}"
        )


def parse_db_url(url: str) -> Dict[str, Optional[str]]:
    """Parse a PostgreSQL URL into components without exposing passwords in logs."""
    parsed = urlparse(url)
    if parsed.scheme not in ("postgres", "postgresql"):
        raise ValueError(f"Unsupported DB URL scheme in {url!r} (expected postgres/postgresql)")

    dbname = parsed.path.lstrip("/") or None
    if not dbname:
        raise ValueError(f"Could not determine database name from URL: {url!r}")

    return {
        "host": parsed.hostname or "localhost",
        "port": parsed.port or 5432,
        "dbname": dbname,
        "user": parsed.username or "",
        "password": parsed.password or "",
    }


def mask_conn_info(conn: Dict[str, Optional[str]]) -> str:
    """Return a safe display string for a DB connection (no password)."""
    user = conn.get("user") or ""
    host = conn.get("host") or ""
    port = conn.get("port")
    dbname = conn.get("dbname") or ""

    identity = ""
    if user:
        identity += f"{user}@"
    identity += host or "localhost"
    if port:
        identity += f":{port}"
    return f"postgresql://{identity}/{dbname}"


def _reset_local_database(local_conn: Dict[str, Optional[str]]) -> None:
    """
    Drop and recreate the local database so it's completely clean before restore.

    This connects to an admin DB (postgres/template1) and issues DROP/CREATE DATABASE.
    """
    host = local_conn["host"]
    port = local_conn["port"]
    user = local_conn["user"]
    dbname = local_conn["dbname"]

    safe_db = str(dbname).replace('"', '""')
    env = os.environ.copy()
    if local_conn.get("password"):
        env["PGPASSWORD"] = str(local_conn["password"])

    last_error: Optional[subprocess.CalledProcessError] = None
    for admin_db in ("postgres", "template1"):
        # Try FORCE drop first (Postgres 13+), then plain drop.
        for sql in (
            f'DROP DATABASE IF EXISTS "{safe_db}" WITH (FORCE);',
            f'DROP DATABASE IF EXISTS "{safe_db}";',
        ):
            try:
                drop_cmd = [
                    "psql",
                    "-h",
                    str(host),
                    "-p",
                    str(port),
                    "-U",
                    str(user),
                    "-d",
                    admin_db,
                    "-v",
                    "ON_ERROR_STOP=1",
                    "-c",
                    sql,
                ]
                subprocess.run(drop_cmd, env=env, check=True, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
                last_error = None
                break
            except subprocess.CalledProcessError as exc:
                last_error = exc
        if last_error is None:
            # Create database
            create_cmd = [
                "psql",
                "-h",
                str(host),
                "-p",
                str(port),
                "-U",
                str(user),
                "-d",
                admin_db,
                "-v",
                "ON_ERROR_STOP=1",
                "-c",
                f'CREATE DATABASE "{safe_db}";',
            ]
            subprocess.run(create_cmd, env=env, check=True, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
            logger.info("Recreated local database %s on %s:%s", dbname, host, port)
            return

    raise SystemExit(
        "Failed to drop/recreate local database. Please ensure no active connections "
        f"to '{dbname}' and that user '{user}' has sufficient privileges."
    )


def get_instance_connection(args: argparse.Namespace) -> Dict[str, Optional[str]]:
    """Resolve instance (source) DB connection info from CLI/env."""
    # Highest priority: explicit URL from CLI/env
    if args.instance_url:
        return parse_db_url(args.instance_url)

    env_url = os.getenv("INSTANCE_DATABASE_URL")
    if env_url:
        return parse_db_url(env_url)

    # Fallback: discrete fields (recommended for complex passwords)
    host = args.instance_host or os.getenv("INSTANCE_DB_HOST")
    dbname = args.instance_db or os.getenv("INSTANCE_DB_NAME")
    user = args.instance_user or os.getenv("INSTANCE_DB_USER")
    password = args.instance_password or os.getenv("INSTANCE_DB_PASSWORD")
    port = args.instance_port or os.getenv("INSTANCE_DB_PORT") or "5432"

    if not all([host, dbname, user, password]):
        raise SystemExit(
            "Instance DB connection info not provided.\n"
            "Provide either INSTANCE_DATABASE_URL or all of "
            "INSTANCE_DB_HOST/INSTANCE_DB_PORT/INSTANCE_DB_NAME/INSTANCE_DB_USER/INSTANCE_DB_PASSWORD, "
            "or use the corresponding --instance-* CLI options."
        )

    return {
        "host": host,
        "port": int(port),
        "dbname": dbname,
        "user": user,
        "password": password,
    }


def get_local_connection(args: argparse.Namespace) -> Dict[str, Optional[str]]:
    """Resolve local (target) DB connection info from CLI/env."""
    # CLI override
    if args.local_url:
        return parse_db_url(args.local_url)

    env_url = os.getenv("LOCAL_DATABASE_URL") or os.getenv("DATABASE_URL")
    if env_url:
        return parse_db_url(env_url)

    raise SystemExit(
        "Local DB connection info not provided.\n"
        "Set LOCAL_DATABASE_URL or DATABASE_URL in backend/backend.env (or process env), "
        "or pass --local-url."
    )


def confirm_overwrite(
    instance_conn: Dict[str, Optional[str]],
    local_conn: Dict[str, Optional[str]],
    mode: str,
    tables: Optional[list[str]],
    assume_yes: bool,
    dry_run: bool,
) -> bool:
    """Ask the user to confirm destructive overwrite."""
    if dry_run:
        # Dry-run never mutates, safe without prompt.
        return True
    if assume_yes:
        return True

    print("=" * 80)
    print("⚠️  DANGEROUS OPERATION: Sync local DB from INSTANCE")
    print(f"  Mode   : {mode}")
    print(f"  Source : {mask_conn_info(instance_conn)}")
    print(f"  Target : {mask_conn_info(local_conn)}")
    if tables:
        print(f"  Tables : {', '.join(tables)}")
    if mode.upper() == "DATA ONLY":
        print("  Action : Local table data will be replaced to match the instance")
        print("           for the selected tables (schema remains unchanged).")
    elif mode.upper() == "SCHEMA ONLY":
        print("  Action : Local database schema will be dropped/recreated to match")
        print("           the instance (data rows remain where compatible).")
    else:
        print("  Action : Local database will be DROPPED and recreated,")
        print("           then schema and data restored from the instance dump.")
    print("=" * 80)
    resp = input("Type 'yes' to continue: ").strip().lower()
    if resp != "yes":
        print("Aborted by user.")
        return False
    return True


def sync_databases(
    instance_conn: Dict[str, Optional[str]],
    local_conn: Dict[str, Optional[str]],
    schema_only: bool = False,
    data_only: bool = False,
    tables: Optional[list[str]] = None,
    dry_run: bool = False,
) -> None:
    """Run pg_dump on the instance and pg_restore into local."""
    if schema_only and data_only:
        raise ValueError("Cannot run with both schema_only and data_only enabled.")

    if schema_only:
        mode_desc = "schema-only"
    elif data_only:
        mode_desc = "data-only"
    else:
        mode_desc = "schema + data"
    if tables:
        mode_desc += f" (tables: {', '.join(tables)})"

    logger.info("Sync mode           : %s", mode_desc)
    logger.info("Instance (source)   : %s", mask_conn_info(instance_conn))
    logger.info("Local (destination) : %s", mask_conn_info(local_conn))

    if dry_run:
        logger.info("[DRY-RUN] Skipping pg_dump/pg_restore execution.")
        return

    dump_file: Optional[str] = None
    try:
        tmp = tempfile.NamedTemporaryFile(prefix="instance_db_", suffix=".dump", delete=False)
        dump_file = tmp.name
        tmp.close()

        # Prepare pg_dump command from INSTANCE
        dump_cmd = [
            "pg_dump",
            "-h",
            str(instance_conn["host"]),
            "-p",
            str(instance_conn["port"]),
            "-U",
            str(instance_conn["user"]),
            "-d",
            str(instance_conn["dbname"]),
            "--no-owner",
            "--no-privileges",
            "-F",
            "c",  # custom format for pg_restore
            "-f",
            dump_file,
        ]
        if schema_only:
            dump_cmd.append("--schema-only")
        elif data_only:
            dump_cmd.append("--data-only")
        if tables:
            for t in tables:
                dump_cmd.extend(["-t", t])

        dump_env = os.environ.copy()
        if instance_conn.get("password"):
            dump_env["PGPASSWORD"] = str(instance_conn["password"])

        logger.info("Running pg_dump from instance...")
        subprocess.run(dump_cmd, env=dump_env, check=True)
        logger.info("pg_dump completed, dump file: %s", dump_file)

        # For full schema+data sync, completely reset the local database first
        if not schema_only and not data_only:
            logger.info("Resetting local database before restore (drop & recreate)...")
            _reset_local_database(local_conn)

        # Prepare pg_restore command into LOCAL
        restore_cmd = [
            "pg_restore",
            "-h",
            str(local_conn["host"]),
            "-p",
            str(local_conn["port"]),
            "-U",
            str(local_conn["user"]),
            "-d",
            str(local_conn["dbname"]),
        ]
        # For schema/full sync, drop existing objects first.
        # For data-only, we must NOT use --clean/--if-exists (PostgreSQL forbids it).
        if not data_only:
            restore_cmd.extend(["--clean", "--if-exists"])
        restore_cmd.extend(
            [
                "--no-owner",
                "--no-privileges",
                dump_file,
            ]
        )
        if schema_only:
            restore_cmd.append("--schema-only")
        elif data_only:
            restore_cmd.append("--data-only")

        restore_env = os.environ.copy()
        if local_conn.get("password"):
            restore_env["PGPASSWORD"] = str(local_conn["password"])

        logger.info("Running pg_restore into local...")
        subprocess.run(restore_cmd, env=restore_env, check=True)
        logger.info("Database sync completed successfully.")

    except subprocess.CalledProcessError as exc:
        logger.error("Command failed with exit code %s: %s", exc.returncode, exc.cmd)
        raise SystemExit(exc.returncode)
    finally:
        if dump_file and os.path.exists(dump_file):
            try:
                os.remove(dump_file)
            except OSError:
                logger.warning("Could not remove temporary dump file: %s", dump_file)


def build_arg_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        description="Sync local PostgreSQL database from remote instance (schema + data)."
    )

    # Instance connection options
    parser.add_argument(
        "--instance-url",
        help="PostgreSQL URL for the source instance "
        "(overrides INSTANCE_DATABASE_URL and INSTANCE_DB_* env vars).",
    )
    parser.add_argument(
        "--instance-host",
        help="Instance DB host (fallback if INSTANCE_DATABASE_URL not set).",
    )
    parser.add_argument(
        "--instance-port",
        type=int,
        help="Instance DB port (default 5432).",
    )
    parser.add_argument(
        "--instance-db",
        help="Instance DB name, e.g. document_processing_platform.",
    )
    parser.add_argument(
        "--instance-user",
        help="Instance DB username, e.g. dbmasteruser.",
    )
    parser.add_argument(
        "--instance-password",
        help="Instance DB password (use env INSTANCE_DB_PASSWORD if you prefer).",
    )

    # Local connection options
    parser.add_argument(
        "--local-url",
        help="PostgreSQL URL for the local DB (overrides LOCAL_DATABASE_URL and DATABASE_URL).",
    )
    # Behaviour / safety
    parser.add_argument(
        "--schema-only",
        action="store_true",
        help="Sync schema only (no data). Default is schema + data.",
    )
    parser.add_argument(
        "--data-only",
        action="store_true",
        help="Sync data only (no schema). Default is schema + data.",
    )
    parser.add_argument(
        "-t",
        "--table",
        action="append",
        help=(
            "Limit sync to the given table name. "
            "Can be specified multiple times; if omitted, operates on the whole database."
        ),
    )
    parser.add_argument(
        "--dry-run",
        action="store_true",
        help="Show what would be done without applying any changes.",
    )
    parser.add_argument(
        "-y",
        "--yes",
        action="store_true",
        help="Skip interactive confirmation (DANGEROUS).",
    )

    return parser


def main(argv: Optional[list[str]] = None) -> None:
    logging.basicConfig(
        level=logging.INFO,
        format="%(asctime)s - %(levelname)s - %(message)s",
    )

    load_backend_env()
    ensure_pg_tools_available()

    parser = build_arg_parser()
    args = parser.parse_args(argv)

    if args.schema_only and args.data_only:
        raise SystemExit("Cannot use --schema-only and --data-only together.")

    instance_conn = get_instance_connection(args)
    local_conn = get_local_connection(args)
    tables = args.table or []

    if args.schema_only:
        mode = "SCHEMA ONLY"
    elif args.data_only:
        mode = "DATA ONLY"
    else:
        mode = "SCHEMA + DATA"

    if not confirm_overwrite(
        instance_conn=instance_conn,
        local_conn=local_conn,
        mode=mode,
        tables=tables or None,
        assume_yes=args.yes,
        dry_run=args.dry_run,
    ):
        return

    sync_databases(
        instance_conn=instance_conn,
        local_conn=local_conn,
        schema_only=args.schema_only,
        data_only=args.data_only,
        tables=tables or None,
        dry_run=args.dry_run,
    )


if __name__ == "__main__":
    main()
