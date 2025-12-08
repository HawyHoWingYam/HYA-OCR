#!/usr/bin/env python3
"""
数据库健康检查脚本
用于验证数据库连接和基本功能
"""

import sys
import logging
from sqlalchemy import create_engine, text
from sqlalchemy.exc import SQLAlchemyError
import time

# 添加项目根目录到路径
sys.path.insert(0, "/app")

from db.database import get_database_url
from db.models import Company, DocumentType, ProcessingJob, BatchJob

# 配置日志
logging.basicConfig(
    level=logging.INFO, format="%(asctime)s - %(levelname)s - %(message)s"
)
logger = logging.getLogger(__name__)


def check_database_connection(database_url: str) -> bool:
    """检查数据库连接"""
    try:
        logger.info("检查数据库连接...")
        engine = create_engine(database_url)

        with engine.connect() as conn:
            result = conn.execute(text("SELECT 1 as test"))
            row = result.fetchone()
            if row and row[0] == 1:
                logger.info("✅ 数据库连接正常")
                engine.dispose()
                return True
            else:
                logger.error("❌ 数据库连接测试失败")
                engine.dispose()
                return False

    except Exception as e:
        logger.error(f"❌ 数据库连接失败: {e}")
        return False


def check_tables_exist(database_url: str) -> bool:
    """检查必要的表是否存在"""
    try:
        logger.info("检查数据库表...")
        engine = create_engine(database_url)

        required_tables = [
            "companies",
            "document_types",
            "company_doc_type_configs",
            "processing_jobs",
            "batch_jobs",
            "files",
            "document_files",
            "api_usage",
            "users",
            "departments",
            "ocr_orders",
            "ocr_order_items",
        ]

        with engine.connect() as conn:
            for table in required_tables:
                try:
                    result = conn.execute(text(f"SELECT COUNT(*) FROM {table}"))
                    count = result.scalar()
                    logger.info(f"✅ 表 {table}: {count} 条记录")
                except SQLAlchemyError as e:
                    logger.error(f"❌ 表 {table} 不存在或无法访问: {e}")
                    engine.dispose()
                    return False

        engine.dispose()
        return True

    except Exception as e:
        logger.error(f"❌ 检查数据库表失败: {e}")
        return False


def check_constraints(database_url: str) -> bool:
    """检查数据库约束"""
    try:
        logger.info("检查数据库约束...")
        engine = create_engine(database_url)

        with engine.connect() as conn:
            # 检查外键约束
            constraints_query = """
            SELECT conname, contype 
            FROM pg_constraint 
            WHERE contype IN ('f', 'c')
            ORDER BY conname
            """

            result = conn.execute(text(constraints_query))
            constraints = result.fetchall()

            fk_count = sum(1 for c in constraints if c[1] == "f")
            check_count = sum(1 for c in constraints if c[1] == "c")

            logger.info(f"✅ 外键约束: {fk_count} 个")
            logger.info(f"✅ 检查约束: {check_count} 个")

        engine.dispose()
        return True

    except Exception as e:
        logger.warning(f"⚠️ 检查约束失败 (可能是非PostgreSQL数据库): {e}")
        return True  # 非关键错误


def check_indexes(database_url: str) -> bool:
    """检查数据库索引"""
    try:
        logger.info("检查数据库索引...")
        engine = create_engine(database_url)

        with engine.connect() as conn:
            # 检查索引
            indexes_query = """
            SELECT indexname, tablename 
            FROM pg_indexes 
            WHERE schemaname = 'public'
            ORDER BY tablename, indexname
            """

            result = conn.execute(text(indexes_query))
            indexes = result.fetchall()

            logger.info(f"✅ 数据库索引: {len(indexes)} 个")

            # 检查关键索引
            key_indexes = [
                "idx_processing_jobs_status",
                "idx_processing_jobs_company_id",
                "idx_api_usage_timestamp",
            ]

            existing_indexes = [idx[0] for idx in indexes]
            for key_idx in key_indexes:
                if key_idx in existing_indexes:
                    logger.info(f"✅ 关键索引存在: {key_idx}")
                else:
                    logger.warning(f"⚠️ 关键索引缺失: {key_idx}")

        engine.dispose()
        return True

    except Exception as e:
        logger.warning(f"⚠️ 检查索引失败 (可能是非PostgreSQL数据库): {e}")
        return True  # 非关键错误


def check_data_integrity(database_url: str) -> bool:
    """检查数据完整性"""
    try:
        logger.info("检查数据完整性...")
        engine = create_engine(database_url)

        from sqlalchemy.orm import sessionmaker

        Session = sessionmaker(bind=engine)
        session = Session()

        try:
            # 检查是否有基本的公司和文档类型数据
            company_count = session.query(Company).count()
            doc_type_count = session.query(DocumentType).count()

            if company_count == 0:
                logger.warning("⚠️ 没有公司数据，请插入初始数据")
            else:
                logger.info(f"✅ 公司数据: {company_count} 个")

            if doc_type_count == 0:
                logger.warning("⚠️ 没有文档类型数据，请插入初始数据")
            else:
                logger.info(f"✅ 文档类型数据: {doc_type_count} 个")

            # 检查处理任务统计
            job_count = session.query(ProcessingJob).count()
            batch_count = session.query(BatchJob).count()

            logger.info(f"✅ 处理任务: {job_count} 个")
            logger.info(f"✅ 批量任务: {batch_count} 个")

        finally:
            session.close()

        engine.dispose()
        return True

    except Exception as e:
        logger.error(f"❌ 检查数据完整性失败: {e}")
        return False


def check_database_performance(database_url: str) -> bool:
    """简单的数据库性能检查"""
    try:
        logger.info("检查数据库性能...")
        engine = create_engine(database_url)

        with engine.connect() as conn:
            # 测试简单查询响应时间
            start_time = time.time()
            conn.execute(text("SELECT COUNT(*) FROM companies"))
            query_time = time.time() - start_time

            if query_time < 1.0:
                logger.info(f"✅ 查询响应时间: {query_time:.3f}s (良好)")
            elif query_time < 3.0:
                logger.warning(f"⚠️ 查询响应时间: {query_time:.3f}s (一般)")
            else:
                logger.warning(f"⚠️ 查询响应时间: {query_time:.3f}s (较慢)")

        engine.dispose()
        return True

    except Exception as e:
        logger.error(f"❌ 性能检查失败: {e}")
        return False


def main():
    """主函数"""
    logger.info("🔍 开始数据库健康检查...")

    try:
        # 获取数据库URL
        database_url = get_database_url()
        logger.info(
            f"数据库: {database_url.split('@')[1] if '@' in database_url else 'localhost'}"
        )

        checks = [
            ("数据库连接", check_database_connection),
            ("数据库表", check_tables_exist),
            ("数据库约束", check_constraints),
            ("数据库索引", check_indexes),
            ("数据完整性", check_data_integrity),
            ("数据库性能", check_database_performance),
        ]

        passed_checks = 0
        total_checks = len(checks)

        for check_name, check_func in checks:
            logger.info(f"🔍 执行检查: {check_name}")
            try:
                if check_func(database_url):
                    passed_checks += 1
                    logger.info(f"✅ {check_name} - 通过")
                else:
                    logger.error(f"❌ {check_name} - 失败")
            except Exception as e:
                logger.error(f"❌ {check_name} - 异常: {e}")

        # 输出总结
        logger.info("=" * 50)
        logger.info(f"🎯 健康检查完成: {passed_checks}/{total_checks} 项通过")

        if passed_checks == total_checks:
            logger.info("🎉 数据库健康状态: 优秀")
            sys.exit(0)
        elif passed_checks >= total_checks * 0.8:
            logger.warning("⚠️ 数据库健康状态: 良好 (有轻微问题)")
            sys.exit(0)
        elif passed_checks >= total_checks * 0.6:
            logger.warning("⚠️ 数据库健康状态: 一般 (需要关注)")
            sys.exit(1)
        else:
            logger.error("❌ 数据库健康状态: 差 (需要立即处理)")
            sys.exit(1)

    except Exception as e:
        logger.error(f"❌ 健康检查发生未知错误: {e}")
        sys.exit(1)


if __name__ == "__main__":
    main()
