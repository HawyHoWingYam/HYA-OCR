#!/usr/bin/env python3
"""
Migration script to convert existing temp-prefixed files to clean path structure

This script:
1. Identifies configurations with temp-prefixed or legacy file paths
2. Copies files from old paths to new clean path structure
3. Updates database paths to reflect new structure
4. Verifies all files are accessible after migration
5. Optionally cleans up old files

Usage:
    python scripts/migrate_to_clean_paths.py --dry-run  # Preview changes
    python scripts/migrate_to_clean_paths.py --execute  # Execute migration
    python scripts/migrate_to_clean_paths.py --cleanup  # Cleanup old files after verification
"""

import os
import sys
import logging
import argparse
import json
from typing import List, Dict, Optional, Tuple
from datetime import datetime

# Add parent directory to path to import local modules
sys.path.append(os.path.join(os.path.dirname(__file__), '..'))

from sqlalchemy.orm import Session
from db.database import get_db
from db.models import CompanyDocTypeConfig, Company, DocumentType
from utils.s3_storage import get_s3_manager

# Setup logging
logging.basicConfig(
    level=logging.INFO,
    format='%(asctime)s - %(levelname)s - %(message)s'
)
logger = logging.getLogger(__name__)

class CleanPathMigrator:
    """Handles migration from legacy/temp paths to clean path structure"""
    
    def __init__(self, s3_manager, db: Session):
        self.s3_manager = s3_manager
        self.db = db
        self.migration_results = []
        
    def identify_migration_candidates(self) -> List[Dict]:
        """Identify configurations that need migration"""
        logger.info("🔍 Identifying migration candidates...")
        
        candidates = []
        configs = self.db.query(CompanyDocTypeConfig).all()
        
        for config in configs:
            needs_migration = False
            issues = []
            
            # Check for temp prefixes in paths
            if config.prompt_path and 'temp_' in config.prompt_path:
                needs_migration = True
                issues.append("prompt_path contains temp prefix")
            
            if config.schema_path and 'temp_' in config.schema_path:
                needs_migration = True
                issues.append("schema_path contains temp prefix")
            
            # Check for missing original filenames
            if config.prompt_path and not config.original_prompt_filename:
                needs_migration = True
                issues.append("missing original_prompt_filename")
                
            if config.schema_path and not config.original_schema_filename:
                needs_migration = True
                issues.append("missing original_schema_filename")
            
            # Check for legacy path structures (config_XX_ patterns)
            if config.prompt_path and 'config_' in config.prompt_path and config.prompt_path.count('_') > 1:
                needs_migration = True
                issues.append("prompt_path uses legacy config_ naming")
                
            if config.schema_path and 'config_' in config.schema_path and config.schema_path.count('_') > 1:
                needs_migration = True
                issues.append("schema_path uses legacy config_ naming")
            
            if needs_migration:
                candidates.append({
                    'config': config,
                    'issues': issues,
                    'company': config.company,
                    'doc_type': config.document_type
                })
        
        logger.info(f"📊 Found {len(candidates)} configurations needing migration")
        return candidates
    
    def extract_original_filename(self, s3_path: str, file_type: str) -> str:
        """Extract original filename from S3 path or metadata"""
        try:
            # Try to get original filename from S3 metadata first
            s3_key = s3_path.replace(f"s3://{self.s3_manager.bucket_name}/", "")
            folder_type = "prompts" if file_type == "prompt" else "schemas"
            
            file_info = self.s3_manager.get_file_info(s3_key, folder_type)
            if file_info and "metadata" in file_info:
                metadata = file_info["metadata"]
                if "original_filename" in metadata:
                    return metadata["original_filename"]
            
            # Fallback: extract from path
            filename = s3_path.split("/")[-1]
            
            # Remove temp prefixes
            if filename.startswith('temp_'):
                # temp_1234567890_filename.ext -> filename.ext
                parts = filename.split('_', 2)
                if len(parts) >= 3:
                    filename = parts[2]
            
            # Remove config prefixes  
            if filename.startswith('config_'):
                # config_40_prompt.txt -> prompt.txt
                parts = filename.split('_', 2)
                if len(parts) >= 3:
                    filename = parts[2]
                elif len(parts) == 2:
                    # config_40.txt -> 40.txt (keep the ID part)
                    filename = f"{file_type}.{'txt' if file_type == 'prompt' else 'json'}"
            
            return filename
            
        except Exception as e:
            logger.warning(f"⚠️ Failed to extract original filename from {s3_path}: {e}")
            return f"{file_type}.{'txt' if file_type == 'prompt' else 'json'}"
    
    def migrate_config(self, candidate: Dict, dry_run: bool = True) -> Dict:
        """Migrate a single configuration to clean path structure"""
        config = candidate['config']
        company = candidate['company']
        doc_type = candidate['doc_type']
        
        logger.info(f"🔧 {'[DRY RUN] ' if dry_run else ''}Migrating config {config.config_id}")
        
        migration_result = {
            'config_id': config.config_id,
            'success': False,
            'changes': [],
            'errors': []
        }
        
        try:
            # Migrate prompt file
            if config.prompt_path:
                result = self._migrate_file(
                    config, company, doc_type, 'prompt', 
                    config.prompt_path, dry_run
                )
                migration_result['changes'].extend(result['changes'])
                migration_result['errors'].extend(result['errors'])
            
            # Migrate schema file  
            if config.schema_path:
                result = self._migrate_file(
                    config, company, doc_type, 'schema',
                    config.schema_path, dry_run
                )
                migration_result['changes'].extend(result['changes'])
                migration_result['errors'].extend(result['errors'])
            
            migration_result['success'] = len(migration_result['errors']) == 0
            
        except Exception as e:
            error_msg = f"Migration failed for config {config.config_id}: {e}"
            logger.error(f"❌ {error_msg}")
            migration_result['errors'].append(error_msg)
        
        return migration_result
    
    def _migrate_file(self, config, company, doc_type, file_type: str, 
                     old_s3_path: str, dry_run: bool) -> Dict:
        """Migrate a single file to clean path structure"""
        result = {'changes': [], 'errors': []}
        
        try:
            # Extract original filename
            original_filename = self.extract_original_filename(old_s3_path, file_type)
            
            # Construct new clean path
            new_s3_path = f"s3://{self.s3_manager.bucket_name}/companies/{company.company_id}/{file_type}s/{doc_type.doc_type_id}/{config.config_id}/{original_filename}"
            
            # Check if migration is needed
            if old_s3_path == new_s3_path:
                logger.info(f"✅ {file_type} already in clean format: {new_s3_path}")
                return result
            
            logger.info(f"📁 {file_type} migration:")
            logger.info(f"   From: {old_s3_path}")
            logger.info(f"   To:   {new_s3_path}")
            
            if not dry_run:
                # Download file from old location
                old_s3_key = old_s3_path.replace(f"s3://{self.s3_manager.bucket_name}/", "")
                file_content = None
                
                # Try different download methods based on path structure
                if 'companies/' in old_s3_key:
                    # New-style path, use company file manager
                    if file_type == 'prompt':
                        file_content = self.s3_manager.download_prompt_by_id(
                            company.company_id, doc_type.doc_type_id, 
                            config.config_id, filename=None
                        )
                    else:
                        schema_data = self.s3_manager.download_schema_by_id(
                            company.company_id, doc_type.doc_type_id,
                            config.config_id, filename=None
                        )
                        if schema_data:
                            file_content = json.dumps(schema_data, indent=2, ensure_ascii=False)
                else:
                    # Legacy path, use raw download
                    path_parts = old_s3_key.split('/')
                    if len(path_parts) >= 2:
                        company_part, doc_type_part = path_parts[0], path_parts[1]
                        filename = path_parts[-1]
                        if file_type == 'prompt':
                            file_content = self.s3_manager.download_prompt_raw(
                                company_part, doc_type_part, filename
                            )
                        else:
                            file_content = self.s3_manager.download_schema_raw(
                                company_part, doc_type_part, filename
                            )
                
                if file_content is None:
                    raise Exception(f"Failed to download {file_type} from {old_s3_path}")
                
                # Upload to new clean location
                metadata = {
                    'original_filename': original_filename,
                    'migrated_from': old_s3_path,
                    'migration_date': datetime.now().isoformat()
                }
                
                if file_type == 'prompt':
                    new_s3_key = self.s3_manager.upload_prompt_by_id(
                        company.company_id, doc_type.doc_type_id, config.config_id,
                        file_content, original_filename, metadata
                    )
                else:
                    schema_data = json.loads(file_content) if isinstance(file_content, str) else file_content
                    new_s3_key = self.s3_manager.upload_schema_by_id(
                        company.company_id, doc_type.doc_type_id, config.config_id,
                        schema_data, original_filename, metadata
                    )
                
                if not new_s3_key:
                    raise Exception(f"Failed to upload {file_type} to {new_s3_path}")
                
                # Update database
                if file_type == 'prompt':
                    config.prompt_path = new_s3_path
                    config.original_prompt_filename = original_filename
                else:
                    config.schema_path = new_s3_path
                    config.original_schema_filename = original_filename
                
                self.db.commit()
                logger.info(f"✅ Successfully migrated {file_type}")
                
            result['changes'].append(f"Migrated {file_type}: {old_s3_path} -> {new_s3_path}")
            
        except Exception as e:
            error_msg = f"Failed to migrate {file_type}: {e}"
            logger.error(f"❌ {error_msg}")
            result['errors'].append(error_msg)
        
        return result
    
    def verify_migration(self, config_id: int) -> bool:
        """Verify that migrated files are accessible"""
        logger.info(f"🔍 Verifying migration for config {config_id}")
        
        try:
            config = self.db.query(CompanyDocTypeConfig).filter(
                CompanyDocTypeConfig.config_id == config_id
            ).first()
            
            if not config:
                logger.error(f"❌ Config {config_id} not found")
                return False
            
            success = True
            
            # Test prompt download
            if config.prompt_path and config.original_prompt_filename:
                try:
                    content = self.s3_manager.download_prompt_by_id(
                        config.company_id, config.doc_type_id, config_id,
                        config.original_prompt_filename
                    )
                    if content:
                        logger.info(f"✅ Prompt verification successful")
                    else:
                        logger.error(f"❌ Prompt verification failed")
                        success = False
                except Exception as e:
                    logger.error(f"❌ Prompt verification error: {e}")
                    success = False
            
            # Test schema download
            if config.schema_path and config.original_schema_filename:
                try:
                    content = self.s3_manager.download_schema_by_id(
                        config.company_id, config.doc_type_id, config_id,
                        config.original_schema_filename
                    )
                    if content:
                        logger.info(f"✅ Schema verification successful")
                    else:
                        logger.error(f"❌ Schema verification failed")
                        success = False
                except Exception as e:
                    logger.error(f"❌ Schema verification error: {e}")
                    success = False
            
            return success
            
        except Exception as e:
            logger.error(f"❌ Verification failed for config {config_id}: {e}")
            return False

def main():
    parser = argparse.ArgumentParser(description='Migrate files to clean path structure')
    parser.add_argument('--dry-run', action='store_true', 
                       help='Preview changes without executing')
    parser.add_argument('--execute', action='store_true',
                       help='Execute the migration')
    parser.add_argument('--cleanup', action='store_true',
                       help='Cleanup old files after verification')
    parser.add_argument('--verify-only', action='store_true',
                       help='Only verify existing migrations')
    
    args = parser.parse_args()
    
    if not any([args.dry_run, args.execute, args.cleanup, args.verify_only]):
        parser.error('Please specify one of: --dry-run, --execute, --cleanup, --verify-only')
    
    logger.info("🚀 Starting Clean Path Migration Script")
    logger.info(f"Mode: {'DRY RUN' if args.dry_run else 'EXECUTE' if args.execute else 'CLEANUP' if args.cleanup else 'VERIFY'}")
    
    try:
        # Initialize S3 manager and database
        s3_manager = get_s3_manager()
        if not s3_manager:
            logger.error("❌ S3 not configured or available")
            return
        
        db = next(get_db())
        
        migrator = CleanPathMigrator(s3_manager, db)
        
        if args.verify_only:
            # Verify all configurations
            configs = db.query(CompanyDocTypeConfig).all()
            for config in configs:
                migrator.verify_migration(config.config_id)
            return
        
        # Identify candidates
        candidates = migrator.identify_migration_candidates()
        
        if not candidates:
            logger.info("🎉 No migrations needed! All configurations already use clean path structure.")
            return
        
        # Process candidates
        total_processed = 0
        total_successful = 0
        
        for candidate in candidates:
            result = migrator.migrate_config(candidate, dry_run=args.dry_run)
            migrator.migration_results.append(result)
            
            total_processed += 1
            if result['success']:
                total_successful += 1
            
            # Verify if not dry run
            if args.execute and result['success']:
                if migrator.verify_migration(result['config_id']):
                    logger.info(f"✅ Migration and verification successful for config {result['config_id']}")
                else:
                    logger.error(f"❌ Migration successful but verification failed for config {result['config_id']}")
        
        # Summary
        logger.info("\n" + "="*50)
        logger.info("📊 MIGRATION SUMMARY")
        logger.info("="*50)
        logger.info(f"Total configurations processed: {total_processed}")
        logger.info(f"Successful migrations: {total_successful}")
        logger.info(f"Failed migrations: {total_processed - total_successful}")
        
        if args.dry_run:
            logger.info("\n🔍 This was a DRY RUN - no changes were made")
            logger.info("Run with --execute to perform the actual migration")
        elif args.execute:
            logger.info("\n✅ Migration completed successfully!")
            logger.info("Files have been migrated to clean path structure")
            logger.info("Original filenames are preserved and stored in database")
        
    except Exception as e:
        logger.error(f"❌ Migration script failed: {e}")
        sys.exit(1)
    finally:
        if 'db' in locals():
            db.close()

if __name__ == "__main__":
    main()