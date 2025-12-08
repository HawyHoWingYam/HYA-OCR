#!/usr/bin/env python3
"""
S3 Structure Migration Script

This script migrates existing S3 files from the old name-based structure to the new ID-based structure.

OLD STRUCTURE:
├── prompts/
│   ├── {company_name}/{doc_type_name}/file.txt
│   └── {company_code}/{doc_type_code}/file.txt
├── schemas/
│   ├── {company_name}/{doc_type_name}/file.json
│   └── {company_code}/{doc_type_code}/file.json

NEW STRUCTURE:
├── companies/
│   ├── {company_id}/
│   │   ├── prompts/
│   │   │   └── {doc_type_id}/
│   │   │       └── {config_id}_{filename}
│   │   ├── schemas/
│   │   │   └── {doc_type_id}/
│   │   │       └── {config_id}_{filename}
│   │   ├── uploads/
│   │   │   └── {job_id}_{filename}
│   │   ├── results/
│   │   │   └── {job_id}_{filename}
│   │   └── exports/
│   │       └── {export_id}_{filename}
"""

import os
import sys
import json
import logging
import argparse
from typing import Dict, List, Optional, Any
from datetime import datetime
import boto3
from botocore.exceptions import ClientError

# Add backend directory to Python path
sys.path.append(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from db.database import SessionLocal
from db.models import Company, DocumentType, CompanyDocTypeConfig
from utils.company_file_manager import CompanyFileManager, FileType
from config_loader import config_loader

# Set up logging
logging.basicConfig(
    level=logging.INFO,
    format='%(asctime)s - %(name)s - %(levelname)s - %(message)s'
)
logger = logging.getLogger(__name__)


class S3MigrationManager:
    """Manages migration of S3 files from name-based to ID-based structure"""
    
    def __init__(self, dry_run: bool = True):
        """
        Initialize migration manager
        
        Args:
            dry_run: If True, only simulate migration without making changes
        """
        self.dry_run = dry_run
        self.company_file_manager = CompanyFileManager()
        
        # Initialize S3 client
        self.s3_client = boto3.client('s3')
        self.bucket_name = config_loader.get_aws_config().get('s3_bucket_name', 'hya-ocr-sandbox')
        
        # Migration statistics
        self.stats = {
            'files_scanned': 0,
            'files_migrated': 0,
            'files_skipped': 0,
            'files_failed': 0,
            'errors': []
        }
        
        # Database session
        self.db_session = SessionLocal()
        
        # Load mappings from database
        self._load_database_mappings()
    
    def _load_database_mappings(self):
        """Load company and document type mappings from database"""
        logger.info("Loading database mappings...")
        
        # Load companies
        self.companies = {}  # code -> company_id mapping
        self.company_names = {}  # name -> company_id mapping 
        for company in self.db_session.query(Company).all():
            self.companies[company.company_code] = company.company_id
            self.company_names[company.company_name.lower()] = company.company_id
        
        # Load document types
        self.doc_types = {}  # code -> doc_type_id mapping
        self.doc_type_names = {}  # name -> doc_type_id mapping
        for doc_type in self.db_session.query(DocumentType).all():
            self.doc_types[doc_type.type_code] = doc_type.doc_type_id
            self.doc_type_names[doc_type.type_name.lower()] = doc_type.doc_type_id
        
        # Load configurations for config_id mapping
        self.configs = {}  # (company_id, doc_type_id) -> config_id mapping
        for config in self.db_session.query(CompanyDocTypeConfig).all():
            key = (config.company_id, config.doc_type_id)
            self.configs[key] = config.config_id
            
        logger.info(f"Loaded {len(self.companies)} companies, {len(self.doc_types)} document types, {len(self.configs)} configurations")
    
    def _resolve_company_id(self, company_identifier: str) -> Optional[int]:
        """
        Resolve company identifier to company_id
        
        Args:
            company_identifier: Company code, name, or folder name
            
        Returns:
            Optional[int]: Company ID if found
        """
        # Try exact code match first
        if company_identifier in self.companies:
            return self.companies[company_identifier]
        
        # Try name match (case insensitive)
        name_lower = company_identifier.lower()
        if name_lower in self.company_names:
            return self.company_names[name_lower]
        
        # Try partial matches for common variations
        for code, company_id in self.companies.items():
            if code.lower() == name_lower or name_lower in code.lower():
                return company_id
        
        return None
    
    def _resolve_doc_type_id(self, doc_type_identifier: str) -> Optional[int]:
        """
        Resolve document type identifier to doc_type_id
        
        Args:
            doc_type_identifier: Doc type code, name, or folder name
            
        Returns:
            Optional[int]: Document type ID if found
        """
        # Try exact code match first
        if doc_type_identifier in self.doc_types:
            return self.doc_types[doc_type_identifier]
        
        # Try name match (case insensitive)
        name_lower = doc_type_identifier.lower()
        if name_lower in self.doc_type_names:
            return self.doc_type_names[name_lower]
        
        # Try partial matches for common variations
        for code, doc_type_id in self.doc_types.items():
            if code.lower() == name_lower or name_lower in code.lower():
                return doc_type_id
        
        return None
    
    def _get_config_id(self, company_id: int, doc_type_id: int) -> Optional[int]:
        """
        Get configuration ID for company and document type
        
        Args:
            company_id: Company ID
            doc_type_id: Document type ID
            
        Returns:
            Optional[int]: Configuration ID if exists
        """
        key = (company_id, doc_type_id)
        return self.configs.get(key)
    
    def _list_legacy_files(self, folder_type: str) -> List[Dict[str, Any]]:
        """
        List all files in legacy folder structure
        
        Args:
            folder_type: "prompts" or "schemas"
            
        Returns:
            List of file objects with metadata
        """
        logger.info(f"Scanning legacy {folder_type} folder...")
        
        files = []
        try:
            paginator = self.s3_client.get_paginator('list_objects_v2')
            page_iterator = paginator.paginate(
                Bucket=self.bucket_name,
                Prefix=f"{folder_type}/"
            )
            
            for page in page_iterator:
                if 'Contents' in page:
                    for obj in page['Contents']:
                        # Skip folder markers
                        if obj['Key'].endswith('/'):
                            continue
                            
                        files.append({
                            'key': obj['Key'],
                            'size': obj['Size'],
                            'last_modified': obj['LastModified'],
                            'etag': obj['ETag']
                        })
                        
        except ClientError as e:
            logger.error(f"Error listing {folder_type}: {e}")
            
        logger.info(f"Found {len(files)} files in legacy {folder_type} folder")
        return files
    
    def _parse_legacy_path(self, file_key: str) -> Optional[Dict[str, Any]]:
        """
        Parse legacy file path to extract company and doc_type identifiers
        
        Args:
            file_key: S3 object key (e.g., "prompts/hana/admin_billing/file.txt")
            
        Returns:
            Dictionary with parsed information or None if invalid
        """
        parts = file_key.split('/')
        
        if len(parts) < 3:
            logger.warning(f"Invalid path format: {file_key}")
            return None
        
        folder_type = parts[0]  # "prompts" or "schemas"
        company_identifier = parts[1]
        doc_type_identifier = parts[2]
        filename = parts[-1] if len(parts) > 3 else parts[2]
        
        # If there are more than 3 parts, the filename might be nested
        if len(parts) > 3:
            # Handle nested paths like "prompts/hana/admin_billing/nested/file.txt"
            doc_type_identifier = '/'.join(parts[2:-1])
            filename = parts[-1]
        
        return {
            'folder_type': folder_type,
            'company_identifier': company_identifier,
            'doc_type_identifier': doc_type_identifier,
            'filename': filename,
            'original_key': file_key
        }
    
    def _generate_new_path(self, parsed_info: Dict[str, Any]) -> Optional[str]:
        """
        Generate new ID-based path for a file
        
        Args:
            parsed_info: Parsed legacy path information
            
        Returns:
            New S3 key path or None if cannot be mapped
        """
        company_id = self._resolve_company_id(parsed_info['company_identifier'])
        if not company_id:
            logger.warning(f"Cannot resolve company: {parsed_info['company_identifier']}")
            return None
        
        doc_type_id = self._resolve_doc_type_id(parsed_info['doc_type_identifier'])
        if not doc_type_id:
            logger.warning(f"Cannot resolve doc type: {parsed_info['doc_type_identifier']}")
            return None
        
        config_id = self._get_config_id(company_id, doc_type_id)
        if not config_id:
            logger.warning(f"No configuration found for company_id={company_id}, doc_type_id={doc_type_id}")
            # Use a default config_id of 1 for migration
            config_id = 1
        
        # Determine file type
        file_type = FileType.PROMPT if parsed_info['folder_type'] == 'prompts' else FileType.SCHEMA
        
        # Generate new path using CompanyFileManager
        try:
            new_path = self.company_file_manager.get_company_file_path(
                company_id=company_id,
                file_type=file_type,
                filename=f"{config_id}_{parsed_info['filename']}",
                doc_type_id=doc_type_id,
                config_id=config_id
            )
            return new_path
        except Exception as e:
            logger.error(f"Error generating new path: {e}")
            return None
    
    def _copy_file(self, source_key: str, dest_key: str) -> bool:
        """
        Copy file from source to destination in S3
        
        Args:
            source_key: Source S3 key
            dest_key: Destination S3 key
            
        Returns:
            True if successful, False otherwise
        """
        if self.dry_run:
            logger.info(f"[DRY RUN] Would copy: {source_key} -> {dest_key}")
            return True
        
        try:
            copy_source = {
                'Bucket': self.bucket_name,
                'Key': source_key
            }
            
            self.s3_client.copy_object(
                CopySource=copy_source,
                Bucket=self.bucket_name,
                Key=dest_key
            )
            
            logger.info(f"✅ Copied: {source_key} -> {dest_key}")
            return True
            
        except ClientError as e:
            logger.error(f"❌ Copy failed {source_key} -> {dest_key}: {e}")
            return False
    
    def _file_exists(self, key: str) -> bool:
        """
        Check if file exists in S3
        
        Args:
            key: S3 object key
            
        Returns:
            True if file exists
        """
        try:
            self.s3_client.head_object(Bucket=self.bucket_name, Key=key)
            return True
        except ClientError:
            return False
    
    def migrate_folder(self, folder_type: str) -> Dict[str, int]:
        """
        Migrate all files in a specific folder type
        
        Args:
            folder_type: "prompts" or "schemas"
            
        Returns:
            Migration statistics for this folder
        """
        logger.info(f"🚀 Starting migration of {folder_type} folder...")
        
        folder_stats = {
            'scanned': 0,
            'migrated': 0,
            'skipped': 0,
            'failed': 0
        }
        
        # List all legacy files
        legacy_files = self._list_legacy_files(folder_type)
        
        for file_info in legacy_files:
            folder_stats['scanned'] += 1
            self.stats['files_scanned'] += 1
            
            file_key = file_info['key']
            
            # Skip if already in new structure (starts with "companies/")
            if file_key.startswith('companies/'):
                logger.info(f"⏭️ Skipping already migrated file: {file_key}")
                folder_stats['skipped'] += 1
                self.stats['files_skipped'] += 1
                continue
            
            # Parse legacy path
            parsed_info = self._parse_legacy_path(file_key)
            if not parsed_info:
                logger.error(f"❌ Failed to parse path: {file_key}")
                folder_stats['failed'] += 1
                self.stats['files_failed'] += 1
                self.stats['errors'].append(f"Failed to parse: {file_key}")
                continue
            
            # Generate new path
            new_path = self._generate_new_path(parsed_info)
            if not new_path:
                logger.error(f"❌ Failed to generate new path for: {file_key}")
                folder_stats['failed'] += 1
                self.stats['files_failed'] += 1
                self.stats['errors'].append(f"Failed to map: {file_key}")
                continue
            
            # Check if destination already exists
            if self._file_exists(new_path):
                logger.info(f"⏭️ Destination already exists: {new_path}")
                folder_stats['skipped'] += 1
                self.stats['files_skipped'] += 1
                continue
            
            # Copy file to new location
            if self._copy_file(file_key, new_path):
                folder_stats['migrated'] += 1
                self.stats['files_migrated'] += 1
            else:
                folder_stats['failed'] += 1
                self.stats['files_failed'] += 1
                self.stats['errors'].append(f"Copy failed: {file_key} -> {new_path}")
        
        logger.info(f"✅ Completed {folder_type} migration: {folder_stats}")
        return folder_stats
    
    def migrate_all(self) -> Dict[str, Any]:
        """
        Migrate all legacy files to new structure
        
        Returns:
            Complete migration statistics
        """
        logger.info("🎯 Starting complete S3 structure migration...")
        
        start_time = datetime.now()
        
        # Migrate prompts and schemas
        prompts_stats = self.migrate_folder('prompts')
        schemas_stats = self.migrate_folder('schemas')
        
        end_time = datetime.now()
        duration = (end_time - start_time).total_seconds()
        
        # Compile final statistics
        final_stats = {
            'start_time': start_time.isoformat(),
            'end_time': end_time.isoformat(),
            'duration_seconds': duration,
            'dry_run': self.dry_run,
            'total_files_scanned': self.stats['files_scanned'],
            'total_files_migrated': self.stats['files_migrated'],
            'total_files_skipped': self.stats['files_skipped'],
            'total_files_failed': self.stats['files_failed'],
            'prompts_stats': prompts_stats,
            'schemas_stats': schemas_stats,
            'errors': self.stats['errors']
        }
        
        # Log final summary
        logger.info("="*60)
        logger.info("📊 MIGRATION SUMMARY")
        logger.info("="*60)
        logger.info(f"Mode: {'DRY RUN' if self.dry_run else 'LIVE MIGRATION'}")
        logger.info(f"Duration: {duration:.2f} seconds")
        logger.info(f"Total files scanned: {self.stats['files_scanned']}")
        logger.info(f"Total files migrated: {self.stats['files_migrated']}")
        logger.info(f"Total files skipped: {self.stats['files_skipped']}")
        logger.info(f"Total files failed: {self.stats['files_failed']}")
        
        if self.stats['errors']:
            logger.error(f"Errors encountered: {len(self.stats['errors'])}")
            for error in self.stats['errors'][:5]:  # Show first 5 errors
                logger.error(f"  - {error}")
            if len(self.stats['errors']) > 5:
                logger.error(f"  ... and {len(self.stats['errors']) - 5} more errors")
        
        logger.info("="*60)
        
        return final_stats
    
    def cleanup(self):
        """Clean up resources"""
        self.db_session.close()


def main():
    """Main migration script entry point"""
    parser = argparse.ArgumentParser(
        description='Migrate S3 files from name-based to ID-based structure'
    )
    parser.add_argument(
        '--dry-run', 
        action='store_true', 
        default=True,
        help='Run in simulation mode without making changes (default: True)'
    )
    parser.add_argument(
        '--execute', 
        action='store_true',
        help='Execute actual migration (overrides --dry-run)'
    )
    parser.add_argument(
        '--log-level',
        choices=['DEBUG', 'INFO', 'WARNING', 'ERROR'],
        default='INFO',
        help='Set logging level (default: INFO)'
    )
    parser.add_argument(
        '--output',
        help='Save migration report to JSON file'
    )
    
    args = parser.parse_args()
    
    # Set logging level
    logging.getLogger().setLevel(getattr(logging, args.log_level))
    
    # Determine run mode
    dry_run = not args.execute
    
    logger.info("🚀 Starting S3 Migration Script")
    logger.info(f"Mode: {'DRY RUN' if dry_run else 'LIVE MIGRATION'}")
    logger.info(f"Log Level: {args.log_level}")
    
    # Create migration manager
    migration_manager = S3MigrationManager(dry_run=dry_run)
    
    try:
        # Run migration
        results = migration_manager.migrate_all()
        
        # Save results to file if requested
        if args.output:
            with open(args.output, 'w') as f:
                json.dump(results, f, indent=2, default=str)
            logger.info(f"📄 Migration report saved to: {args.output}")
        
        # Exit with appropriate code
        if results['total_files_failed'] > 0:
            logger.error("Migration completed with errors")
            sys.exit(1)
        else:
            logger.info("✅ Migration completed successfully")
            sys.exit(0)
            
    except Exception as e:
        logger.error(f"❌ Migration failed: {e}")
        sys.exit(1)
    finally:
        migration_manager.cleanup()


if __name__ == '__main__':
    main()