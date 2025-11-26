from sqlalchemy import create_engine, inspect, text
from sqlalchemy.ext.declarative import declarative_base
from sqlalchemy.orm import sessionmaker
import os
import json
import urllib.parse
import logging

logger = logging.getLogger(__name__)
Base = declarative_base()


def get_database_url() -> str:
    """
    獲取數據庫 URL
    優先級：環境變量 > AWS Secrets Manager > 配置文件
    """
    try:
        # 首先嘗試從配置加載器獲取
        from config_loader import config_loader

        return config_loader.get_database_url()
    except ImportError:
        logger.warning("Config loader not available, using fallback method")
        return _fallback_get_database_url()
    except Exception as e:
        logger.error(f"Failed to get database URL from config loader: {e}")
        return _fallback_get_database_url()


def _fallback_get_database_url() -> str:
    """備用方法：直接從環境變量或配置文件獲取數據庫 URL"""
    # 優先從環境變量獲取
    database_url = os.getenv("DATABASE_URL")
    if database_url:
        logger.info("Using database URL from environment variable")
        return _encode_database_url(database_url)

    # 回退到配置文件
    config_paths = [
        "env/config.json",
        os.path.join(os.path.dirname(__file__), "..", "env", "config.json"),
    ]

    for config_path in config_paths:
        try:
            if os.path.exists(config_path):
                with open(config_path, "r", encoding="utf-8") as f:
                    config = json.load(f)
                    database_url = config.get("database_url")
                    if database_url and not database_url.startswith(
                        "REPLACE_WITH_ENV_VAR"
                    ):
                        logger.warning("Using database URL from config file (fallback)")
                        return _encode_database_url(database_url)
        except (json.JSONDecodeError, IOError) as e:
            logger.warning(f"Could not read config file {config_path}: {e}")
            continue

    raise ValueError("Database URL not found in environment variables or config file")


def _encode_database_url(database_url: str) -> str:
    """編碼數據庫 URL 中的特殊字符"""
    try:
        # 解析 URL 以處理特殊字符
        parts = database_url.split("@")
        if len(parts) == 2:
            auth_part = parts[0]
            host_part = parts[1]

            # 提取用戶名和密碼
            auth_parts = auth_part.split("://", 1)[1].split(":", 1)
            if len(auth_parts) == 2:
                username = auth_parts[0]
                password = auth_parts[1]

                # URL 編碼密碼以處理特殊字符
                encoded_password = urllib.parse.quote_plus(password)

                # 重構 URL
                protocol = auth_part.split("://", 1)[0]
                return f"{protocol}://{username}:{encoded_password}@{host_part}"

        return database_url
    except Exception as e:
        logger.warning(f"Could not encode database URL: {e}")
        return database_url


def _ensure_mapping_schema(engine):
    """Ensure mapping-related tables and columns match ORM expectations."""
    try:
        inspector = inspect(engine)

        # Ensure mapping tables exist without importing ORM (avoids duplicate declarative definitions)
        dialect = engine.dialect.name

        create_mapping_templates_sql = {
            "postgresql": """
                CREATE TABLE IF NOT EXISTS mapping_templates (
                    template_id SERIAL PRIMARY KEY,
                    template_name VARCHAR(255) NOT NULL,
                    company_id INTEGER NULL,
                    doc_type_id INTEGER NULL,
                    item_type VARCHAR(32) NOT NULL DEFAULT 'single_source',
                    config JSONB NOT NULL,
                    priority INTEGER NOT NULL DEFAULT 100,
                    created_at TIMESTAMP WITHOUT TIME ZONE DEFAULT NOW(),
                    updated_at TIMESTAMP WITHOUT TIME ZONE DEFAULT NOW()
                )
            """,
            "sqlite": """
                CREATE TABLE IF NOT EXISTS mapping_templates (
                    template_id INTEGER PRIMARY KEY AUTOINCREMENT,
                    template_name TEXT NOT NULL,
                    company_id INTEGER NULL,
                    doc_type_id INTEGER NULL,
                    item_type TEXT NOT NULL DEFAULT 'single_source',
                    config TEXT NOT NULL,
                    priority INTEGER NOT NULL DEFAULT 100,
                    created_at TEXT DEFAULT CURRENT_TIMESTAMP,
                    updated_at TEXT DEFAULT CURRENT_TIMESTAMP
                )
            """,
        }

        create_mapping_defaults_sql = {
            "postgresql": """
                CREATE TABLE IF NOT EXISTS company_doc_mapping_defaults (
                    default_id SERIAL PRIMARY KEY,
                    company_id INTEGER NOT NULL,
                    doc_type_id INTEGER NOT NULL,
                    item_type VARCHAR(32) NOT NULL DEFAULT 'single_source',
                    template_id INTEGER NULL REFERENCES mapping_templates(template_id) ON DELETE SET NULL,
                    config_override JSONB NULL,
                    created_at TIMESTAMP WITHOUT TIME ZONE DEFAULT NOW(),
                    updated_at TIMESTAMP WITHOUT TIME ZONE DEFAULT NOW(),
                    UNIQUE (company_id, doc_type_id, item_type)
                )
            """,
            "sqlite": """
                CREATE TABLE IF NOT EXISTS company_doc_mapping_defaults (
                    default_id INTEGER PRIMARY KEY AUTOINCREMENT,
                    company_id INTEGER NOT NULL,
                    doc_type_id INTEGER NOT NULL,
                    item_type TEXT NOT NULL DEFAULT 'single_source',
                    template_id INTEGER NULL,
                    config_override TEXT NULL,
                    created_at TEXT DEFAULT CURRENT_TIMESTAMP,
                    updated_at TEXT DEFAULT CURRENT_TIMESTAMP,
                    UNIQUE (company_id, doc_type_id, item_type)
                )
            """,
        }

        if not inspector.has_table("mapping_templates"):
            ddl = create_mapping_templates_sql.get(dialect)
            if ddl:
                with engine.begin() as connection:
                    connection.execute(text(ddl))
                logger.info("Created mapping_templates table")
            else:
                logger.warning("Unsupported dialect '%s' for creating mapping_templates", dialect)

        if not inspector.has_table("company_doc_mapping_defaults"):
            ddl = create_mapping_defaults_sql.get(dialect)
            if ddl:
                with engine.begin() as connection:
                    connection.execute(text(ddl))
                logger.info("Created company_doc_mapping_defaults table")
            else:
                logger.warning("Unsupported dialect '%s' for creating mapping defaults", dialect)

        # Ensure new columns on ocr_order_items
        if inspector.has_table("ocr_order_items"):
            existing_columns = {col["name"] for col in inspector.get_columns("ocr_order_items")}
            statements = []

            dialect = engine.dialect.name

            if "item_type" not in existing_columns:
                if dialect == "postgresql":
                    statements.append("ALTER TABLE ocr_order_items ADD COLUMN item_type VARCHAR(32) DEFAULT 'single_source'")
                    statements.append("ALTER TABLE ocr_order_items ALTER COLUMN item_type SET NOT NULL")
                else:
                    statements.append("ALTER TABLE ocr_order_items ADD COLUMN item_type VARCHAR(32)")
                    statements.append("UPDATE ocr_order_items SET item_type = 'single_source' WHERE item_type IS NULL")

            if "applied_template_id" not in existing_columns:
                statements.append("ALTER TABLE ocr_order_items ADD COLUMN applied_template_id INTEGER NULL")
                if dialect == "postgresql":
                    statements.append(
                        "ALTER TABLE ocr_order_items ADD CONSTRAINT fk_order_items_template FOREIGN KEY (applied_template_id) REFERENCES mapping_templates(template_id) ON DELETE SET NULL"
                    )

            if "mapping_config" not in existing_columns:
                if dialect == "postgresql":
                    statements.append("ALTER TABLE ocr_order_items ADD COLUMN mapping_config JSONB NULL")
                else:
                    statements.append("ALTER TABLE ocr_order_items ADD COLUMN mapping_config TEXT NULL")

            if statements:
                with engine.begin() as connection:
                    for stmt in statements:
                        connection.execute(text(stmt))
                logger.info("Ensured ocr_order_items mapping columns exist.")

        if inspector.has_table("company_doc_mapping_defaults"):
            default_columns = {col["name"] for col in inspector.get_columns("company_doc_mapping_defaults")}
            statements = []

            if "item_type" not in default_columns:
                if dialect == "postgresql":
                    statements.append("ALTER TABLE company_doc_mapping_defaults ADD COLUMN item_type VARCHAR(32) DEFAULT 'single_source'")
                    statements.append("ALTER TABLE company_doc_mapping_defaults ALTER COLUMN item_type SET NOT NULL")
                else:
                    statements.append("ALTER TABLE company_doc_mapping_defaults ADD COLUMN item_type VARCHAR(32)")
                    statements.append("UPDATE company_doc_mapping_defaults SET item_type = 'single_source' WHERE item_type IS NULL")

            if statements:
                with engine.begin() as connection:
                    for stmt in statements:
                        connection.execute(text(stmt))
                logger.info("Ensured company_doc_mapping_defaults columns exist.")

        # Ensure api_usage table supports both legacy jobs and new order items
        if inspector.has_table("api_usage"):
            api_columns = {col["name"]: col for col in inspector.get_columns("api_usage")}
            api_statements = []

            # Add item_id column if missing (link to ocr_order_items)
            if "item_id" not in api_columns:
                if dialect == "postgresql":
                    api_statements.append(
                        "ALTER TABLE api_usage "
                        "ADD COLUMN item_id INTEGER NULL "
                        "REFERENCES ocr_order_items(item_id) ON DELETE SET NULL"
                    )
                else:
                    # SQLite and other dialects – add nullable column without FK
                    api_statements.append(
                        "ALTER TABLE api_usage ADD COLUMN item_id INTEGER NULL"
                    )

            # Make job_id nullable for dual-system support
            job_col = api_columns.get("job_id")
            if job_col is not None and not job_col.get("nullable", True):
                if dialect == "postgresql":
                    api_statements.append(
                        "ALTER TABLE api_usage ALTER COLUMN job_id DROP NOT NULL"
                    )
                else:
                    logger.warning(
                        "api_usage.job_id is NOT NULL but automatic migration to nullable "
                        "is only implemented for PostgreSQL. "
                        "Please adjust schema manually if needed for dialect '%s'.",
                        dialect,
                    )

            if api_statements:
                with engine.begin() as connection:
                    for stmt in api_statements:
                        connection.execute(text(stmt))
                logger.info("Ensured api_usage schema supports job_id/item_id linkage.")

            # Add check constraint to enforce at least one linkage (job or item)
            if dialect == "postgresql":
                try:
                    with engine.begin() as connection:
                        connection.execute(
                            text(
                                "ALTER TABLE api_usage "
                                "ADD CONSTRAINT ck_api_usage_job_or_item "
                                "CHECK (job_id IS NOT NULL OR item_id IS NOT NULL)"
                            )
                        )
                    logger.info("Ensured ck_api_usage_job_or_item check constraint exists.")
                except Exception as e:
                    # Constraint may already exist – ignore duplicate errors
                    if "already exists" in str(e) or "duplicate" in str(e):
                        logger.info(
                            "Check constraint ck_api_usage_job_or_item already exists on api_usage."
                        )
                    else:
                        logger.warning(
                            "Failed to add ck_api_usage_job_or_item constraint on api_usage: %s",
                            e,
                        )

    except Exception as err:
        logger.error(f"Failed to ensure mapping schema: {err}")


def create_database_engine():
    """創建數據庫引擎"""
    try:
        database_url = get_database_url()

        # 根據數據庫類型設置連接參數
        if database_url.startswith("sqlite"):
            # SQLite 不支持 PostgreSQL 特定參數
            connect_args = {}
            pool_size = 5  # SQLite 連接池較小
            max_overflow = 10
            logger.info("Using SQLite database configuration")
        else:
            # PostgreSQL 連接參數
            connect_args = {
                "connect_timeout": 30,
                "application_name": "GeminiOCR_Backend",
            }
            pool_size = 10
            max_overflow = 20
            logger.info("Using PostgreSQL database configuration")

        # 創建引擎，添加連接池和重試機制
        engine = create_engine(
            database_url,
            pool_size=pool_size,
            max_overflow=max_overflow,
            pool_recycle=3600,
            pool_pre_ping=True,
            connect_args=connect_args,
        )

        _ensure_mapping_schema(engine)

        logger.info("✅ Database connection established successfully")
        return engine

    except Exception as e:
        logger.error(f"❌ Failed to create database engine: {e}")
        raise


# 創建數據庫引擎
try:
    engine = create_database_engine()
except Exception as e:
    logger.critical(f"Cannot initialize database: {e}")
    # 在開發環境中，可能需要延遲初始化
    engine = None

# 創建會話工廠
if engine:
    SessionLocal = sessionmaker(autocommit=False, autoflush=False, bind=engine)
else:
    SessionLocal = None

# 依賴注入函數：獲取 DB 會話
def get_db():
    """獲取數據庫會話"""
    global engine, SessionLocal

    if not SessionLocal:
        # 嘗試延遲初始化
        engine = create_database_engine()
        SessionLocal = sessionmaker(autocommit=False, autoflush=False, bind=engine)

    db = SessionLocal()
    try:
        yield db
    except Exception as e:
        logger.error(f"Database session error: {e}")
        db.rollback()
        raise
    finally:
        db.close()


def get_database_info() -> dict:
    """獲取數據庫信息"""
    try:
        database_url = get_database_url()
        # 隱藏敏感信息
        safe_url = database_url.split("@")[1] if "@" in database_url else "unknown"
        return {
            "status": "connected" if engine else "disconnected",
            "host": safe_url,
            "pool_size": engine.pool.size() if engine else 0,
            "checked_in": engine.pool.checkedin() if engine else 0,
            "checked_out": engine.pool.checkedout() if engine else 0,
        }
    except Exception as e:
        return {"status": "error", "error": str(e)}
