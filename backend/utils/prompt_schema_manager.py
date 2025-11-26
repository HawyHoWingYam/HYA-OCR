"""
Prompt and Schema Management System
统一的prompt和schema管理系统，支持S3和本地存储

功能特性:
- S3和本地存储的自动切换
- 内存缓存机制提升性能
- 自动验证prompt和schema格式
- 统一的错误处理和日志记录
- 支持批量操作和迁移
"""

import json
import logging
import os
from typing import Optional, Dict, Tuple, Union
from datetime import datetime, timedelta
from pathlib import Path
import asyncio
from concurrent.futures import ThreadPoolExecutor

from .s3_storage import get_s3_manager, is_s3_enabled

logger = logging.getLogger(__name__)


class PromptSchemaCache:
    """内存缓存管理器"""
    
    def __init__(self, max_size: int = 100, ttl_minutes: int = 30):
        self.cache: Dict[str, Dict] = {}
        self.max_size = max_size
        self.ttl = timedelta(minutes=ttl_minutes)
        
    def _generate_key(self, company_code: str, doc_type_code: str, file_type: str, filename: str) -> str:
        """生成缓存键"""
        return f"{company_code}:{doc_type_code}:{file_type}:{filename}"
    
    def get(self, company_code: str, doc_type_code: str, file_type: str, filename: str) -> Optional[Union[str, dict]]:
        """从缓存获取内容"""
        key = self._generate_key(company_code, doc_type_code, file_type, filename)
        
        if key in self.cache:
            cached_item = self.cache[key]
            # 检查是否过期
            if datetime.now() - cached_item["cached_at"] < self.ttl:
                logger.debug(f"🟢 缓存命中: {key}")
                return cached_item["content"]
            else:
                # 过期，删除
                del self.cache[key]
                logger.debug(f"🔄 缓存过期，已删除: {key}")
        
        return None
    
    def set(self, company_code: str, doc_type_code: str, file_type: str, filename: str, content: Union[str, dict]):
        """设置缓存内容"""
        key = self._generate_key(company_code, doc_type_code, file_type, filename)
        
        # 如果缓存已满，删除最旧的项目
        if len(self.cache) >= self.max_size:
            oldest_key = min(self.cache.keys(), key=lambda k: self.cache[k]["cached_at"])
            del self.cache[oldest_key]
            logger.debug(f"🗑️ 缓存已满，删除最旧项: {oldest_key}")
        
        self.cache[key] = {
            "content": content,
            "cached_at": datetime.now()
        }
        logger.debug(f"💾 缓存已设置: {key}")
    
    def invalidate(self, company_code: str = None, doc_type_code: str = None):
        """使缓存失效"""
        if company_code and doc_type_code:
            # 删除特定公司和文档类型的缓存
            keys_to_delete = [k for k in self.cache.keys() if k.startswith(f"{company_code}:{doc_type_code}:")]
            for key in keys_to_delete:
                del self.cache[key]
            logger.debug(f"🗑️ 已清理缓存: {company_code}:{doc_type_code}")
        else:
            # 清空所有缓存
            self.cache.clear()
            logger.debug("🗑️ 已清空所有缓存")
    
    def get_stats(self) -> dict:
        """获取缓存统计信息"""
        return {
            "total_items": len(self.cache),
            "max_size": self.max_size,
            "ttl_minutes": self.ttl.total_seconds() / 60,
            "items": [
                {
                    "key": key,
                    "cached_at": item["cached_at"].isoformat(),
                    "size_bytes": len(str(item["content"]))
                }
                for key, item in self.cache.items()
            ]
        }


class PromptSchemaValidator:
    """Prompt和Schema验证器"""
    
    def __init__(self):
        """初始化验证器"""
        self.config = {
            "strict_mode": True,
            "prompt_min_length": 10,
            "prompt_max_length": 50000,
            "required_prompt_keywords": ["extract", "analyze", "identify", "process"],
            "schema_required_fields": ["type", "properties"]
        }
    
    def update_config(self, config: dict):
        """更新验证器配置"""
        self.config.update(config)
    
    def validate_prompt(self, content: str) -> Tuple[bool, str]:
        """验证prompt内容"""
        try:
            if not content or not content.strip():
                return False, "Prompt内容不能为空"
            
            # 检查最小长度
            min_length = self.config.get("prompt_min_length", 10)
            if len(content.strip()) < min_length:
                return False, f"Prompt内容过短（最少{min_length}个字符）"
            
            # 检查最大长度
            max_length = self.config.get("prompt_max_length", 50000)
            if len(content) > max_length:
                return False, f"Prompt内容过长（最大{max_length}个字符）"
            
            # 检查是否包含基本的指令词汇（如果启用严格模式）
            if self.config.get("strict_mode", True):
                required_keywords = self.config.get("required_prompt_keywords", [])
                # 添加中文关键词
                all_keywords = required_keywords + ["请", "提取", "分析", "识别"]
                if not any(keyword.lower() in content.lower() for keyword in all_keywords):
                    if self.config.get("strict_mode", True):
                        return False, f"Prompt必须包含以下指令关键词之一: {', '.join(required_keywords)}"
                    else:
                        logger.warning("⚠️ Prompt似乎不包含常见的指令关键词")
            
            return True, "Prompt验证通过"
            
        except Exception as e:
            return False, f"Prompt验证失败: {e}"
    
    def validate_schema(self, schema_data: dict) -> Tuple[bool, str]:
        """验证schema格式"""
        try:
            if not isinstance(schema_data, dict):
                return False, "Schema必须是JSON对象"
            
            # 检查必需的字段
            required_fields = self.config.get("schema_required_fields", ["type", "properties"])
            for field in required_fields:
                if field not in schema_data:
                    return False, f"Schema缺少必需字段: {field}"
            
            # 验证type字段（如果需要）
            if "type" in required_fields and schema_data.get("type") != "object":
                return False, "Schema的type字段必须是'object'"
            
            # 验证properties字段（如果需要）
            if "properties" in required_fields:
                properties = schema_data.get("properties", {})
                if not isinstance(properties, dict):
                    return False, "Schema的properties字段必须是对象"
                
                if self.config.get("strict_mode", True) and len(properties) == 0:
                    return False, "Schema的properties不能为空"
                
                # 检查每个属性的格式
                for prop_name, prop_def in properties.items():
                    if not isinstance(prop_def, dict):
                        return False, f"属性'{prop_name}'的定义必须是对象"
                    
                    if self.config.get("strict_mode", True) and "type" not in prop_def:
                        return False, f"属性'{prop_name}'缺少type字段"
                    
                    if "type" in prop_def:
                        valid_types = ["string", "number", "integer", "boolean", "array", "object"]
                        if prop_def["type"] not in valid_types:
                            return False, f"属性'{prop_name}'的type值无效: {prop_def['type']}"
            
            return True, "Schema验证通过"
            
        except Exception as e:
            return False, f"Schema验证失败: {e}"


class PromptSchemaManager:
    """Prompt和Schema统一管理器"""
    
    def __init__(self, config: Optional[dict] = None):
        """
        初始化管理器
        
        Args:
            config: 可选的配置字典，如果未提供则从config_loader加载
        """
        # 加载配置
        if config is None:
            try:
                from config_loader import config_loader
                self.config = config_loader.get_prompt_schema_config()
            except ImportError:
                # 如果config_loader不可用，使用默认配置
                self.config = self._get_default_config()
        else:
            self.config = config
            
        # 初始化数据库会话（用于查询配置路径）
        self.db_session = None
        
        # 初始化组件
        self.s3_manager = get_s3_manager()
        
        # 根据配置决定是否启用缓存
        cache_config = self.config.get("cache", {})
        if cache_config.get("enabled", True):
            self.cache = PromptSchemaCache(
                cache_config.get("max_size", 100), 
                cache_config.get("ttl_minutes", 30)
            )
        else:
            self.cache = None
        
        self.validator = PromptSchemaValidator()
        
        # 配置线程池
        performance_config = self.config.get("performance", {})
        thread_pool_size = performance_config.get("thread_pool_size", 4)
        self.executor = ThreadPoolExecutor(max_workers=thread_pool_size)
        
        # 本地备份路径（可通过环境变量覆盖，兼容多系统路径）
        backup_config = self.config.get("local_backup", {})
        backup_path = Path(str(backup_config.get("path", "uploads/prompt_schema_backup")))
        storage_root_env = os.getenv("LOCAL_STORAGE_ROOT")
        storage_root = Path(storage_root_env) if storage_root_env else backup_path
        self.local_backup_path = storage_root.expanduser().resolve()

        def _resolve_local_path(env_key: str, default_path: Path) -> Path:
            value = os.getenv(env_key)
            candidate = Path(value) if value else default_path
            return candidate.expanduser().resolve()

        self.prompt_local_path = _resolve_local_path(
            "PROMPT_LOCAL_PATH", self.local_backup_path / "prompts"
        )
        self.schema_local_path = _resolve_local_path(
            "SCHEMA_LOCAL_PATH", self.local_backup_path / "schemas"
        )

        if backup_config.get("enabled", True):
            for path in {
                self.local_backup_path,
                self.prompt_local_path,
                self.schema_local_path,
            }:
                path.mkdir(parents=True, exist_ok=True)
        
        # 更新验证器配置
        validation_config = self.config.get("validation", {})
        self.validator.update_config(validation_config)
        
        logger.info("✅ PromptSchemaManager初始化完成")
        logger.info(f"   - 存储后端: {self.config.get('storage_backend', 'auto')}")
        logger.info(f"   - S3启用: {is_s3_enabled()}")
        logger.info(f"   - 缓存启用: {self.cache is not None}")
        logger.info(f"   - 本地备份: {backup_config.get('enabled', True)}")
    
    def _get_default_config(self) -> dict:
        """获取默认配置"""
        return {
            "storage_backend": "auto",
            "cache": {"enabled": True, "max_size": 100, "ttl_minutes": 30},
            "s3": {
                "enabled": True, "bucket_name": None, "region": "ap-southeast-1",
                "prompt_prefix": "prompts/", "schema_prefix": "schemas/",
                "auto_backup": True, "encryption": True
            },
            "local_backup": {"enabled": True, "path": "uploads/prompt_schema_backup", "auto_sync": True},
            "validation": {
                "strict_mode": True, "prompt_min_length": 10, "prompt_max_length": 50000,
                "required_prompt_keywords": ["extract", "analyze", "identify", "process"],
                "schema_required_fields": ["type", "properties"]
            },
            "performance": {
                "thread_pool_size": 4, "concurrent_uploads": 3,
                "retry_attempts": 3, "timeout_seconds": 30
            }
        }
    
    def _get_local_path(self, company_code: str, doc_type_code: str, filename: str, file_type: str) -> Path:
        """生成本地文件路径"""
        base_dir = self.prompt_local_path if file_type == "prompt" else self.schema_local_path
        return base_dir / company_code / doc_type_code / filename
    
    def _save_local_backup(self, company_code: str, doc_type_code: str, filename: str, 
                          file_type: str, content: Union[str, dict]) -> bool:
        """保存本地备份"""
        try:
            local_path = self._get_local_path(company_code, doc_type_code, filename, file_type)
            local_path.parent.mkdir(parents=True, exist_ok=True)
            
            if file_type == "prompt":
                local_path.write_text(content, encoding="utf-8")
            else:  # schema
                local_path.write_text(json.dumps(content, ensure_ascii=False, indent=2), encoding="utf-8")
            
            logger.debug(f"💾 本地备份已保存: {local_path}")
            return True
            
        except Exception as e:
            logger.error(f"❌ 保存本地备份失败: {e}")
            return False
    
    def _load_local_backup(self, company_code: str, doc_type_code: str, filename: str, 
                          file_type: str) -> Optional[Union[str, dict]]:
        """加载本地备份"""
        try:
            local_path = self._get_local_path(company_code, doc_type_code, filename, file_type)
            
            if not local_path.exists():
                return None
            
            if file_type == "prompt":
                content = local_path.read_text(encoding="utf-8")
                return content
            else:  # schema
                content = local_path.read_text(encoding="utf-8")
                return json.loads(content)
                
        except Exception as e:
            logger.error(f"❌ 加载本地备份失败: {e}")
            return None
    
    def _get_db_session(self):
        """获取数据库会话"""
        if self.db_session is None:
            try:
                from db.database import get_db
                self.db_session = next(get_db())
            except Exception as e:
                logger.error(f"❌ 获取数据库会话失败: {e}")
                return None
        return self.db_session
    
    def _get_config_paths(self, company_code: str, doc_type_code: str) -> Tuple[Optional[str], Optional[str]]:
        """
        从数据库获取公司文档配置的路径
        
        Args:
            company_code: 公司代码
            doc_type_code: 文档类型代码
            
        Returns:
            Tuple[Optional[str], Optional[str]]: (prompt_path, schema_path)
        """
        try:
            db = self._get_db_session()
            if db is None:
                return None, None
                
            from db.models import Company, DocumentType, CompanyDocumentConfig
            
            # 查询配置
            config = db.query(CompanyDocumentConfig).join(
                Company, CompanyDocumentConfig.company_id == Company.company_id
            ).join(
                DocumentType, CompanyDocumentConfig.doc_type_id == DocumentType.doc_type_id
            ).filter(
                Company.company_code == company_code,
                DocumentType.type_code == doc_type_code,
                CompanyDocumentConfig.active == True
            ).first()
            
            if config:
                return config.prompt_path, config.schema_path
            else:
                logger.warning(f"⚠️ 未找到配置: {company_code}/{doc_type_code}")
                return None, None
                
        except Exception as e:
            logger.error(f"❌ 查询数据库配置失败: {e}")
            return None, None
    
    def _extract_s3_key_from_path(self, s3_path: str) -> Optional[str]:
        """
        从完整S3路径中提取key
        
        Args:
            s3_path: 完整S3路径，如 s3://bucket/key/path
            
        Returns:
            Optional[str]: S3 key部分，如 key/path
        """
        try:
            if not s3_path or not s3_path.startswith('s3://'):
                return None
                
            # 移除s3://前缀
            path_without_protocol = s3_path[5:]
            
            # 找到第一个/，分离bucket和key
            slash_index = path_without_protocol.find('/')
            if slash_index == -1:
                return None
                
            # 返回key部分
            key = path_without_protocol[slash_index + 1:]
            return key if key else None
            
        except Exception as e:
            logger.error(f"❌ 解析S3路径失败: {e}")
            return None
    
    async def get_prompt(self, company_code: str, doc_type_code: str, filename: str = "prompt.txt") -> Optional[str]:
        """
        获取prompt内容
        
        Args:
            company_code: 公司代码
            doc_type_code: 文档类型代码
            filename: 文件名
            
        Returns:
            Optional[str]: prompt内容
        """
        try:
            # 1. 尝试从缓存获取
            if self.cache:
                cached_content = self.cache.get(company_code, doc_type_code, "prompt", filename)
                if cached_content is not None:
                    return cached_content
            
            # 2. 从数据库获取实际配置路径
            prompt_path, _ = self._get_config_paths(company_code, doc_type_code)
            
            if prompt_path and self.s3_manager:
                # 2a. 如果是S3路径，直接从S3读取
                if prompt_path.startswith('s3://'):
                    s3_key = self._extract_s3_key_from_path(prompt_path)
                    if s3_key:
                        content = await asyncio.get_event_loop().run_in_executor(
                            self.executor,
                            self.s3_manager.get_file_by_key,
                            s3_key
                        )
                        
                        if content is not None:
                            # 验证内容
                            is_valid, message = self.validator.validate_prompt(content)
                            if not is_valid:
                                logger.warning(f"⚠️ Prompt验证失败: {message}")
                            
                            # 缓存结果
                            if self.cache:
                                self.cache.set(company_code, doc_type_code, "prompt", filename, content)
                            
                            # 保存本地备份
                            self._save_local_backup(company_code, doc_type_code, filename, "prompt", content)
                            
                            return content
                
                # 2b. 回退到旧的约定路径方式
                content = await asyncio.get_event_loop().run_in_executor(
                    self.executor,
                    self.s3_manager.get_prompt,
                    company_code, doc_type_code, filename
                )
                
                if content is not None:
                    # 验证内容
                    is_valid, message = self.validator.validate_prompt(content)
                    if not is_valid:
                        logger.warning(f"⚠️ Prompt验证失败: {message}")
                    
                    # 缓存结果
                    if self.cache:
                        self.cache.set(company_code, doc_type_code, "prompt", filename, content)
                    
                    # 保存本地备份
                    self._save_local_backup(company_code, doc_type_code, filename, "prompt", content)
                    
                    return content
            
            # 3. 回退到本地备份
            content = self._load_local_backup(company_code, doc_type_code, filename, "prompt")
            if content is not None:
                logger.info(f"📂 使用本地备份prompt: {company_code}/{doc_type_code}/{filename}")
                
                # 缓存结果
                if self.cache:
                    self.cache.set(company_code, doc_type_code, "prompt", filename, content)
                
                return content
            
            logger.error(f"❌ 未找到prompt文件: {company_code}/{doc_type_code}/{filename}")
            return None
            
        except Exception as e:
            logger.error(f"❌ 获取prompt失败: {e}")
            return None
    
    async def get_schema(self, company_code: str, doc_type_code: str, filename: str = "schema.json") -> Optional[dict]:
        """
        获取schema数据
        
        Args:
            company_code: 公司代码
            doc_type_code: 文档类型代码
            filename: 文件名
            
        Returns:
            Optional[dict]: schema数据
        """
        try:
            # 1. 尝试从缓存获取
            if self.cache:
                cached_content = self.cache.get(company_code, doc_type_code, "schema", filename)
                if cached_content is not None:
                    return cached_content
            
            # 2. 从数据库获取实际配置路径
            _, schema_path = self._get_config_paths(company_code, doc_type_code)
            
            if schema_path and self.s3_manager:
                # 2a. 如果是S3路径，直接从S3读取
                if schema_path.startswith('s3://'):
                    s3_key = self._extract_s3_key_from_path(schema_path)
                    if s3_key:
                        content = await asyncio.get_event_loop().run_in_executor(
                            self.executor,
                            self.s3_manager.get_schema_by_key,
                            s3_key
                        )
                        
                        if content is not None:
                            # 验证内容
                            is_valid, message = self.validator.validate_schema(content)
                            if not is_valid:
                                logger.warning(f"⚠️ Schema验证失败: {message}")
                            
                            # 缓存结果
                            if self.cache:
                                self.cache.set(company_code, doc_type_code, "schema", filename, content)
                            
                            # 保存本地备份
                            self._save_local_backup(company_code, doc_type_code, filename, "schema", content)
                            
                            return content
                
                # 2b. 回退到旧的约定路径方式
                content = await asyncio.get_event_loop().run_in_executor(
                    self.executor,
                    self.s3_manager.get_schema,
                    company_code, doc_type_code, filename
                )
                
                if content is not None:
                    # 验证内容
                    is_valid, message = self.validator.validate_schema(content)
                    if not is_valid:
                        logger.warning(f"⚠️ Schema验证失败: {message}")
                    
                    # 缓存结果
                    if self.cache:
                        self.cache.set(company_code, doc_type_code, "schema", filename, content)
                    
                    # 保存本地备份
                    self._save_local_backup(company_code, doc_type_code, filename, "schema", content)
                    
                    return content
            
            # 3. 回退到本地备份
            content = self._load_local_backup(company_code, doc_type_code, filename, "schema")
            if content is not None:
                logger.info(f"📂 使用本地备份schema: {company_code}/{doc_type_code}/{filename}")
                
                # 缓存结果
                if self.cache:
                    self.cache.set(company_code, doc_type_code, "schema", filename, content)
                
                return content
            
            logger.error(f"❌ 未找到schema文件: {company_code}/{doc_type_code}/{filename}")
            return None
            
        except Exception as e:
            logger.error(f"❌ 获取schema失败: {e}")
            return None
    
    async def upload_prompt(self, company_code: str, doc_type_code: str, content: str, 
                           filename: str = "prompt.txt", metadata: Optional[dict] = None) -> bool:
        """
        上传prompt
        
        Args:
            company_code: 公司代码
            doc_type_code: 文档类型代码
            content: prompt内容
            filename: 文件名
            metadata: 元数据
            
        Returns:
            bool: 上传是否成功
        """
        try:
            # 验证内容
            is_valid, message = self.validator.validate_prompt(content)
            if not is_valid:
                logger.error(f"❌ Prompt验证失败: {message}")
                return False
            
            # 上传到S3
            success = False
            if self.s3_manager:
                s3_key = await asyncio.get_event_loop().run_in_executor(
                    self.executor,
                    self.s3_manager.upload_prompt,
                    company_code, doc_type_code, content, filename, metadata
                )
                success = s3_key is not None
            
            # 保存本地备份（无论S3是否成功）
            self._save_local_backup(company_code, doc_type_code, filename, "prompt", content)
            
            # 清除缓存
            if self.cache:
                self.cache.invalidate(company_code, doc_type_code)
            
            if success:
                logger.info(f"✅ Prompt上传成功: {company_code}/{doc_type_code}/{filename}")
            else:
                logger.warning(f"⚠️ S3上传失败，但本地备份已保存: {company_code}/{doc_type_code}/{filename}")
            
            return True  # 只要本地备份成功就认为成功
            
        except Exception as e:
            logger.error(f"❌ 上传prompt失败: {e}")
            return False
    
    async def upload_schema(self, company_code: str, doc_type_code: str, schema_data: dict, 
                           filename: str = "schema.json", metadata: Optional[dict] = None) -> bool:
        """
        上传schema
        
        Args:
            company_code: 公司代码
            doc_type_code: 文档类型代码
            schema_data: schema数据
            filename: 文件名
            metadata: 元数据
            
        Returns:
            bool: 上传是否成功
        """
        try:
            # 验证内容
            is_valid, message = self.validator.validate_schema(schema_data)
            if not is_valid:
                logger.error(f"❌ Schema验证失败: {message}")
                return False
            
            # 上传到S3
            success = False
            if self.s3_manager:
                s3_key = await asyncio.get_event_loop().run_in_executor(
                    self.executor,
                    self.s3_manager.upload_schema,
                    company_code, doc_type_code, schema_data, filename, metadata
                )
                success = s3_key is not None
            
            # 保存本地备份（无论S3是否成功）
            self._save_local_backup(company_code, doc_type_code, filename, "schema", schema_data)
            
            # 清除缓存
            if self.cache:
                self.cache.invalidate(company_code, doc_type_code)
            
            if success:
                logger.info(f"✅ Schema上传成功: {company_code}/{doc_type_code}/{filename}")
            else:
                logger.warning(f"⚠️ S3上传失败，但本地备份已保存: {company_code}/{doc_type_code}/{filename}")
            
            return True  # 只要本地备份成功就认为成功
            
        except Exception as e:
            logger.error(f"❌ 上传schema失败: {e}")
            return False
    
    def get_health_status(self) -> dict:
        """获取健康状态"""
        try:
            s3_status = self.s3_manager.get_health_status() if self.s3_manager else {"status": "disabled"}
            cache_stats = self.cache.get_stats() if self.cache else {"status": "disabled"}
            
            prompt_files = sum(1 for p in self.prompt_local_path.rglob("*") if p.is_file()) if self.prompt_local_path.exists() else 0
            schema_files = sum(1 for p in self.schema_local_path.rglob("*") if p.is_file()) if self.schema_local_path.exists() else 0

            return {
                "status": "healthy",
                "s3_storage": s3_status,
                "cache": cache_stats,
                "local_backup_path": str(self.local_backup_path),
                "prompt_local_path": str(self.prompt_local_path),
                "schema_local_path": str(self.schema_local_path),
                "prompt_files_count": prompt_files,
                "schema_files_count": schema_files,
            }
            
        except Exception as e:
            return {
                "status": "unhealthy",
                "error": str(e)
            }
    
    async def list_available_templates(self, company_code: Optional[str] = None) -> dict:
        """列出可用的模板"""
        try:
            result = {
                "prompts": [],
                "schemas": []
            }
            
            # 从S3获取
            if self.s3_manager:
                prompts = await asyncio.get_event_loop().run_in_executor(
                    self.executor,
                    self.s3_manager.list_prompts,
                    company_code
                )
                schemas = await asyncio.get_event_loop().run_in_executor(
                    self.executor,
                    self.s3_manager.list_schemas,
                    company_code
                )
                
                result["prompts"].extend(prompts)
                result["schemas"].extend(schemas)
            
            def _iter_company_dirs(base_dir: Path):
                if not base_dir.exists():
                    return []
                entries = []
                for company_dir in base_dir.iterdir():
                    if not company_dir.is_dir() or (company_code and company_dir.name != company_code):
                        continue
                    for doc_type_dir in company_dir.iterdir():
                        if doc_type_dir.is_dir():
                            entries.append((company_dir, doc_type_dir))
                return entries

            for company_dir, doc_type_dir in _iter_company_dirs(self.prompt_local_path):
                for prompt_file in doc_type_dir.glob("*.txt"):
                    result["prompts"].append({
                        "key": f"{company_dir.name}/{doc_type_dir.name}/{prompt_file.name}",
                        "source": "local_backup",
                        "size": prompt_file.stat().st_size,
                        "last_modified": datetime.fromtimestamp(prompt_file.stat().st_mtime)
                    })

            for company_dir, doc_type_dir in _iter_company_dirs(self.schema_local_path):
                for schema_file in doc_type_dir.glob("*.json"):
                    result["schemas"].append({
                        "key": f"{company_dir.name}/{doc_type_dir.name}/{schema_file.name}",
                        "source": "local_backup",
                        "size": schema_file.stat().st_size,
                        "last_modified": datetime.fromtimestamp(schema_file.stat().st_mtime)
                    })
            
            return result
            
        except Exception as e:
            logger.error(f"❌ 列出模板失败: {e}")
            return {"prompts": [], "schemas": []}


# 全局管理器实例
_prompt_schema_manager = None


def get_prompt_schema_manager() -> PromptSchemaManager:
    """获取全局PromptSchemaManager实例"""
    global _prompt_schema_manager
    
    if _prompt_schema_manager is None:
        _prompt_schema_manager = PromptSchemaManager()
    
    return _prompt_schema_manager


def clean_schema_for_gemini(schema):
    """
    Clean JSON schema for Gemini API compatibility by removing unsupported fields.
    
    Args:
        schema: The JSON schema dictionary
        
    Returns:
        Cleaned schema dictionary safe for Gemini API
    """
    if not isinstance(schema, dict):
        return schema
    
    # Fields that cause Gemini API errors
    problematic_fields = [
        "$schema",
        "$id",
        "$ref",
        "definitions",
        "patternProperties",
        "additionalProperties",  # Gemini response schema does not accept this flag
    ]
    
    cleaned_schema = {}
    for key, value in schema.items():
        if key in problematic_fields:
            logger.info(f"Removing problematic schema field for Gemini compatibility: {key}")
            continue
            
        if isinstance(value, dict):
            # Recursively clean nested dictionaries
            cleaned_value = clean_schema_for_gemini(value)
            # Only add if the cleaned value is not empty
            if cleaned_value:
                cleaned_schema[key] = cleaned_value
        elif isinstance(value, list):
            cleaned_schema[key] = [
                clean_schema_for_gemini(item) if isinstance(item, dict) else item
                for item in value
            ]
        else:
            cleaned_schema[key] = value
    
    return cleaned_schema


async def load_prompt_and_schema(company_code: str, doc_type_code: str) -> Tuple[Optional[str], Optional[dict]]:
    """
    便捷函数：同时加载prompt和schema，并自动清理schema以兼容Gemini API
    
    Args:
        company_code: 公司代码
        doc_type_code: 文档类型代码
        
    Returns:
        Tuple[Optional[str], Optional[dict]]: (prompt内容, 清理后的schema数据)
    """
    manager = get_prompt_schema_manager()
    
    # 并行加载
    prompt_task = manager.get_prompt(company_code, doc_type_code)
    schema_task = manager.get_schema(company_code, doc_type_code)
    
    prompt_content, schema_data = await asyncio.gather(prompt_task, schema_task)
    
    # Clean schema for Gemini API compatibility
    if schema_data:
        schema_data = clean_schema_for_gemini(schema_data)
        logger.debug(f"Schema cleaned for Gemini API compatibility: {company_code}/{doc_type_code}")
    
    return prompt_content, schema_data
