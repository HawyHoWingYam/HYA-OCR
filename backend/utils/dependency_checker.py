"""
統一的依賴檢查服務
用於檢查實體刪除前的依賴關係，確保數據完整性
"""

from typing import Dict, List, Optional, Tuple
from sqlalchemy.orm import Session
from db.models import (
    Company, DocumentType, ProcessingJob, BatchJob,
    CompanyDocTypeConfig, DepartmentDocTypeAccess
)
import logging

logger = logging.getLogger(__name__)

class DependencyChecker:
    """統一的依賴檢查服務"""
    
    def __init__(self, db: Session):
        self.db = db
    
    def check_company_dependencies(self, company_id: int) -> Dict[str, any]:
        """
        檢查公司的所有依賴關係
        
        Args:
            company_id: 公司ID
            
        Returns:
            Dict包含依賴檢查結果
        """
        company = self.db.query(Company).filter(Company.company_id == company_id).first()
        if not company:
            return {"exists": False, "error": "Company not found"}
        
        # 檢查 processing_jobs 依賴
        processing_jobs_count = (
            self.db.query(ProcessingJob)
            .filter(ProcessingJob.company_id == company_id)
            .count()
        )
        
        # 檢查 batch_jobs 依賴
        batch_jobs_count = (
            self.db.query(BatchJob)
            .filter(BatchJob.company_id == company_id)
            .count()
        )
        
        # 檢查 company_document_configs 依賴
        config_count = (
            self.db.query(CompanyDocTypeConfig)
            .filter(CompanyDocTypeConfig.company_id == company_id)
            .count()
        )
        
        dependencies = {
            "processing_jobs": processing_jobs_count,
            "batch_jobs": batch_jobs_count,
            "company_configs": config_count
        }
        
        total_dependencies = sum(dependencies.values())
        
        return {
            "exists": True,
            "company_name": company.company_name,
            "can_delete": total_dependencies == 0,
            "total_dependencies": total_dependencies,
            "dependencies": dependencies,
            "blocking_message": self._generate_company_blocking_message(company.company_name, dependencies) if total_dependencies > 0 else None
        }
    
    def check_document_type_dependencies(self, doc_type_id: int) -> Dict[str, any]:
        """
        檢查文檔類型的所有依賴關係
        
        Args:
            doc_type_id: 文檔類型ID
            
        Returns:
            Dict包含依賴檢查結果
        """
        doc_type = self.db.query(DocumentType).filter(DocumentType.doc_type_id == doc_type_id).first()
        if not doc_type:
            return {"exists": False, "error": "Document type not found"}
        
        # 檢查 processing_jobs 依賴
        processing_jobs_count = (
            self.db.query(ProcessingJob)
            .filter(ProcessingJob.doc_type_id == doc_type_id)
            .count()
        )
        
        # 檢查 batch_jobs 依賴
        batch_jobs_count = (
            self.db.query(BatchJob)
            .filter(BatchJob.doc_type_id == doc_type_id)
            .count()
        )
        
        # 檢查 company_document_configs 依賴
        config_count = (
            self.db.query(CompanyDocTypeConfig)
            .filter(CompanyDocTypeConfig.doc_type_id == doc_type_id)
            .count()
        )
        
        # 檢查 department_doc_type_access 依賴
        dept_access_count = (
            self.db.query(DepartmentDocTypeAccess)
            .filter(DepartmentDocTypeAccess.doc_type_id == doc_type_id)
            .count()
        )
        
        dependencies = {
            "processing_jobs": processing_jobs_count,
            "batch_jobs": batch_jobs_count,
            "company_configs": config_count,
            "department_access": dept_access_count
        }
        
        total_dependencies = sum(dependencies.values())
        
        return {
            "exists": True,
            "type_name": doc_type.type_name,
            "can_delete": total_dependencies == 0,
            "total_dependencies": total_dependencies,
            "dependencies": dependencies,
            "blocking_message": self._generate_document_type_blocking_message(doc_type.type_name, dependencies) if total_dependencies > 0 else None
        }
    
    def get_detailed_dependencies(self, entity_type: str, entity_id: int) -> Dict[str, List[Dict]]:
        """
        獲取詳細的依賴關係信息，包括具體的記錄
        
        Args:
            entity_type: 'company' 或 'document_type'
            entity_id: 實體ID
            
        Returns:
            包含詳細依賴信息的字典
        """
        detailed_deps = {}
        
        if entity_type == "company":
            # 獲取相關的 processing jobs
            processing_jobs = (
                self.db.query(ProcessingJob)
                .filter(ProcessingJob.company_id == entity_id)
                .limit(10)  # 限制顯示數量
                .all()
            )
            detailed_deps["processing_jobs"] = [
                {
                    "job_id": job.job_id,
                    "filename": job.original_filename,
                    "status": job.status,
                    "created_at": job.created_at.isoformat() if job.created_at else None
                }
                for job in processing_jobs
            ]
            
            # 獲取相關的 batch jobs
            batch_jobs = (
                self.db.query(BatchJob)
                .filter(BatchJob.company_id == entity_id)
                .limit(10)
                .all()
            )
            detailed_deps["batch_jobs"] = [
                {
                    "batch_id": batch.batch_id,
                    "status": batch.status,
                    "created_at": batch.created_at.isoformat() if batch.created_at else None
                }
                for batch in batch_jobs
            ]
            
        elif entity_type == "document_type":
            # 獲取相關的 processing jobs
            processing_jobs = (
                self.db.query(ProcessingJob)
                .filter(ProcessingJob.doc_type_id == entity_id)
                .limit(10)
                .all()
            )
            detailed_deps["processing_jobs"] = [
                {
                    "job_id": job.job_id,
                    "filename": job.original_filename,
                    "status": job.status,
                    "company_id": job.company_id,
                    "created_at": job.created_at.isoformat() if job.created_at else None
                }
                for job in processing_jobs
            ]
        
        return detailed_deps
    
    def suggest_migration_targets(self, entity_type: str, entity_id: int) -> List[Dict]:
        """
        建議可用的遷移目標
        
        Args:
            entity_type: 'company' 或 'document_type'
            entity_id: 當前實體ID
            
        Returns:
            可用遷移目標列表
        """
        targets = []
        
        if entity_type == "company":
            # 建議其他活躍的公司
            other_companies = (
                self.db.query(Company)
                .filter(Company.company_id != entity_id)
                .filter(Company.active == True)
                .all()
            )
            targets = [
                {
                    "id": company.company_id,
                    "name": company.company_name,
                    "code": company.company_code
                }
                for company in other_companies
            ]
            
        elif entity_type == "document_type":
            # 建議其他可用的文檔類型
            other_doc_types = (
                self.db.query(DocumentType)
                .filter(DocumentType.doc_type_id != entity_id)
                .all()
            )
            targets = [
                {
                    "id": doc_type.doc_type_id,
                    "name": doc_type.type_name,
                    "code": doc_type.type_code
                }
                for doc_type in other_doc_types
            ]
        
        return targets
    
    def _generate_company_blocking_message(self, company_name: str, dependencies: Dict[str, int]) -> str:
        """生成公司刪除阻塞消息"""
        blocking_items = []
        
        if dependencies["processing_jobs"] > 0:
            blocking_items.append(f"{dependencies['processing_jobs']} processing job(s)")
        if dependencies["batch_jobs"] > 0:
            blocking_items.append(f"{dependencies['batch_jobs']} batch job(s)")
        if dependencies["company_configs"] > 0:
            blocking_items.append(f"{dependencies['company_configs']} company configuration(s)")
        
        items_text = ", ".join(blocking_items)
        return f"Cannot delete company '{company_name}': {items_text} exist. Delete or reassign them first."
    
    def _generate_document_type_blocking_message(self, type_name: str, dependencies: Dict[str, int]) -> str:
        """生成文檔類型刪除阻塞消息"""
        blocking_items = []
        
        if dependencies["processing_jobs"] > 0:
            blocking_items.append(f"{dependencies['processing_jobs']} processing job(s)")
        if dependencies["batch_jobs"] > 0:
            blocking_items.append(f"{dependencies['batch_jobs']} batch job(s)")
        if dependencies["company_configs"] > 0:
            blocking_items.append(f"{dependencies['company_configs']} company configuration(s)")
        if dependencies["department_access"] > 0:
            blocking_items.append(f"{dependencies['department_access']} department access rule(s)")
        
        items_text = ", ".join(blocking_items)
        return f"Cannot delete document type '{type_name}': {items_text} exist. Delete or reassign them first."


# 便利函數
def check_can_delete_company(db: Session, company_id: int) -> Tuple[bool, Optional[str]]:
    """
    快速檢查公司是否可以刪除
    
    Returns:
        (can_delete: bool, error_message: Optional[str])
    """
    checker = DependencyChecker(db)
    result = checker.check_company_dependencies(company_id)
    
    if not result["exists"]:
        return False, result["error"]
    
    return result["can_delete"], result.get("blocking_message")


def check_can_delete_document_type(db: Session, doc_type_id: int) -> Tuple[bool, Optional[str]]:
    """
    快速檢查文檔類型是否可以刪除
    
    Returns:
        (can_delete: bool, error_message: Optional[str])
    """
    checker = DependencyChecker(db)
    result = checker.check_document_type_dependencies(doc_type_id)
    
    if not result["exists"]:
        return False, result["error"]
    
    return result["can_delete"], result.get("blocking_message")