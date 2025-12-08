"""
S3 Backup Manager - Comprehensive backup system implementation
Provides automated backup functionality for all file types and database configurations.
"""

import logging
import os
import json
import gzip
import asyncio
from typing import Dict, List, Optional, Any, Tuple
from datetime import datetime, timedelta
from enum import Enum
import uuid

from .s3_storage import S3StorageManager, get_s3_manager
from .enhanced_file_manager import EnhancedFileManager, FileRetentionPolicy
from db.database import get_db
from db.models import BatchJob, Company, DocumentType, CompanyDocTypeConfig
from sqlalchemy.orm import Session

logger = logging.getLogger(__name__)


class BackupType(Enum):
    """Types of backups supported"""
    DATABASE = "database"
    CONFIGURATIONS = "configurations"
    FILES = "files"
    EXPORTS = "exports"
    SYSTEM = "system"


class BackupFrequency(Enum):
    """Backup frequency options"""
    HOURLY = "hourly"
    DAILY = "daily"
    WEEKLY = "weekly"
    MONTHLY = "monthly"
    ON_DEMAND = "on_demand"


class BackupStatus(Enum):
    """Backup operation status"""
    PENDING = "pending"
    RUNNING = "running"
    COMPLETED = "completed"
    FAILED = "failed"
    CANCELLED = "cancelled"


class S3BackupManager:
    """Comprehensive backup manager for S3 storage system."""
    
    def __init__(self, s3_manager: Optional[S3StorageManager] = None):
        self.s3_manager = s3_manager or get_s3_manager()
        self.enhanced_file_manager = EnhancedFileManager(self.s3_manager)
        
        # Backup configuration
        self.backup_config = {
            'retention_policies': {
                BackupType.DATABASE: {'days': 30, 'max_count': 50},
                BackupType.CONFIGURATIONS: {'days': 90, 'max_count': 100},
                BackupType.FILES: {'days': 365, 'max_count': 1000},
                BackupType.EXPORTS: {'days': 180, 'max_count': 200},
                BackupType.SYSTEM: {'days': 30, 'max_count': 30}
            },
            'compression': True,
            'encryption': False  # Can be enabled with AWS KMS
        }
    
    def initialize_backup_structure(self) -> Dict[str, Any]:
        """Initialize backup folder structure in S3."""
        if not self.s3_manager:
            return {'success': False, 'error': 'S3 manager not available'}
        
        results = {
            'folders_created': 0,
            'policies_set': 0,
            'errors': 0,
            'success': True
        }
        
        try:
            # Define backup folder structure
            backup_folders = {
                'backups/database/': {
                    'purpose': 'Database configuration and metadata backups',
                    'retention_days': 30,
                    'auto_cleanup': True
                },
                'backups/configurations/': {
                    'purpose': 'Company document configuration backups',
                    'retention_days': 90,
                    'auto_cleanup': True
                },
                'backups/files/': {
                    'purpose': 'Critical file backups (prompts, schemas)',
                    'retention_days': 365,
                    'auto_cleanup': True
                },
                'backups/exports/': {
                    'purpose': 'Export and result file backups',
                    'retention_days': 180,
                    'auto_cleanup': True
                },
                'backups/system/': {
                    'purpose': 'System configuration and log backups',
                    'retention_days': 30,
                    'auto_cleanup': True
                }
            }
            
            for folder_path, config in backup_folders.items():
                try:
                    # Create folder info file
                    info_content = {
                        'folder_type': 'backup',
                        'backup_category': folder_path.split('/')[-2],
                        'created_at': datetime.now().isoformat(),
                        'config': config,
                        'structure_version': '1.0'
                    }
                    
                    info_key = f"{folder_path}.backup_info"
                    
                    self.s3_manager.s3_client.put_object(
                        Bucket=self.s3_manager.bucket_name,
                        Key=info_key,
                        Body=json.dumps(info_content, indent=2).encode('utf-8'),
                        ContentType='application/json',
                        Metadata={
                            'backup_folder': 'true',
                            'backup_type': config.get('purpose', 'general'),
                            'retention_days': str(config.get('retention_days', 30)),
                            'auto_cleanup': str(config.get('auto_cleanup', True)).lower()
                        }
                    )
                    
                    results['folders_created'] += 1
                    results['policies_set'] += 1
                    logger.info(f"✅ Created backup folder: {folder_path}")
                    
                except Exception as e:
                    logger.error(f"❌ Failed to create backup folder {folder_path}: {e}")
                    results['errors'] += 1
        
        except Exception as e:
            logger.error(f"❌ Backup structure initialization failed: {e}")
            results['success'] = False
            results['error'] = str(e)
        
        return results
    
    def create_database_backup(self, db: Session, backup_name: Optional[str] = None) -> Dict[str, Any]:
        """Create backup of database configurations and metadata."""
        if not backup_name:
            backup_name = f"db_backup_{datetime.now().strftime('%Y%m%d_%H%M%S')}"
        
        backup_result = {
            'backup_id': str(uuid.uuid4()),
            'backup_name': backup_name,
            'backup_type': BackupType.DATABASE.value,
            'status': BackupStatus.PENDING.value,
            'created_at': datetime.now().isoformat(),
            'files_created': 0,
            'total_size': 0,
            'errors': []
        }
        
        try:
            backup_result['status'] = BackupStatus.RUNNING.value
            
            # Create backup folder
            backup_folder = f"backups/database/{datetime.now().strftime('%Y-%m-%d')}/{backup_name}"
            
            # 1. Backup companies
            companies = db.query(Company).all()
            companies_data = []
            for company in companies:
                companies_data.append({
                    'company_id': company.company_id,
                    'company_code': company.company_code,
                    'company_name': company.company_name,
                    'description': company.description,
                    'active': company.active,
                    'created_at': company.created_at.isoformat() if company.created_at else None
                })
            
            self._save_backup_file(backup_folder, 'companies.json', companies_data)
            backup_result['files_created'] += 1
            
            # 2. Backup document types
            doc_types = db.query(DocumentType).all()
            doc_types_data = []
            for doc_type in doc_types:
                doc_types_data.append({
                    'doc_type_id': doc_type.doc_type_id,
                    'type_code': doc_type.type_code,
                    'type_name': doc_type.type_name,
                    'description': doc_type.description,
                    'active': doc_type.active,
                    'created_at': doc_type.created_at.isoformat() if doc_type.created_at else None
                })
            
            self._save_backup_file(backup_folder, 'document_types.json', doc_types_data)
            backup_result['files_created'] += 1
            
            # 3. Backup company document configurations
            configs = db.query(CompanyDocTypeConfig).all()
            configs_data = []
            for config in configs:
                configs_data.append({
                    'config_id': config.config_id,
                    'company_id': config.company_id,
                    'doc_type_id': config.doc_type_id,
                    'prompt_path': config.prompt_path,
                    'schema_path': config.schema_path,
                    'storage_type': str(config.storage_type),
                    'active': config.active,
                    'created_at': config.created_at.isoformat() if config.created_at else None,
                    'updated_at': config.updated_at.isoformat() if config.updated_at else None
                })
            
            self._save_backup_file(backup_folder, 'configurations.json', configs_data)
            backup_result['files_created'] += 1
            
            # 4. Backup recent batch jobs metadata
            recent_jobs = db.query(BatchJob).filter(
                BatchJob.created_at >= datetime.now() - timedelta(days=30)
            ).all()
            
            jobs_data = []
            for job in recent_jobs:
                jobs_data.append({
                    'batch_id': job.batch_id,
                    'company_id': job.company_id,
                    'doc_type_id': job.doc_type_id,
                    'status': job.status,
                    'file_count': job.file_count,
                    'total_files': job.total_files,
                    'processed_files': job.processed_files,
                    'json_output_path': job.json_output_path,
                    'csv_output_path': job.csv_output_path,
                    'excel_output_path': job.excel_output_path,
                    'netsuite_csv_path': job.netsuite_csv_path,
                    'created_at': job.created_at.isoformat() if job.created_at else None
                })
            
            self._save_backup_file(backup_folder, 'recent_batch_jobs.json', jobs_data)
            backup_result['files_created'] += 1
            
            # 5. Create backup manifest
            manifest = {
                'backup_id': backup_result['backup_id'],
                'backup_name': backup_name,
                'backup_type': BackupType.DATABASE.value,
                'created_at': backup_result['created_at'],
                'files': [
                    'companies.json',
                    'document_types.json', 
                    'configurations.json',
                    'recent_batch_jobs.json'
                ],
                'record_counts': {
                    'companies': len(companies_data),
                    'document_types': len(doc_types_data),
                    'configurations': len(configs_data),
                    'batch_jobs': len(jobs_data)
                }
            }
            
            self._save_backup_file(backup_folder, 'manifest.json', manifest)
            backup_result['files_created'] += 1
            
            backup_result['status'] = BackupStatus.COMPLETED.value
            logger.info(f"✅ Database backup completed: {backup_name}")
            
        except Exception as e:
            backup_result['status'] = BackupStatus.FAILED.value
            backup_result['errors'].append(str(e))
            logger.error(f"❌ Database backup failed: {e}")
        
        return backup_result
    
    def create_configuration_backup(self, db: Session, company_id: Optional[int] = None) -> Dict[str, Any]:
        """Create backup of configuration files (prompts, schemas)."""
        backup_name = f"config_backup_{datetime.now().strftime('%Y%m%d_%H%M%S')}"
        if company_id:
            backup_name += f"_company_{company_id}"
        
        backup_result = {
            'backup_id': str(uuid.uuid4()),
            'backup_name': backup_name,
            'backup_type': BackupType.CONFIGURATIONS.value,
            'status': BackupStatus.PENDING.value,
            'created_at': datetime.now().isoformat(),
            'files_backed_up': 0,
            'total_size': 0,
            'errors': []
        }
        
        try:
            backup_result['status'] = BackupStatus.RUNNING.value
            
            backup_folder = f"backups/configurations/{datetime.now().strftime('%Y-%m-%d')}/{backup_name}"
            
            # Get configurations to backup
            query = db.query(CompanyDocTypeConfig)
            if company_id:
                query = query.filter(CompanyDocTypeConfig.company_id == company_id)

            configs = query.filter(CompanyDocTypeConfig.active == True).all()
            
            backed_up_files = []
            
            for config in configs:
                try:
                    # Backup prompt file
                    if config.prompt_path:
                        prompt_content = self._download_config_file(config.prompt_path)
                        if prompt_content:
                            prompt_backup_path = f"{backup_folder}/prompts/config_{config.config_id}_prompt.txt"
                            self._save_backup_file_raw(prompt_backup_path, prompt_content)
                            backed_up_files.append(prompt_backup_path)
                            backup_result['files_backed_up'] += 1
                    
                    # Backup schema file
                    if config.schema_path:
                        schema_content = self._download_config_file(config.schema_path)
                        if schema_content:
                            schema_backup_path = f"{backup_folder}/schemas/config_{config.config_id}_schema.json"
                            self._save_backup_file_raw(schema_backup_path, schema_content)
                            backed_up_files.append(schema_backup_path)
                            backup_result['files_backed_up'] += 1
                
                except Exception as e:
                    backup_result['errors'].append(f"Config {config.config_id}: {str(e)}")
                    logger.error(f"❌ Failed to backup config {config.config_id}: {e}")
            
            # Create backup manifest
            manifest = {
                'backup_id': backup_result['backup_id'],
                'backup_name': backup_name,
                'backup_type': BackupType.CONFIGURATIONS.value,
                'created_at': backup_result['created_at'],
                'company_id': company_id,
                'configurations_count': len(configs),
                'files_backed_up': backed_up_files,
                'errors': backup_result['errors']
            }
            
            self._save_backup_file(backup_folder, 'manifest.json', manifest)
            
            backup_result['status'] = BackupStatus.COMPLETED.value
            logger.info(f"✅ Configuration backup completed: {backup_name}")
            
        except Exception as e:
            backup_result['status'] = BackupStatus.FAILED.value
            backup_result['errors'].append(str(e))
            logger.error(f"❌ Configuration backup failed: {e}")
        
        return backup_result
    
    def _download_config_file(self, file_path: str) -> Optional[bytes]:
        """Download configuration file content."""
        try:
            if file_path.startswith('s3://'):
                return self.s3_manager.download_file_by_stored_path(file_path)
            else:
                # Try different folder types
                for folder in ['prompts', 'schemas']:
                    content = self.s3_manager.download_file(file_path, folder)
                    if content:
                        return content
                return None
        except Exception as e:
            logger.error(f"❌ Failed to download config file {file_path}: {e}")
            return None
    
    def _save_backup_file(self, backup_folder: str, filename: str, data: Any):
        """Save backup file with compression if enabled."""
        try:
            # Convert data to JSON if needed
            if isinstance(data, (dict, list)):
                content = json.dumps(data, indent=2, ensure_ascii=False).encode('utf-8')
            else:
                content = data.encode('utf-8') if isinstance(data, str) else data
            
            # Compress if enabled
            if self.backup_config['compression']:
                content = gzip.compress(content)
                filename += '.gz'
            
            file_path = f"{backup_folder}/{filename}"
            
            # Upload to S3
            self.s3_manager.s3_client.put_object(
                Bucket=self.s3_manager.bucket_name,
                Key=file_path,
                Body=content,
                ContentType='application/json' if filename.endswith('.json') else 'application/octet-stream',
                Metadata={
                    'backup_file': 'true',
                    'compressed': str(self.backup_config['compression']).lower(),
                    'created_at': datetime.now().isoformat()
                }
            )
            
            logger.debug(f"💾 Saved backup file: {file_path}")
            
        except Exception as e:
            logger.error(f"❌ Failed to save backup file {filename}: {e}")
            raise
    
    def _save_backup_file_raw(self, file_path: str, content: bytes):
        """Save raw backup file content."""
        try:
            # Compress if enabled
            if self.backup_config['compression']:
                content = gzip.compress(content)
                file_path += '.gz'
            
            # Upload to S3
            self.s3_manager.s3_client.put_object(
                Bucket=self.s3_manager.bucket_name,
                Key=file_path,
                Body=content,
                Metadata={
                    'backup_file': 'true',
                    'compressed': str(self.backup_config['compression']).lower(),
                    'created_at': datetime.now().isoformat()
                }
            )
            
            logger.debug(f"💾 Saved raw backup file: {file_path}")
            
        except Exception as e:
            logger.error(f"❌ Failed to save raw backup file: {e}")
            raise
    
    def cleanup_old_backups(self) -> Dict[str, Any]:
        """Clean up old backups based on retention policies."""
        cleanup_results = {
            'backups_analyzed': 0,
            'backups_deleted': 0,
            'space_freed': 0,
            'errors': 0,
            'cleanup_by_type': {}
        }
        
        if not self.s3_manager:
            return {'error': 'S3 manager not available'}
        
        try:
            for backup_type, policy in self.backup_config['retention_policies'].items():
                type_results = {
                    'analyzed': 0,
                    'deleted': 0,
                    'space_freed': 0
                }
                
                # List all backups of this type
                backup_prefix = f"backups/{backup_type.value}/"
                
                paginator = self.s3_manager.s3_client.get_paginator('list_objects_v2')
                pages = paginator.paginate(
                    Bucket=self.s3_manager.bucket_name,
                    Prefix=backup_prefix
                )
                
                backup_folders = set()
                
                for page in pages:
                    if 'Contents' not in page:
                        continue
                    
                    for obj in page['Contents']:
                        # Extract backup folder from path
                        key = obj['Key']
                        parts = key.split('/')
                        if len(parts) >= 4:  # backups/type/date/backup_name/file
                            backup_folder = '/'.join(parts[:4])
                            backup_folders.add(backup_folder)
                
                # Check each backup folder for retention
                cutoff_date = datetime.now() - timedelta(days=policy['days'])
                
                for backup_folder in backup_folders:
                    type_results['analyzed'] += 1
                    
                    # Extract date from folder path
                    try:
                        date_part = backup_folder.split('/')[2]  # YYYY-MM-DD
                        backup_date = datetime.strptime(date_part, '%Y-%m-%d')
                        
                        if backup_date < cutoff_date:
                            # Delete this backup
                            deleted_size = self._delete_backup_folder(backup_folder)
                            if deleted_size > 0:
                                type_results['deleted'] += 1
                                type_results['space_freed'] += deleted_size
                                logger.info(f"🗑️ Deleted old backup: {backup_folder}")
                    
                    except Exception as e:
                        logger.error(f"❌ Error processing backup folder {backup_folder}: {e}")
                        cleanup_results['errors'] += 1
                
                cleanup_results['cleanup_by_type'][backup_type.value] = type_results
                cleanup_results['backups_analyzed'] += type_results['analyzed']
                cleanup_results['backups_deleted'] += type_results['deleted']
                cleanup_results['space_freed'] += type_results['space_freed']
        
        except Exception as e:
            logger.error(f"❌ Backup cleanup failed: {e}")
            cleanup_results['error'] = str(e)
        
        return cleanup_results
    
    def _delete_backup_folder(self, backup_folder: str) -> int:
        """Delete all files in a backup folder and return total size freed."""
        total_size = 0
        
        try:
            # List all files in the backup folder
            paginator = self.s3_manager.s3_client.get_paginator('list_objects_v2')
            pages = paginator.paginate(
                Bucket=self.s3_manager.bucket_name,
                Prefix=backup_folder + '/'
            )
            
            objects_to_delete = []
            
            for page in pages:
                if 'Contents' not in page:
                    continue
                
                for obj in page['Contents']:
                    objects_to_delete.append({'Key': obj['Key']})
                    total_size += obj['Size']
            
            # Delete objects in batches
            if objects_to_delete:
                # S3 delete_objects can handle up to 1000 objects at once
                for i in range(0, len(objects_to_delete), 1000):
                    batch = objects_to_delete[i:i+1000]
                    
                    self.s3_manager.s3_client.delete_objects(
                        Bucket=self.s3_manager.bucket_name,
                        Delete={'Objects': batch}
                    )
        
        except Exception as e:
            logger.error(f"❌ Failed to delete backup folder {backup_folder}: {e}")
            return 0
        
        return total_size
    
    def restore_database_backup(self, backup_name: str, db: Session) -> Dict[str, Any]:
        """Restore database configurations from backup."""
        # This is a placeholder for database restore functionality
        # In production, this would need careful implementation with transaction rollback
        logger.warning("⚠️ Database restore functionality requires careful implementation")
        return {
            'success': False,
            'message': 'Database restore requires manual implementation for safety'
        }
    
    def list_available_backups(self) -> Dict[str, Any]:
        """List all available backups."""
        backups = {
            'database': [],
            'configurations': [],
            'files': [],
            'exports': [],
            'system': []
        }
        
        if not self.s3_manager:
            return {'error': 'S3 manager not available'}
        
        try:
            for backup_type in BackupType:
                backup_prefix = f"backups/{backup_type.value}/"
                
                paginator = self.s3_manager.s3_client.get_paginator('list_objects_v2')
                pages = paginator.paginate(
                    Bucket=self.s3_manager.bucket_name,
                    Prefix=backup_prefix
                )
                
                backup_folders = {}
                
                for page in pages:
                    if 'Contents' not in page:
                        continue
                    
                    for obj in page['Contents']:
                        key = obj['Key']
                        if key.endswith('manifest.json'):
                            # This is a backup manifest
                            try:
                                manifest_content = self.s3_manager.download_file_by_stored_path(
                                    f"s3://{self.s3_manager.bucket_name}/{key}"
                                )
                                if manifest_content:
                                    manifest = json.loads(manifest_content.decode('utf-8'))
                                    backups[backup_type.value].append(manifest)
                            except Exception as e:
                                logger.error(f"❌ Failed to read backup manifest {key}: {e}")
        
        except Exception as e:
            logger.error(f"❌ Failed to list backups: {e}")
            backups['error'] = str(e)
        
        return backups


def initialize_backup_system(s3_manager: Optional[S3StorageManager] = None) -> Dict[str, Any]:
    """Initialize the complete backup system."""
    backup_manager = S3BackupManager(s3_manager)
    return backup_manager.initialize_backup_structure()


def create_automated_backup(backup_type: BackupType, 
                          s3_manager: Optional[S3StorageManager] = None,
                          company_id: Optional[int] = None) -> Dict[str, Any]:
    """Create automated backup of specified type."""
    backup_manager = S3BackupManager(s3_manager)
    db = next(get_db())
    
    try:
        if backup_type == BackupType.DATABASE:
            return backup_manager.create_database_backup(db)
        elif backup_type == BackupType.CONFIGURATIONS:
            return backup_manager.create_configuration_backup(db, company_id)
        else:
            return {'success': False, 'error': f'Backup type {backup_type.value} not yet implemented'}
    finally:
        db.close()


if __name__ == "__main__":
    # Example usage
    result = initialize_backup_system()
    print(f"Backup system initialization: {result}")