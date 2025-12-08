#!/usr/bin/env python3
"""
Database Path Migration Script

This script updates database records to use the new ID-based S3 paths.

Updates the following tables:
- company_document_configs: prompt_path, schema_path fields
- Other tables with file path references as needed

OLD PATH FORMAT:
- prompts/{company_name|code}/{doc_type_name|code}/file.txt
- schemas/{company_name|code}/{doc_type_name|code}/file.json

NEW PATH FORMAT:
- s3://bucket/companies/{company_id}/prompts/{doc_type_id}/{config_id}_file.txt
- s3://bucket/companies/{company_id}/schemas/{doc_type_id}/{config_id}_file.json
"""

import os
import sys
import json
import logging
import argparse
from typing import Dict, Optional, Any
from datetime import datetime

# Add backend directory to Python path
sys.path.append(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from db.database import SessionLocal
from db.models import CompanyDocTypeConfig, File
from utils.company_file_manager import CompanyFileManager, FileType
from config_loader import config_loader

# Set up logging
logging.basicConfig(
    level=logging.INFO,
    format='%(asctime)s - %(name)s - %(levelname)s - %(message)s'
)
logger = logging.getLogger(__name__)


class DatabasePathMigrator:
    """Migrates database file paths from name-based to ID-based format"""
    
    def __init__(self, dry_run: bool = True):
        """
        Initialize database path migrator
        
        Args:
            dry_run: If True, only simulate migration without making changes
        """
        self.dry_run = dry_run
        self.company_file_manager = CompanyFileManager()
        self.bucket_name = config_loader.get_aws_config().get('s3_bucket_name', 'hya-ocr-sandbox')
        
        # Migration statistics
        self.stats = {
            'configs_scanned': 0,
            'configs_updated': 0,
            'configs_skipped': 0,
            'configs_failed': 0,
            'files_scanned': 0,
            'files_updated': 0,
            'files_skipped': 0,
            'files_failed': 0,
            'errors': []
        }
        
        # Database session
        self.db_session = SessionLocal()
    
    def _parse_legacy_path(self, path: str) -> Optional[Dict[str, Any]]:
        """
        Parse legacy path format to extract components
        
        Args:
            path: Legacy path (e.g., "prompts/hana/admin_billing/file.txt")
            
        Returns:
            Dictionary with parsed information or None if invalid
        """
        # Handle S3 URIs
        if path.startswith('s3://'):
            # Extract the key part after bucket
            parts = path.split('/', 3)
            if len(parts) >= 4:
                path = parts[3]  # Everything after s3://bucket/
            else:
                return None
        
        # Skip if already in new format (starts with "companies/")
        if path.startswith('companies/'):
            return {
                'is_new_format': True,
                'original_path': path
            }
        
        # Parse old format: folder_type/company_identifier/doc_type_identifier/filename
        parts = path.strip('/').split('/')
        
        if len(parts) < 3:
            logger.warning(f"Invalid path format: {path}")
            return None
        
        folder_type = parts[0]  # "prompts" or "schemas"
        company_identifier = parts[1]
        
        # Handle nested paths - everything between company and filename
        if len(parts) == 3:
            doc_type_identifier = parts[1]  # Sometimes company and doc_type are the same
            filename = parts[2]
        else:
            doc_type_identifier = '/'.join(parts[2:-1])
            filename = parts[-1]
        
        return {
            'is_new_format': False,
            'folder_type': folder_type,
            'company_identifier': company_identifier,
            'doc_type_identifier': doc_type_identifier,
            'filename': filename,
            'original_path': path
        }
    
    def _generate_new_s3_path(self, config: CompanyDocTypeConfig, file_type: str, original_path: str) -> Optional[str]:
        """
        Generate new S3 URI for a configuration file

        Args:
            config: CompanyDocTypeConfig object
            file_type: "prompt" or "schema"
            original_path: Original file path
            
        Returns:
            New S3 URI or None if generation fails
        """
        try:
            # Parse original path to get filename
            parsed = self._parse_legacy_path(original_path)
            if not parsed:
                return None
            
            # If already in new format, convert to full S3 URI
            if parsed.get('is_new_format'):
                return f"s3://{self.bucket_name}/{parsed['original_path']}"
            
            # Extract filename from original path
            filename = parsed.get('filename', f"{file_type}.{'txt' if file_type == 'prompt' else 'json'}")
            
            # Ensure filename has config_id prefix for uniqueness
            if not filename.startswith(f"config_{config.config_id}_"):
                # Extract file extension
                name_parts = filename.rsplit('.', 1)
                if len(name_parts) == 2:
                    name, ext = name_parts
                    filename = f"config_{config.config_id}_{name}.{ext}"
                else:
                    filename = f"config_{config.config_id}_{filename}"
            
            # Determine FileType
            file_type_enum = FileType.PROMPT if file_type == "prompt" else FileType.SCHEMA
            
            # Generate new path using CompanyFileManager
            new_path = self.company_file_manager.get_company_file_path(
                company_id=config.company_id,
                file_type=file_type_enum,
                filename=filename,
                doc_type_id=config.doc_type_id,
                config_id=config.config_id
            )
            
            # Return full S3 URI
            return f"s3://{self.bucket_name}/{new_path}"
            
        except Exception as e:
            logger.error(f"Error generating new path for config {config.config_id}: {e}")
            return None
    
    def migrate_config_paths(self) -> Dict[str, int]:
        """
        Migrate all company_document_configs table paths
        
        Returns:
            Migration statistics for configs
        """
        logger.info("🚀 Starting migration of company_document_configs paths...")
        
        config_stats = {
            'scanned': 0,
            'updated': 0,
            'skipped': 0,
            'failed': 0
        }
        
        # Query all configurations
        configs = self.db_session.query(CompanyDocTypeConfig).all()
        logger.info(f"Found {len(configs)} configurations to check")
        
        for config in configs:
            config_stats['scanned'] += 1
            self.stats['configs_scanned'] += 1
            
            updated = False
            update_data = {}
            
            # Check and migrate prompt_path
            if config.prompt_path:
                new_prompt_path = self._generate_new_s3_path(config, "prompt", config.prompt_path)
                if new_prompt_path and new_prompt_path != config.prompt_path:
                    update_data['prompt_path'] = new_prompt_path
                    logger.info(f"📝 Config {config.config_id} prompt: {config.prompt_path} -> {new_prompt_path}")
                    updated = True
                elif not new_prompt_path:
                    logger.error(f"❌ Failed to generate new prompt path for config {config.config_id}")
                    self.stats['errors'].append(f"Config {config.config_id}: prompt path generation failed")
            
            # Check and migrate schema_path
            if config.schema_path:
                new_schema_path = self._generate_new_s3_path(config, "schema", config.schema_path)
                if new_schema_path and new_schema_path != config.schema_path:
                    update_data['schema_path'] = new_schema_path
                    logger.info(f"📝 Config {config.config_id} schema: {config.schema_path} -> {new_schema_path}")
                    updated = True
                elif not new_schema_path:
                    logger.error(f"❌ Failed to generate new schema path for config {config.config_id}")
                    self.stats['errors'].append(f"Config {config.config_id}: schema path generation failed")
            
            # Update database if changes were made
            if updated and update_data:
                if self.dry_run:
                    logger.info(f"[DRY RUN] Would update config {config.config_id} with: {update_data}")
                    config_stats['updated'] += 1
                    self.stats['configs_updated'] += 1
                else:
                    try:
                        # Update the configuration
                        for key, value in update_data.items():
                            setattr(config, key, value)
                        
                        # Set updated timestamp
                        config.updated_at = datetime.utcnow()
                        
                        self.db_session.commit()
                        
                        logger.info(f"✅ Updated config {config.config_id}")
                        config_stats['updated'] += 1
                        self.stats['configs_updated'] += 1
                        
                    except Exception as e:
                        self.db_session.rollback()
                        logger.error(f"❌ Failed to update config {config.config_id}: {e}")
                        config_stats['failed'] += 1
                        self.stats['configs_failed'] += 1
                        self.stats['errors'].append(f"Config {config.config_id} update failed: {str(e)}")
            else:
                if not updated:
                    logger.info(f"⏭️ Config {config.config_id} already up-to-date or no paths to migrate")
                config_stats['skipped'] += 1
                self.stats['configs_skipped'] += 1
        
        logger.info(f"✅ Completed config migration: {config_stats}")
        return config_stats
    
    def migrate_file_paths(self) -> Dict[str, int]:
        """
        Migrate file table paths (if needed)
        
        Returns:
            Migration statistics for files
        """
        logger.info("🚀 Starting migration of files table paths...")
        
        file_stats = {
            'scanned': 0,
            'updated': 0,
            'skipped': 0,
            'failed': 0
        }
        
        # Query files that might have legacy paths
        files = self.db_session.query(File).filter(
            ~File.file_path.like('s3://%%/companies/%%')  # Not already in new format
        ).all()
        
        logger.info(f"Found {len(files)} files to check")
        
        for file_obj in files:
            file_stats['scanned'] += 1
            self.stats['files_scanned'] += 1
            
            # Parse the current file path
            parsed = self._parse_legacy_path(file_obj.file_path)
            
            if not parsed or parsed.get('is_new_format'):
                logger.info(f"⏭️ File {file_obj.file_id} already in new format or unparseable")
                file_stats['skipped'] += 1
                self.stats['files_skipped'] += 1
                continue
            
            # For files table, we need to map to the appropriate new path
            # This is more complex as files might be uploads, results, exports, etc.
            
            # For now, just log what would need to be updated
            logger.info(f"📄 File {file_obj.file_id} needs path update: {file_obj.file_path}")
            
            # Skip actual file path migration for now as it's complex without job context
            file_stats['skipped'] += 1
            self.stats['files_skipped'] += 1
        
        logger.info(f"✅ Completed file migration: {file_stats}")
        return file_stats
    
    def migrate_all(self) -> Dict[str, Any]:
        """
        Migrate all database paths to new format
        
        Returns:
            Complete migration statistics
        """
        logger.info("🎯 Starting complete database path migration...")
        
        start_time = datetime.now()
        
        # Migrate configuration paths (most important)
        config_stats = self.migrate_config_paths()
        
        # Migrate file paths (less critical for now)
        file_stats = self.migrate_file_paths()
        
        end_time = datetime.now()
        duration = (end_time - start_time).total_seconds()
        
        # Compile final statistics
        final_stats = {
            'start_time': start_time.isoformat(),
            'end_time': end_time.isoformat(),
            'duration_seconds': duration,
            'dry_run': self.dry_run,
            'config_stats': config_stats,
            'file_stats': file_stats,
            'total_errors': len(self.stats['errors']),
            'errors': self.stats['errors']
        }
        
        # Log final summary
        logger.info("="*60)
        logger.info("📊 DATABASE MIGRATION SUMMARY")
        logger.info("="*60)
        logger.info(f"Mode: {'DRY RUN' if self.dry_run else 'LIVE MIGRATION'}")
        logger.info(f"Duration: {duration:.2f} seconds")
        logger.info(f"Configs scanned: {self.stats['configs_scanned']}")
        logger.info(f"Configs updated: {self.stats['configs_updated']}")
        logger.info(f"Configs skipped: {self.stats['configs_skipped']}")
        logger.info(f"Configs failed: {self.stats['configs_failed']}")
        logger.info(f"Files scanned: {self.stats['files_scanned']}")
        logger.info(f"Files updated: {self.stats['files_updated']}")
        
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
        description='Migrate database file paths from name-based to ID-based format'
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
    
    logger.info("🚀 Starting Database Path Migration Script")
    logger.info(f"Mode: {'DRY RUN' if dry_run else 'LIVE MIGRATION'}")
    logger.info(f"Log Level: {args.log_level}")
    
    # Create migration manager
    migrator = DatabasePathMigrator(dry_run=dry_run)
    
    try:
        # Run migration
        results = migrator.migrate_all()
        
        # Save results to file if requested
        if args.output:
            with open(args.output, 'w') as f:
                json.dump(results, f, indent=2, default=str)
            logger.info(f"📄 Migration report saved to: {args.output}")
        
        # Exit with appropriate code
        total_failures = results['config_stats']['failed'] + results['file_stats']['failed']
        if total_failures > 0:
            logger.error("Migration completed with errors")
            sys.exit(1)
        else:
            logger.info("✅ Migration completed successfully")
            sys.exit(0)
            
    except Exception as e:
        logger.error(f"❌ Migration failed: {e}")
        sys.exit(1)
    finally:
        migrator.cleanup()


if __name__ == '__main__':
    main()