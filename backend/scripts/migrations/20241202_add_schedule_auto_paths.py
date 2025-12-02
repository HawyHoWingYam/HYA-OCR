#!/usr/bin/env python3
"""Add auto-managed OneDrive folder columns onto ocr_schedules."""

import logging

from sqlalchemy import create_engine, inspect, text

from db.database import get_database_url


logger = logging.getLogger(__name__)

COLUMN_DEFINITIONS = [
    ("schedule_root_path", "VARCHAR(500)"),
    ("auto_month_folders", "BOOLEAN NOT NULL DEFAULT FALSE"),
    ("month_folder_pattern", "VARCHAR(64) DEFAULT '{YYYYMM}'"),
    ("material_subfolder_name", "VARCHAR(255) DEFAULT 'Material'"),
    ("history_subfolder_name", "VARCHAR(255) DEFAULT 'history'"),
    ("output_filename_pattern", "VARCHAR(255) DEFAULT '{YYYYMM}.xlsx'"),
]


def ensure_columns(engine) -> None:
    inspector = inspect(engine)
    if not inspector.has_table("ocr_schedules"):
        logger.error("Table ocr_schedules does not exist; run init_db first")
        return

    existing_columns = {col["name"] for col in inspector.get_columns("ocr_schedules")}
    statements = []
    for name, ddl in COLUMN_DEFINITIONS:
        if name not in existing_columns:
            statements.append(f"ALTER TABLE ocr_schedules ADD COLUMN {name} {ddl}")

    if not statements:
        logger.info("✅ All auto-folder columns already exist")
        return

    with engine.begin() as conn:
        for stmt in statements:
            logger.info("🔧 Executing: %s", stmt)
            conn.execute(text(stmt))

    logger.info("✅ Added %s column(s) to ocr_schedules", len(statements))


def main() -> None:
    logging.basicConfig(level=logging.INFO, format="%(asctime)s - %(levelname)s - %(message)s")
    database_url = get_database_url()
    engine = create_engine(database_url)
    try:
        ensure_columns(engine)
    finally:
        engine.dispose()


if __name__ == "__main__":
    main()
