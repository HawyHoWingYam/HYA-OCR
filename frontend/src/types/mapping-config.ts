/**
 * TypeScript types for column mapping configuration
 * Mirrors backend/utils/mapping_config.py structures
 */

export interface JoinNormalizeConfig {
  strip_non_digits?: boolean;
  zfill?: number | Record<string, number>;
  strip_invisible?: boolean;
  nfkc?: boolean;
  normalize_ws?: boolean;
  lower?: boolean;
  value_alias_map?: Record<string, string> | Record<string, Record<string, string>>;
}

export interface OutputMetaConfig {
  [destColumn: string]: `ctx:${string}` | `col:${string}`;
}

export interface SingleSourceConfig {
  master_csv_path?: string;
  external_join_keys?: string[];
  column_aliases?: Record<string, string>;
  join_normalize?: JoinNormalizeConfig;
  output_meta?: OutputMetaConfig;
  merge_suffix?: string;
}

export interface MultiSourceStepConfig {
  join_keys?: string[];
  column_aliases?: Record<string, string>;
  join_normalize?: JoinNormalizeConfig;
  output_meta?: OutputMetaConfig;
  merge_suffix?: string;
}

export interface AttachmentSource {
  kind: 'onedrive';
  path: string;
  label?: string;
  metadata?: Record<string, any>;
  join_key?: string;
  filename_contains?: string;
}

export interface ColumnAliasEntry {
  id: string;
  ocrColumn: string;
  masterColumn: string;
}

export interface MasterCsvPreview {
  path: string;
  headers: string[];
  row_count: number;
  sample?: Record<string, any>[];
}

export interface SchemaFieldInfo {
  name: string;
  type: string;
  description?: string;
  required: boolean;
}
