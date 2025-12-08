"""
強制刪除管理器 - 處理實體及其所有依賴的強制刪除
"""
import logging
from typing import Dict, Any, List
from sqlalchemy.orm import Session
from sqlalchemy import text
from db.models import (
    Company, DocumentType, CompanyDocTypeConfig,
    ProcessingJob, BatchJob, File as DBFile, DocumentFile, ApiUsage
)
from utils.s3_storage import get_s3_manager

logger = logging.getLogger(__name__)

class ForceDeleteManager:
    """管理強制刪除操作的核心類"""
    
    def __init__(self, db: Session):
        self.db = db
        self.s3_manager = get_s3_manager()
    
    def force_delete_document_type(self, doc_type_id: int) -> Dict[str, Any]:
        """
        強制刪除文檔類型及其所有依賴
        刪除順序: ProcessingJobs → BatchJobs → Configs → DocumentType
        """
        try:
            # 開始事務
            self.db.begin()
            
            # 獲取文檔類型信息
            doc_type = self.db.query(DocumentType).filter(DocumentType.doc_type_id == doc_type_id).first()
            if not doc_type:
                raise ValueError(f"Document type with ID {doc_type_id} not found")
            
            doc_type_name = doc_type.type_name
            deletion_stats = {
                "processing_jobs": 0,
                "batch_jobs": 0,
                "configs": 0,
                "s3_files": 0
            }
            
            logger.info(f"Starting force delete for document type: {doc_type_name}")
            
            # 1. 刪除 ProcessingJob 記錄
            processing_jobs = self.db.query(ProcessingJob).filter(
                ProcessingJob.doc_type_id == doc_type_id
            ).all()
            
            for job in processing_jobs:
                # 刪除相關的 API 使用記錄
                api_usages = self.db.query(ApiUsage).filter(ApiUsage.job_id == job.job_id).all()
                for api_usage in api_usages:
                    self.db.delete(api_usage)
                
                # 刪除 ProcessingJob 相關的 S3 文件
                s3_deleted = self._delete_processing_job_s3_files(job)
                deletion_stats["s3_files"] += s3_deleted
                
                # 刪除相關的文件記錄 (通過 document_files 關聯表)
                document_files = self.db.query(DocumentFile).filter(DocumentFile.job_id == job.job_id).all()
                for doc_file in document_files:
                    # 刪除實際的文件記錄和 S3 文件
                    file_record = self.db.query(DBFile).filter(DBFile.file_id == doc_file.file_id).first()
                    if file_record:
                        # 刪除文件對應的 S3 文件
                        s3_deleted = self._delete_file_record_s3_files(file_record)
                        deletion_stats["s3_files"] += s3_deleted
                        self.db.delete(file_record)
                    # 刪除關聯記錄
                    self.db.delete(doc_file)
                # 刪除處理任務
                self.db.delete(job)
                deletion_stats["processing_jobs"] += 1
            
            # 2. 刪除 BatchJob 記錄
            batch_jobs = self.db.query(BatchJob).filter(
                BatchJob.doc_type_id == doc_type_id
            ).all()
            
            for batch_job in batch_jobs:
                # 刪除 BatchJob 相關的 S3 文件
                s3_deleted = self._delete_batch_job_s3_files(batch_job)
                deletion_stats["s3_files"] += s3_deleted
                
                self.db.delete(batch_job)
                deletion_stats["batch_jobs"] += 1
            
            # 3. 刪除 CompanyDocTypeConfig 記錄和相關 S3 文件
            configs = self.db.query(CompanyDocTypeConfig).filter(
                CompanyDocTypeConfig.doc_type_id == doc_type_id
            ).all()
            
            for config in configs:
                # 刪除 S3 文件
                s3_deleted = self._delete_config_s3_files(config)
                deletion_stats["s3_files"] += s3_deleted
                
                # 刪除配置記錄
                self.db.delete(config)
                deletion_stats["configs"] += 1
            
            # 4. 最後刪除 DocumentType
            self.db.delete(doc_type)
            
            # 提交事務
            self.db.commit()
            
            logger.info(f"Successfully force deleted document type: {doc_type_name}")
            logger.info(f"Deletion statistics: {deletion_stats}")
            
            return {
                "success": True,
                "message": f"Successfully force deleted document type '{doc_type_name}' and all dependencies",
                "deleted_entity": doc_type_name,
                "statistics": deletion_stats
            }
            
        except Exception as e:
            # 回滾事務
            self.db.rollback()
            logger.error(f"Failed to force delete document type {doc_type_id}: {str(e)}")
            raise Exception(f"Force delete failed: {str(e)}")
    
    def force_delete_company(self, company_id: int) -> Dict[str, Any]:
        """
        強制刪除公司及其所有依賴
        刪除順序: ProcessingJobs → BatchJobs → Configs → Company
        """
        try:
            # 開始事務
            self.db.begin()
            
            # 獲取公司信息
            company = self.db.query(Company).filter(Company.company_id == company_id).first()
            if not company:
                raise ValueError(f"Company with ID {company_id} not found")
            
            company_name = company.company_name
            deletion_stats = {
                "processing_jobs": 0,
                "batch_jobs": 0,
                "configs": 0,
                "s3_files": 0
            }
            
            logger.info(f"Starting force delete for company: {company_name}")
            
            # 1. 刪除 ProcessingJob 記錄
            processing_jobs = self.db.query(ProcessingJob).filter(
                ProcessingJob.company_id == company_id
            ).all()
            
            for job in processing_jobs:
                # 刪除相關的 API 使用記錄
                api_usages = self.db.query(ApiUsage).filter(ApiUsage.job_id == job.job_id).all()
                for api_usage in api_usages:
                    self.db.delete(api_usage)
                
                # 刪除 ProcessingJob 相關的 S3 文件
                s3_deleted = self._delete_processing_job_s3_files(job)
                deletion_stats["s3_files"] += s3_deleted
                
                # 刪除相關的文件記錄 (通過 document_files 關聯表)
                document_files = self.db.query(DocumentFile).filter(DocumentFile.job_id == job.job_id).all()
                for doc_file in document_files:
                    # 刪除實際的文件記錄和 S3 文件
                    file_record = self.db.query(DBFile).filter(DBFile.file_id == doc_file.file_id).first()
                    if file_record:
                        # 刪除文件對應的 S3 文件
                        s3_deleted = self._delete_file_record_s3_files(file_record)
                        deletion_stats["s3_files"] += s3_deleted
                        self.db.delete(file_record)
                    # 刪除關聯記錄
                    self.db.delete(doc_file)
                # 刪除處理任務
                self.db.delete(job)
                deletion_stats["processing_jobs"] += 1
            
            # 2. 刪除 BatchJob 記錄
            batch_jobs = self.db.query(BatchJob).filter(
                BatchJob.company_id == company_id
            ).all()
            
            for batch_job in batch_jobs:
                # 刪除 BatchJob 相關的 S3 文件
                s3_deleted = self._delete_batch_job_s3_files(batch_job)
                deletion_stats["s3_files"] += s3_deleted
                
                self.db.delete(batch_job)
                deletion_stats["batch_jobs"] += 1
            
            # 3. 刪除 CompanyDocTypeConfig 記錄和相關 S3 文件
            configs = self.db.query(CompanyDocTypeConfig).filter(
                CompanyDocTypeConfig.company_id == company_id
            ).all()
            
            for config in configs:
                # 刪除 S3 文件
                s3_deleted = self._delete_config_s3_files(config)
                deletion_stats["s3_files"] += s3_deleted
                
                # 刪除配置記錄
                self.db.delete(config)
                deletion_stats["configs"] += 1
            
            # 4. 最後刪除 Company
            self.db.delete(company)
            
            # 提交事務
            self.db.commit()
            
            logger.info(f"Successfully force deleted company: {company_name}")
            logger.info(f"Deletion statistics: {deletion_stats}")
            
            return {
                "success": True,
                "message": f"Successfully force deleted company '{company_name}' and all dependencies",
                "deleted_entity": company_name,
                "statistics": deletion_stats
            }
            
        except Exception as e:
            # 回滾事務
            self.db.rollback()
            logger.error(f"Failed to force delete company {company_id}: {str(e)}")
            raise Exception(f"Force delete failed: {str(e)}")
    
    def force_delete_config(self, config_id: int) -> Dict[str, Any]:
        """
        強制刪除配置及其相關 S3 文件
        刪除順序: S3文件 → Config記錄
        """
        try:
            # 開始事務
            self.db.begin()
            
            # 獲取配置信息
            config = self.db.query(CompanyDocTypeConfig).filter(
                CompanyDocTypeConfig.config_id == config_id
            ).first()
            if not config:
                raise ValueError(f"Configuration with ID {config_id} not found")
            
            config_name = f"{config.company.company_name} - {config.document_type.type_name}"
            deletion_stats = {
                "s3_files": 0,
                "configs": 1
            }
            
            logger.info(f"Starting force delete for config: {config_name}")
            
            # 1. 刪除 S3 文件
            s3_deleted = self._delete_config_s3_files(config)
            deletion_stats["s3_files"] = s3_deleted
            
            # 2. 刪除配置記錄
            self.db.delete(config)
            
            # 提交事務
            self.db.commit()
            
            logger.info(f"Successfully force deleted config: {config_name}")
            logger.info(f"Deletion statistics: {deletion_stats}")
            
            return {
                "success": True,
                "message": f"Successfully force deleted configuration '{config_name}' and related files",
                "deleted_entity": config_name,
                "statistics": deletion_stats
            }
            
        except Exception as e:
            # 回滾事務
            self.db.rollback()
            logger.error(f"Failed to force delete config {config_id}: {str(e)}")
            raise Exception(f"Force delete failed: {str(e)}")
    
    def _delete_config_s3_files(self, config: CompanyDocTypeConfig) -> int:
        """刪除配置相關的 S3 文件"""
        deleted_count = 0
        
        try:
            # 刪除 prompt 文件
            if config.prompt_path and config.prompt_path.startswith('s3://'):
                try:
                    # 從 s3://bucket/path 格式中提取 bucket 和 key
                    s3_path = config.prompt_path.replace('s3://', '')
                    parts = s3_path.split('/', 1)
                    if len(parts) == 2:
                        bucket, key = parts
                        if self.s3_manager:
                            self.s3_manager.delete_file(key)
                            deleted_count += 1
                            logger.info(f"Deleted S3 prompt file: {config.prompt_path}")
                except Exception as e:
                    logger.warning(f"Failed to delete S3 prompt file {config.prompt_path}: {e}")
            
            # 刪除 schema 文件
            if config.schema_path and config.schema_path.startswith('s3://'):
                try:
                    # 從 s3://bucket/path 格式中提取 bucket 和 key
                    s3_path = config.schema_path.replace('s3://', '')
                    parts = s3_path.split('/', 1)
                    if len(parts) == 2:
                        bucket, key = parts
                        if self.s3_manager:
                            self.s3_manager.delete_file(key)
                            deleted_count += 1
                            logger.info(f"Deleted S3 schema file: {config.schema_path}")
                except Exception as e:
                    logger.warning(f"Failed to delete S3 schema file {config.schema_path}: {e}")
                    
        except Exception as e:
            logger.warning(f"Error during S3 file deletion: {e}")
        
        return deleted_count
    
    def _delete_processing_job_s3_files(self, job: ProcessingJob) -> int:
        """刪除 ProcessingJob 相關的 S3 文件"""
        deleted_count = 0
        
        try:
            # 刪除 PDF 文件 (原始上傳文件)
            if job.s3_pdf_path and job.s3_pdf_path.startswith('s3://'):
                try:
                    s3_path = job.s3_pdf_path.replace('s3://', '')
                    parts = s3_path.split('/', 1)
                    if len(parts) == 2:
                        bucket, key = parts
                        if self.s3_manager:
                            self.s3_manager.delete_file(key)
                            deleted_count += 1
                            logger.info(f"Deleted S3 PDF file: {job.s3_pdf_path}")
                except Exception as e:
                    logger.warning(f"Failed to delete S3 PDF file {job.s3_pdf_path}: {e}")
            
            # 刪除 JSON 結果文件
            if job.s3_json_path and job.s3_json_path.startswith('s3://'):
                try:
                    s3_path = job.s3_json_path.replace('s3://', '')
                    parts = s3_path.split('/', 1)
                    if len(parts) == 2:
                        bucket, key = parts
                        if self.s3_manager:
                            self.s3_manager.delete_file(key)
                            deleted_count += 1
                            logger.info(f"Deleted S3 JSON file: {job.s3_json_path}")
                except Exception as e:
                    logger.warning(f"Failed to delete S3 JSON file {job.s3_json_path}: {e}")
            
            # 刪除 Excel 結果文件
            if job.s3_excel_path and job.s3_excel_path.startswith('s3://'):
                try:
                    s3_path = job.s3_excel_path.replace('s3://', '')
                    parts = s3_path.split('/', 1)
                    if len(parts) == 2:
                        bucket, key = parts
                        if self.s3_manager:
                            self.s3_manager.delete_file(key)
                            deleted_count += 1
                            logger.info(f"Deleted S3 Excel file: {job.s3_excel_path}")
                except Exception as e:
                    logger.warning(f"Failed to delete S3 Excel file {job.s3_excel_path}: {e}")
                    
        except Exception as e:
            logger.warning(f"Error during ProcessingJob S3 file deletion: {e}")
        
        return deleted_count
    
    def _delete_batch_job_s3_files(self, batch_job: BatchJob) -> int:
        """刪除 BatchJob 相關的 S3 文件"""
        deleted_count = 0
        
        try:
            # 刪除 ZIP 文件
            if batch_job.s3_upload_path and batch_job.s3_upload_path.startswith('s3://'):
                try:
                    s3_path = batch_job.s3_upload_path.replace('s3://', '')
                    parts = s3_path.split('/', 1)
                    if len(parts) == 2:
                        bucket, key = parts
                        if self.s3_manager:
                            self.s3_manager.delete_file(key)
                            deleted_count += 1
                            logger.info(f"Deleted S3 ZIP file: {batch_job.s3_upload_path}")
                except Exception as e:
                    logger.warning(f"Failed to delete S3 ZIP file {batch_job.s3_upload_path}: {e}")
            
            # 刪除 JSON 輸出文件
            if batch_job.json_output_path and batch_job.json_output_path.startswith('s3://'):
                try:
                    s3_path = batch_job.json_output_path.replace('s3://', '')
                    parts = s3_path.split('/', 1)
                    if len(parts) == 2:
                        bucket, key = parts
                        if self.s3_manager:
                            self.s3_manager.delete_file(key)
                            deleted_count += 1
                            logger.info(f"Deleted S3 JSON output file: {batch_job.json_output_path}")
                except Exception as e:
                    logger.warning(f"Failed to delete S3 JSON output file {batch_job.json_output_path}: {e}")
            
            # 刪除 Excel 輸出文件
            if batch_job.excel_output_path and batch_job.excel_output_path.startswith('s3://'):
                try:
                    s3_path = batch_job.excel_output_path.replace('s3://', '')
                    parts = s3_path.split('/', 1)
                    if len(parts) == 2:
                        bucket, key = parts
                        if self.s3_manager:
                            self.s3_manager.delete_file(key)
                            deleted_count += 1
                            logger.info(f"Deleted S3 Excel output file: {batch_job.excel_output_path}")
                except Exception as e:
                    logger.warning(f"Failed to delete S3 Excel output file {batch_job.excel_output_path}: {e}")
                    
        except Exception as e:
            logger.warning(f"Error during BatchJob S3 file deletion: {e}")
        
        return deleted_count
    
    def _delete_file_record_s3_files(self, file_record: DBFile) -> int:
        """刪除 File 記錄相關的 S3 文件"""
        deleted_count = 0
        
        try:
            # 根據 s3_bucket 和 s3_key 刪除 S3 文件
            if file_record.s3_bucket and file_record.s3_key:
                try:
                    if self.s3_manager:
                        self.s3_manager.delete_file(file_record.s3_key)
                        deleted_count += 1
                        logger.info(f"Deleted S3 file: s3://{file_record.s3_bucket}/{file_record.s3_key}")
                except Exception as e:
                    logger.warning(f"Failed to delete S3 file s3://{file_record.s3_bucket}/{file_record.s3_key}: {e}")
                    
        except Exception as e:
            logger.warning(f"Error during File record S3 file deletion: {e}")
        
        return deleted_count

    def force_delete_batch_job(self, batch_id: int) -> Dict[str, Any]:
        """
        Force delete a single batch job and all its dependencies
        Deletion order: Related ProcessingJobs → BatchJob files → BatchJob record
        """
        try:
            # Get batch job info
            batch_job = self.db.query(BatchJob).filter(BatchJob.batch_id == batch_id).first()
            if not batch_job:
                raise ValueError(f"Batch job with ID {batch_id} not found")
            
            batch_name = f"Batch #{batch_job.batch_id} ({batch_job.upload_description})"
            deletion_stats = {
                "processing_jobs": 0,
                "document_files": 0,
                "file_records": 0,
                "api_usages": 0,
                "s3_files": 0,
                "batch_job": 1
            }
            
            logger.info(f"Starting force delete for batch job: {batch_name}")
            
            # 1. Delete all related ProcessingJobs and their dependencies
            processing_jobs = self.db.query(ProcessingJob).filter(
                ProcessingJob.batch_id == batch_id
            ).all()
            
            for job in processing_jobs:
                # Delete related API usage records
                api_usages = self.db.query(ApiUsage).filter(ApiUsage.job_id == job.job_id).all()
                for api_usage in api_usages:
                    self.db.delete(api_usage)
                    deletion_stats["api_usages"] += 1
                
                # Delete ProcessingJob S3 files
                s3_deleted = self._delete_processing_job_s3_files(job)
                deletion_stats["s3_files"] += s3_deleted
                
                # Delete related file records (through document_files association table)
                document_files = self.db.query(DocumentFile).filter(DocumentFile.job_id == job.job_id).all()
                for doc_file in document_files:
                    # Delete actual file record and S3 files
                    file_record = self.db.query(DBFile).filter(DBFile.file_id == doc_file.file_id).first()
                    if file_record:
                        # Delete file's corresponding S3 files
                        s3_deleted = self._delete_file_record_s3_files(file_record)
                        deletion_stats["s3_files"] += s3_deleted
                        self.db.delete(file_record)
                        deletion_stats["file_records"] += 1
                    # Delete association record
                    self.db.delete(doc_file)
                    deletion_stats["document_files"] += 1
                
                # Delete processing job
                self.db.delete(job)
                deletion_stats["processing_jobs"] += 1
            
            # 2. Delete BatchJob S3 files
            s3_deleted = self._delete_batch_job_s3_files(batch_job)
            deletion_stats["s3_files"] += s3_deleted
            
            # 3. Delete the BatchJob record itself
            self.db.delete(batch_job)
            
            # Commit transaction
            self.db.commit()
            
            logger.info(f"Successfully force deleted batch job: {batch_name}")
            logger.info(f"Deletion statistics: {deletion_stats}")
            
            return {
                "success": True,
                "message": f"Successfully deleted batch job '{batch_name}' and all related files",
                "deleted_entity": batch_name,
                "statistics": deletion_stats
            }
            
        except Exception as e:
            # Rollback transaction
            self.db.rollback()
            logger.error(f"Failed to force delete batch job {batch_id}: {str(e)}")
            raise Exception(f"Batch job deletion failed: {str(e)}")