"""
S3 File Structure Migration and Alignment Tool
Implements standardized S3 folder structure and fixes alignment issues.
"""

import logging
import os
from typing import Dict, List, Optional, Tuple
from datetime import datetime
import json
import asyncio
from pathlib import Path

from .s3_storage import S3StorageManager, get_s3_manager
from .company_file_manager import FileType
from db.database import get_db
from db.models import BatchJob, Company, DocumentType, CompanyDocTypeConfig
from sqlalchemy.orm import Session

logger = logging.getLogger(__name__)


class S3StructureMigrationManager:
    """Manages S3 file structure migration and alignment."""
    
    def __init__(self, s3_manager: Optional[S3StorageManager] = None):
        self.s3_manager = s3_manager or get_s3_manager()
        self.migration_stats = {
            'files_migrated': 0,
            'files_skipped': 0,
            'errors': 0,
            'structure_fixes': 0
        }
    
    def analyze_current_structure(self) -> Dict:
        """Analyze current S3 structure and identify alignment issues."""
        analysis = {
            'folder_structure': {},
            'alignment_issues': [],
            'storage_patterns': {},
            'recommendations': []
        }
        
        if not self.s3_manager:
            logger.error("S3 manager not available")
            return analysis
        
        # Analyze each folder type
        folder_types = ['upload', 'results', 'exports', 'prompts', 'schemas']
        
        for folder_type in folder_types:
            logger.info(f"Analyzing {folder_type} folder structure...")
            
            try:
                # List files in each folder
                files = self.s3_manager.list_files(prefix="", folder=folder_type, max_keys=1000)
                
                analysis['folder_structure'][folder_type] = {
                    'total_files': len(files),
                    'patterns': self._analyze_path_patterns(files),
                    'size_total': sum(f.get('size', 0) for f in files)
                }
                
                # Check for alignment issues
                issues = self._detect_alignment_issues(folder_type, files)
                analysis['alignment_issues'].extend(issues)
                
            except Exception as e:
                logger.error(f"Error analyzing {folder_type} folder: {e}")
                analysis['alignment_issues'].append({
                    'type': 'analysis_error',
                    'folder': folder_type,
                    'error': str(e)
                })
        
        # Generate recommendations
        analysis['recommendations'] = self._generate_recommendations(analysis)
        
        return analysis
    
    def _analyze_path_patterns(self, files: List[Dict]) -> Dict:
        """Analyze path patterns in file list."""
        patterns = {
            'legacy_paths': 0,
            'id_based_paths': 0,
            'temp_paths': 0,
            'mixed_patterns': 0
        }
        
        for file_info in files:
            key = file_info.get('key', '')
            
            # Detect pattern types
            if 'temp_' in key:
                patterns['temp_paths'] += 1
            elif key.startswith('companies/') and '/prompts/' in key or '/schemas/' in key:
                patterns['id_based_paths'] += 1
            elif 'batch_results' in key or 'uploads/' in key:
                patterns['legacy_paths'] += 1
            else:
                patterns['mixed_patterns'] += 1
        
        return patterns
    
    def _detect_alignment_issues(self, folder_type: str, files: List[Dict]) -> List[Dict]:
        """Detect alignment issues in folder structure."""
        issues = []
        
        for file_info in files:
            key = file_info.get('key', '')
            
            # Issue 1: Results in wrong folder
            if folder_type == 'upload' and 'batch_results' in key:
                issues.append({
                    'type': 'misplaced_results',
                    'file': key,
                    'current_folder': folder_type,
                    'should_be_folder': 'results',
                    'description': 'JSON results stored in upload folder instead of results folder'
                })
            
            # Issue 2: Inconsistent path structure
            if folder_type in ['prompts', 'schemas']:
                if 'temp_' in key and 'companies/' in key:
                    issues.append({
                        'type': 'temp_path_structure',
                        'file': key,
                        'folder': folder_type,
                        'description': 'Using temporary path structure instead of clean ID-based paths'
                    })
            
            # Issue 3: Missing exports
            if folder_type == 'exports' and len(files) == 0:
                issues.append({
                    'type': 'missing_exports_folder',
                    'folder': folder_type,
                    'description': 'No files found in exports folder - CSV/NetSuite files may not be generated'
                })
        
        return issues
    
    def _generate_recommendations(self, analysis: Dict) -> List[Dict]:
        """Generate recommendations based on analysis."""
        recommendations = []
        
        # Check for misplaced results
        upload_patterns = analysis['folder_structure'].get('upload', {}).get('patterns', {})
        if upload_patterns.get('legacy_paths', 0) > 0:
            recommendations.append({
                'priority': 'high',
                'type': 'move_results',
                'description': 'Move JSON results from upload/batch_results/ to results/ folder',
                'action': 'migrate_batch_results'
            })
        
        # Check for missing CSV generation
        exports_files = analysis['folder_structure'].get('exports', {}).get('total_files', 0)
        if exports_files == 0:
            recommendations.append({
                'priority': 'high',
                'type': 'implement_csv_generation',
                'description': 'Implement missing CSV output path generation for batch jobs',
                'action': 'enable_csv_generation'
            })
        
        # Check for temp path cleanup
        for folder in ['prompts', 'schemas']:
            folder_patterns = analysis['folder_structure'].get(folder, {}).get('patterns', {})
            if folder_patterns.get('temp_paths', 0) > 0:
                recommendations.append({
                    'priority': 'medium',
                    'type': 'standardize_paths',
                    'description': f'Standardize {folder} paths to use clean ID-based structure',
                    'action': f'migrate_{folder}_paths'
                })
        
        return recommendations
    
    def fix_batch_results_structure(self) -> Dict:
        """Fix batch results structure by moving files to correct folders."""
        fix_stats = {
            'moved_files': 0,
            'errors': 0,
            'skipped_files': 0
        }
        
        logger.info("Starting batch results structure fix...")
        
        try:
            # Find misplaced batch results
            upload_files = self.s3_manager.list_files(prefix="batch_results", folder="upload", max_keys=1000)
            
            for file_info in upload_files:
                old_key = file_info['full_key']
                
                # Generate new key in results folder
                # From: upload/batch_results/COMPANY/DOC_TYPE/batch_N/file.json
                # To: results/COMPANY/DOC_TYPE/batch_N/file.json
                if old_key.startswith('upload/batch_results/'):
                    new_key = old_key.replace('upload/batch_results/', 'results/')
                    
                    try:
                        # Copy to new location
                        copy_source = {'Bucket': self.s3_manager.bucket_name, 'Key': old_key}
                        self.s3_manager.s3_client.copy_object(
                            CopySource=copy_source,
                            Bucket=self.s3_manager.bucket_name,
                            Key=new_key
                        )
                        
                        # Verify copy succeeded
                        if self.s3_manager.file_exists(new_key.replace('results/', ''), 'results'):
                            # Delete old file
                            self.s3_manager.s3_client.delete_object(
                                Bucket=self.s3_manager.bucket_name,
                                Key=old_key
                            )
                            
                            fix_stats['moved_files'] += 1
                            logger.info(f"Moved: {old_key} -> {new_key}")
                        else:
                            fix_stats['errors'] += 1
                            logger.error(f"Copy verification failed for {old_key}")
                            
                    except Exception as e:
                        fix_stats['errors'] += 1
                        logger.error(f"Error moving file {old_key}: {e}")
                else:
                    fix_stats['skipped_files'] += 1
            
        except Exception as e:
            logger.error(f"Error during batch results structure fix: {e}")
            fix_stats['errors'] += 1
        
        return fix_stats
    
    def implement_csv_output_generation(self, db: Session) -> Dict:
        """Implement missing CSV output path generation for existing batch jobs."""
        implementation_stats = {
            'jobs_updated': 0,
            'csv_files_generated': 0,
            'errors': 0
        }
        
        logger.info("Implementing CSV output generation for existing batch jobs...")
        
        try:
            # Find batch jobs without CSV output paths
            batch_jobs = db.query(BatchJob).filter(
                BatchJob.csv_output_path.is_(None),
                BatchJob.json_output_path.is_not(None)
            ).limit(50).all()
            
            for job in batch_jobs:
                try:
                    # Get company and doc type info
                    company = db.query(Company).filter(Company.company_id == job.company_id).first()
                    doc_type = db.query(DocumentType).filter(DocumentType.doc_type_id == job.doc_type_id).first()
                    
                    if not company or not doc_type:
                        logger.warning(f"Missing company or doc type for batch job {job.batch_id}")
                        continue
                    
                    # Generate CSV output path
                    csv_filename = f"batch_{job.batch_id}_results.csv"
                    csv_key = f"results/{company.company_code}/{doc_type.type_code}/batch_{job.batch_id}/{csv_filename}"
                    csv_output_path = f"s3://{self.s3_manager.bucket_name}/{csv_key}"
                    
                    # Check if JSON results exist
                    if job.json_output_path:
                        try:
                            # Download JSON results
                            json_content = self.s3_manager.download_file_by_stored_path(job.json_output_path)
                            
                            if json_content:
                                # Parse JSON and generate CSV
                                json_data = json.loads(json_content.decode('utf-8'))
                                csv_content = self._convert_json_to_csv(json_data)
                                
                                # Upload CSV to S3
                                success = self.s3_manager.s3_client.put_object(
                                    Bucket=self.s3_manager.bucket_name,
                                    Key=csv_key,
                                    Body=csv_content.encode('utf-8'),
                                    ContentType='text/csv'
                                )
                                
                                if success:
                                    # Update batch job with CSV path
                                    job.csv_output_path = csv_output_path
                                    db.commit()
                                    
                                    implementation_stats['jobs_updated'] += 1
                                    implementation_stats['csv_files_generated'] += 1
                                    logger.info(f"Generated CSV for batch job {job.batch_id}")
                                
                        except Exception as e:
                            logger.error(f"Error generating CSV for batch job {job.batch_id}: {e}")
                            implementation_stats['errors'] += 1
                    else:
                        # Just update the path structure without generating file
                        job.csv_output_path = csv_output_path
                        db.commit()
                        implementation_stats['jobs_updated'] += 1
                        logger.info(f"Updated CSV path for batch job {job.batch_id}")
                
                except Exception as e:
                    logger.error(f"Error processing batch job {job.batch_id}: {e}")
                    implementation_stats['errors'] += 1
                    db.rollback()
        
        except Exception as e:
            logger.error(f"Error during CSV generation implementation: {e}")
            implementation_stats['errors'] += 1
            db.rollback()
        
        return implementation_stats
    
    def _convert_json_to_csv(self, json_data: Dict) -> str:
        """Convert JSON results to CSV format."""
        import csv
        from io import StringIO
        
        output = StringIO()
        
        # Handle different JSON structures
        if isinstance(json_data, list):
            # List of records
            if json_data:
                writer = csv.DictWriter(output, fieldnames=json_data[0].keys())
                writer.writeheader()
                writer.writerows(json_data)
        elif isinstance(json_data, dict):
            # Single record or nested structure
            if 'results' in json_data:
                results = json_data['results']
                if isinstance(results, list) and results:
                    writer = csv.DictWriter(output, fieldnames=results[0].keys())
                    writer.writeheader()
                    writer.writerows(results)
            else:
                # Flatten single record
                writer = csv.DictWriter(output, fieldnames=json_data.keys())
                writer.writeheader()
                writer.writerow(json_data)
        
        return output.getvalue()
    
    def standardize_folder_structure(self) -> Dict:
        """Standardize S3 folder structure according to design specification."""
        standardization_stats = {
            'folders_created': 0,
            'structures_aligned': 0,
            'errors': 0
        }
        
        logger.info("Standardizing S3 folder structure...")
        
        # Define desired folder structure
        desired_folders = [
            'uploads/',      # OCR Images/ZIP/PDF
            'results/',      # Result JSON files  
            'exports/',      # NetSuite CSV, mapped CSV files
            'prompts/',      # Prompt files
            'schemas/',      # Schema files
            'backups/'       # Backup files (new)
        ]
        
        try:
            for folder in desired_folders:
                # Create folder marker (empty file to ensure folder exists)
                marker_key = f"{folder}.folder_marker"
                
                if not self.s3_manager.file_exists('.folder_marker', folder.rstrip('/')):
                    try:
                        self.s3_manager.s3_client.put_object(
                            Bucket=self.s3_manager.bucket_name,
                            Key=marker_key,
                            Body=b'',
                            Metadata={
                                'purpose': 'folder_structure_marker',
                                'created_at': datetime.now().isoformat()
                            }
                        )
                        standardization_stats['folders_created'] += 1
                        logger.info(f"Created folder structure marker: {folder}")
                    except Exception as e:
                        logger.error(f"Error creating folder {folder}: {e}")
                        standardization_stats['errors'] += 1
        
        except Exception as e:
            logger.error(f"Error during folder structure standardization: {e}")
            standardization_stats['errors'] += 1
        
        return standardization_stats
    
    def add_backups_folder_support(self, db: Session) -> Dict:
        """Add support for backups folder in S3 structure."""
        backup_stats = {
            'backup_policies_created': 0,
            'backup_folders_initialized': 0,
            'errors': 0
        }
        
        logger.info("Adding backups folder support...")
        
        try:
            # Create backup folder structure
            backup_folders = [
                'backups/database/',
                'backups/configurations/',
                'backups/files/',
                'backups/exports/'
            ]
            
            for backup_folder in backup_folders:
                marker_key = f"{backup_folder}.backup_marker"
                
                try:
                    self.s3_manager.s3_client.put_object(
                        Bucket=self.s3_manager.bucket_name,
                        Key=marker_key,
                        Body=json.dumps({
                            'purpose': 'backup_folder',
                            'created_at': datetime.now().isoformat(),
                            'retention_policy': '30_days',
                            'backup_type': backup_folder.split('/')[-2]
                        }).encode('utf-8'),
                        ContentType='application/json',
                        Metadata={
                            'backup_folder': 'true',
                            'created_at': datetime.now().isoformat()
                        }
                    )
                    backup_stats['backup_folders_initialized'] += 1
                    logger.info(f"Initialized backup folder: {backup_folder}")
                except Exception as e:
                    logger.error(f"Error creating backup folder {backup_folder}: {e}")
                    backup_stats['errors'] += 1
        
        except Exception as e:
            logger.error(f"Error during backup folder setup: {e}")
            backup_stats['errors'] += 1
        
        return backup_stats
    
    def run_comprehensive_migration(self, db: Session) -> Dict:
        """Run comprehensive S3 structure migration and fixes."""
        logger.info("Starting comprehensive S3 structure migration...")
        
        migration_results = {
            'analysis': {},
            'batch_results_fix': {},
            'csv_generation': {},
            'folder_standardization': {},
            'backup_support': {},
            'success': True,
            'total_time': 0
        }
        
        start_time = datetime.now()
        
        try:
            # 1. Analyze current structure
            logger.info("Step 1: Analyzing current S3 structure...")
            migration_results['analysis'] = self.analyze_current_structure()
            
            # 2. Fix batch results structure
            logger.info("Step 2: Fixing batch results structure...")
            migration_results['batch_results_fix'] = self.fix_batch_results_structure()
            
            # 3. Implement CSV generation
            logger.info("Step 3: Implementing CSV output generation...")
            migration_results['csv_generation'] = self.implement_csv_output_generation(db)
            
            # 4. Standardize folder structure
            logger.info("Step 4: Standardizing folder structure...")
            migration_results['folder_standardization'] = self.standardize_folder_structure()
            
            # 5. Add backup support
            logger.info("Step 5: Adding backup folder support...")
            migration_results['backup_support'] = self.add_backups_folder_support(db)
            
        except Exception as e:
            logger.error(f"Error during comprehensive migration: {e}")
            migration_results['success'] = False
            migration_results['error'] = str(e)
        
        end_time = datetime.now()
        migration_results['total_time'] = (end_time - start_time).total_seconds()
        
        # Generate summary
        self._log_migration_summary(migration_results)
        
        return migration_results
    
    def _log_migration_summary(self, results: Dict):
        """Log migration summary."""
        logger.info("="*60)
        logger.info("S3 STRUCTURE MIGRATION SUMMARY")
        logger.info("="*60)
        
        if results.get('success'):
            logger.info("✅ Migration completed successfully")
        else:
            logger.error("❌ Migration completed with errors")
            if 'error' in results:
                logger.error(f"Main error: {results['error']}")
        
        logger.info(f"Total migration time: {results.get('total_time', 0):.2f} seconds")
        
        # Analysis summary
        analysis = results.get('analysis', {})
        if analysis:
            logger.info("\n📊 STRUCTURE ANALYSIS:")
            for folder, info in analysis.get('folder_structure', {}).items():
                logger.info(f"  {folder}: {info.get('total_files', 0)} files")
            
            issues = analysis.get('alignment_issues', [])
            logger.info(f"  Alignment issues found: {len(issues)}")
        
        # Fix summaries
        fixes = ['batch_results_fix', 'csv_generation', 'folder_standardization', 'backup_support']
        for fix_type in fixes:
            fix_stats = results.get(fix_type, {})
            if fix_stats:
                logger.info(f"\n🔧 {fix_type.upper().replace('_', ' ')}:")
                for key, value in fix_stats.items():
                    logger.info(f"  {key}: {value}")
        
        logger.info("="*60)


def run_s3_migration():
    """Convenience function to run S3 migration."""
    s3_manager = get_s3_manager()
    if not s3_manager:
        logger.error("S3 manager not available")
        return None
    
    migration_manager = S3StructureMigrationManager(s3_manager)
    
    # Get database session
    db = next(get_db())
    try:
        results = migration_manager.run_comprehensive_migration(db)
        return results
    finally:
        db.close()


if __name__ == "__main__":
    # Run migration if called directly
    results = run_s3_migration()
    if results:
        print(f"Migration completed. Success: {results.get('success')}")
    else:
        print("Migration failed - S3 not available")