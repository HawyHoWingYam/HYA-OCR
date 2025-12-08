'use client';

import { Suspense, useCallback, useEffect, useMemo, useState } from 'react';
import { useRouter, useSearchParams } from 'next/navigation';
import type {
  Company,
  DocumentType,
  CompanyDocTypeConfig,
  MappingItemType,
} from '@/lib/api';
import { companiesApi, documentTypesApi, ocrConfigsApi } from '@/lib/api';
import ConfigFileUploadField from '@/components/ocr/ConfigFileUploadField';
import MasterCsvPicker from '@/components/ocr/MasterCsvPicker';
import OneDriveBrowserModal from '@/components/ocr/OneDriveBrowserModal';
import JoinKeySelector from '@/components/ocr/JoinKeySelector';
import ColumnAliasEditor from '@/components/ocr/ColumnAliasEditor';
import JoinNormalizeEditor from '@/components/ocr/JoinNormalizeEditor';
import SchemaFieldExplorer from '@/components/ocr/SchemaFieldExplorer';

const ITEM_TYPE_OPTIONS: Array<{ value: MappingItemType; label: string }> = [
  { value: 'single_source', label: 'Single Source' },
  { value: 'multi_source', label: 'Multi Source' },
];

const deriveFileNameFromPath = (path?: string) => {
  if (!path) return '';
  const segments = path.split('/');
  return segments[segments.length - 1] || '';
};

type OutputMetaRow = {
  dest: string;
  srcType: 'ctx' | 'col';
  srcKey: string;
};

type AttachmentRule = {
  path?: string;
  filename_contains?: string;
  join_key?: string;
  label?: string;
  metadata?: string;
};

interface FormState {
  company_id: string;
  doc_type_id: string;
  item_type: MappingItemType;
  prompt_path: string;
  schema_path: string;
  master_csv_path: string;
  output_template_path: string;
  original_prompt_filename?: string;
  original_schema_filename?: string;
  priority: string;
  active: boolean;
  single_external_join_keys: string;
  single_column_aliases: string;
  single_normalize_strip_non_digits: boolean;
  single_normalize_zfill: string;
  single_output_meta_rows: OutputMetaRow[];
  single_merge_suffix: string;
  multi_step1_join_keys: string;
  multi_step1_column_aliases: string;
  multi_step1_normalize_strip_non_digits: boolean;
  multi_step1_normalize_zfill: string;
  multi_step1_merge_suffix: string;
  multi_step2_join_keys: string;
  multi_step2_column_aliases: string;
  multi_step2_normalize_strip_non_digits: boolean;
  multi_step2_normalize_zfill: string;
  multi_step2_output_meta_rows: OutputMetaRow[];
  multi_step2_merge_suffix: string;
  multi_internal_join_key: string;
}

const defaultFormState: FormState = {
  company_id: '',
  doc_type_id: '',
  item_type: 'single_source',
  prompt_path: '',
  schema_path: '',
  master_csv_path: '',
  output_template_path: '',
  original_prompt_filename: '',
  original_schema_filename: '',
  priority: '100',
  active: true,
  single_external_join_keys: '',
  single_column_aliases: '',
  single_normalize_strip_non_digits: false,
  single_normalize_zfill: '',
  single_output_meta_rows: [],
  single_merge_suffix: '',
  multi_step1_join_keys: '',
  multi_step1_column_aliases: '',
  multi_step1_normalize_strip_non_digits: false,
  multi_step1_normalize_zfill: '',
  multi_step1_merge_suffix: '',
  multi_step2_join_keys: '',
  multi_step2_column_aliases: '',
  multi_step2_normalize_strip_non_digits: false,
  multi_step2_normalize_zfill: '',
  multi_step2_output_meta_rows: [],
  multi_step2_merge_suffix: '',
  multi_internal_join_key: '',
};

function CreateOcrConfigPageContent() {
  const router = useRouter();
  const searchParams = useSearchParams();
  const returnUrl = searchParams.get('returnUrl');

  const [companies, setCompanies] = useState<Company[]>([]);
  const [docTypes, setDocTypes] = useState<DocumentType[]>([]);
  const [isLoading, setIsLoading] = useState(true);
  const [error, setError] = useState<string>('');

  const [form, setForm] = useState<FormState>(defaultFormState);
  const [multiAttachmentRules, setMultiAttachmentRules] = useState<AttachmentRule[]>([]);
  const [attachmentModalIndex, setAttachmentModalIndex] = useState<number | null>(null);

  const [isSubmitting, setIsSubmitting] = useState(false);
  const [csvPreview, setCsvPreview] = useState<{ headers: string[]; row_count: number } | null>(null);
  const [isPreviewingCsv, setIsPreviewingCsv] = useState(false);

  const parseColumnAliases = (value: string): Record<string, string> => {
    if (!value.trim()) {
      return {};
    }
    const aliases: Record<string, string> = {};
    value
      .split(',')
      .map((token) => token.trim())
      .filter(Boolean)
      .forEach((token) => {
        const [left, right] = token.split(':').map((part) => part.trim());
        if (left && right) {
          aliases[left] = right;
        }
      });
    return aliases;
  };

  useEffect(() => {
    const loadData = async () => {
      setIsLoading(true);
      setError('');
      try {
        const [comps, docs] = await Promise.all([
          companiesApi.getAll(),
          documentTypesApi.getAll(),
        ]);
        setCompanies(comps);
        setDocTypes(docs);
      } catch (err) {
        console.error(err);
        setError('Failed to load companies and document types');
      } finally {
        setIsLoading(false);
      }
    };
    loadData();
  }, []);

  const onFormChange = (e: React.ChangeEvent<HTMLInputElement | HTMLSelectElement>) => {
    const { name, value, type, checked } = e.target as HTMLInputElement;
    setForm((prev) => ({
      ...prev,
      [name]:
        type === 'checkbox'
          ? checked
          : name === 'company_id' || name === 'doc_type_id'
          ? value
          : name === 'priority'
          ? value
          : value,
    }));
  };

  const handleMasterCsvChange = (path: string) => {
    setForm((prev) => ({
      ...prev,
      master_csv_path: path,
    }));
    setCsvPreview(null);
  };

  const handlePreviewMasterCsv = async () => {
    const trimmedPath = form.master_csv_path.trim();
    if (!trimmedPath || isPreviewingCsv) {
      if (!trimmedPath) {
        setCsvPreview(null);
      }
      return;
    }
    setIsPreviewingCsv(true);
    setError('');
    try {
      const resp = await fetch(
        `/api/mapping/master-csv/preview?path=${encodeURIComponent(trimmedPath)}`,
      );
      if (!resp.ok) {
        const data = await resp.json().catch(() => ({}));
        throw new Error(data.detail || 'Failed to preview master CSV');
      }
      const data = await resp.json();
      setCsvPreview({
        headers: data.headers || [],
        row_count: data.row_count || 0,
      });
    } catch (err) {
      console.error(err);
      setCsvPreview(null);
      setError(err instanceof Error ? err.message : 'Failed to preview master CSV');
    } finally {
      setIsPreviewingCsv(false);
    }
  };

  const handlePromptUploadComplete = (path: string) => {
    const filename = path ? deriveFileNameFromPath(path) : '';
    setForm((prev) => ({
      ...prev,
      prompt_path: path,
      original_prompt_filename: filename,
    }));
  };

  const handleSchemaUploadComplete = (path: string) => {
    const filename = path ? deriveFileNameFromPath(path) : '';
    setForm((prev) => ({
      ...prev,
      schema_path: path,
      original_schema_filename: filename,
    }));
  };

  const handleOutputTemplateUploadComplete = (path: string) => {
    setForm((prev) => ({
      ...prev,
      output_template_path: path,
    }));
  };

  const openAttachmentBrowser = (index: number) => {
    setAttachmentModalIndex(index);
  };

  const closeAttachmentBrowser = () => {
    setAttachmentModalIndex(null);
  };

  const handleAttachmentPathSelect = (path: string) => {
    if (attachmentModalIndex === null) return;
    setMultiAttachmentRules((prev) => {
      if (!prev[attachmentModalIndex]) return prev;
      const next = [...prev];
      next[attachmentModalIndex] = { ...next[attachmentModalIndex], path };
      return next;
    });
    closeAttachmentBrowser();
  };

  const renderUploadPlaceholder = (label: string) => (
    <div className="border border-dashed border-gray-300 rounded-lg p-4 bg-gray-50 h-full flex flex-col justify-center">
      <p className="text-sm font-semibold text-gray-700">{label}</p>
      <p className="text-xs text-gray-500">
        Create the config first to upload the {label.toLowerCase()}.
      </p>
    </div>
  );

  const onSubmit = async (e: React.FormEvent) => {
    e.preventDefault();
    if (!form.company_id || !form.doc_type_id) {
      setError('Please select company and document type');
      return;
    }
    setIsSubmitting(true);
    setError('');
    try {
      const trimmedMasterCsv = form.master_csv_path.trim();

      const buildSingleSourceConfig = () => {
        const cfg: any = {};
        if (trimmedMasterCsv) {
          cfg.master_csv_path = trimmedMasterCsv;
        }
        const keys = form.single_external_join_keys
          .split(',')
          .map((k) => k.trim())
          .filter(Boolean);
        if (keys.length > 0) {
          cfg.external_join_keys = keys;
        }
        const aliases = parseColumnAliases(form.single_column_aliases);
        if (Object.keys(aliases).length > 0) {
          cfg.column_aliases = aliases;
        }
        const jn: any = {};
        if (form.single_normalize_strip_non_digits) {
          jn.strip_non_digits = true;
        }
        const zf = (form.single_normalize_zfill || '').trim();
        if (zf) {
          const val = parseInt(zf, 10);
          if (!Number.isNaN(val) && val >= 0) {
            jn.zfill = val;
          }
        }
        if (Object.keys(jn).length > 0) {
          cfg.join_normalize = jn;
        }
        const om: Record<string, string> = {};
        for (const row of form.single_output_meta_rows) {
          const dest = (row.dest || '').trim();
          const srcKey = (row.srcKey || '').trim();
          if (dest && srcKey && (row.srcType === 'ctx' || row.srcType === 'col')) {
            om[dest] = `${row.srcType}:${srcKey}`;
          }
        }
        if (Object.keys(om).length > 0) {
          cfg.output_meta = om;
        }
        const ms = (form.single_merge_suffix || '').trim();
        if (ms) {
          cfg.merge_suffix = ms;
        }
        return Object.keys(cfg).length > 0 ? cfg : null;
      };

      const buildMultiStep1Config = () => {
        const cfg: any = {};
        const keys = form.multi_step1_join_keys
          .split(',')
          .map((k) => k.trim())
          .filter(Boolean);
        if (keys.length > 0) {
          cfg.join_keys = keys;
        }
        const aliases = parseColumnAliases(form.multi_step1_column_aliases);
        if (Object.keys(aliases).length > 0) {
          cfg.column_aliases = aliases;
        }
        const jn: any = {};
        if (form.multi_step1_normalize_strip_non_digits) {
          jn.strip_non_digits = true;
        }
        const zf = (form.multi_step1_normalize_zfill || '').trim();
        if (zf) {
          const val = parseInt(zf, 10);
          if (!Number.isNaN(val) && val >= 0) {
            jn.zfill = val;
          }
        }
        if (Object.keys(jn).length > 0) {
          cfg.join_normalize = jn;
        }
        const ms = (form.multi_step1_merge_suffix || '').trim();
        if (ms) {
          cfg.merge_suffix = ms;
        }
        return Object.keys(cfg).length > 0 ? cfg : null;
      };

      const buildMultiStep2Config = () => {
        const cfg: any = {};
        const keys = form.multi_step2_join_keys
          .split(',')
          .map((k) => k.trim())
          .filter(Boolean);
        if (keys.length > 0) {
          cfg.join_keys = keys;
        }
        const aliases = parseColumnAliases(form.multi_step2_column_aliases);
        if (Object.keys(aliases).length > 0) {
          cfg.column_aliases = aliases;
        }
        const jn: any = {};
        if (form.multi_step2_normalize_strip_non_digits) {
          jn.strip_non_digits = true;
        }
        const zf = (form.multi_step2_normalize_zfill || '').trim();
        if (zf) {
          const val = parseInt(zf, 10);
          if (!Number.isNaN(val) && val >= 0) {
            jn.zfill = val;
          }
        }
        if (Object.keys(jn).length > 0) {
          cfg.join_normalize = jn;
        }
        const om: Record<string, string> = {};
        for (const row of form.multi_step2_output_meta_rows) {
          const dest = (row.dest || '').trim();
          const srcKey = (row.srcKey || '').trim();
          if (dest && srcKey && (row.srcType === 'ctx' || row.srcType === 'col')) {
            om[dest] = `${row.srcType}:${srcKey}`;
          }
        }
        if (Object.keys(om).length > 0) {
          cfg.output_meta = om;
        }
        const ms = (form.multi_step2_merge_suffix || '').trim();
        if (ms) {
          cfg.merge_suffix = ms;
        }
        return Object.keys(cfg).length > 0 ? cfg : null;
      };

      let attachmentValidationFailed = false;
      const buildAttachmentSources = () => {
        if (form.item_type !== 'multi_source') return [];

        for (const r of multiAttachmentRules) {
          const text = (r.metadata || '').trim();
          if (text.length > 0) {
            try {
              const parsed = JSON.parse(text);
              if (!parsed || typeof parsed !== 'object' || Array.isArray(parsed)) {
                throw new Error('metadata must be a JSON object');
              }
            } catch (e) {
              setError(
                'Invalid metadata JSON in attachment rules. Please provide a valid JSON object.',
              );
              attachmentValidationFailed = true;
              return [];
            }
          }
        }

        const cleaned = multiAttachmentRules
          .filter(
            (r) =>
              ((r.join_key || '').trim().length > 0 ||
                (r.filename_contains || '').trim().length > 0 ||
                (r.path || '').trim().length > 0),
          )
          .map((r) => {
            const obj: any = {
              kind: 'onedrive',
              path: (r.path || '').trim(),
              join_key: r.join_key?.trim() || undefined,
              filename_contains: r.filename_contains?.trim() || undefined,
            };
            const label = (r.label || '').trim();
            if (label) obj.label = label;
            const metaText = (r.metadata || '').trim();
            if (metaText) {
              try {
                const parsed = JSON.parse(metaText);
                if (parsed && typeof parsed === 'object' && !Array.isArray(parsed)) {
                  obj.metadata = parsed;
                }
              } catch {
                // metadata parsing already validated above; keep best-effort
              }
            }
            return obj;
          })
          .filter((r) => r.path.length > 0);
        return cleaned;
      };

      const singleConfig =
        form.item_type === 'single_source' ? buildSingleSourceConfig() : null;
      const multiStep1Config =
        form.item_type === 'multi_source' ? buildMultiStep1Config() : null;
      const multiStep2Config =
        form.item_type === 'multi_source' ? buildMultiStep2Config() : null;
      const attachmentSources = buildAttachmentSources();
      if (attachmentValidationFailed) {
        setIsSubmitting(false);
        return;
      }

      const payloadBase = {
        company_id: Number(form.company_id),
        doc_type_id: Number(form.doc_type_id),
        item_type: form.item_type,
        prompt_path: form.prompt_path || null,
        schema_path: form.schema_path || null,
        master_csv_path: trimmedMasterCsv || null,
        output_template_path: form.output_template_path || null,
        storage_type: 'local' as const,
        storage_metadata: null,
        single_source_config: singleConfig,
        multi_source_step1_config: multiStep1Config,
        multi_source_step2_config: multiStep2Config,
        internal_join_key:
          form.item_type === 'multi_source'
            ? (form.multi_internal_join_key || '').trim() || null
            : null,
        attachment_sources:
          form.item_type === 'multi_source' && attachmentSources.length > 0
            ? attachmentSources
            : null,
        active: form.active,
        priority: parseInt(form.priority || '100', 10) || 100,
      };

      await ocrConfigsApi.create(payloadBase);

      // Navigate back to list with preserved filters
      if (returnUrl) {
        router.push(returnUrl);
      } else {
        router.push('/admin/ocr-configs');
      }
    } catch (err) {
      console.error(err);
      setError(err instanceof Error ? err.message : 'Failed to create OCR config');
    } finally {
      setIsSubmitting(false);
    }
  };

  if (isLoading) {
    return <div className="text-center py-10">Loading...</div>;
  }

  return (
    <div className="max-w-6xl mx-auto">
      <div className="mb-6">
        <button
          onClick={() => router.back()}
          className="text-blue-600 hover:text-blue-800 text-sm mb-4"
        >
          ← Back to List
        </button>
        <h1 className="text-2xl font-bold">Create New OCR Config</h1>
      </div>

      {error && (
        <div className="mb-4 bg-red-100 text-red-800 px-4 py-2 rounded text-sm">
          {error}
        </div>
      )}

      <div className="bg-white shadow rounded-lg p-4">
        <form onSubmit={onSubmit} className="space-y-4">
          <div className="grid grid-cols-1 md:grid-cols-3 gap-4">
            <div>
              <label className="block text-xs font-semibold text-gray-600 mb-1">
                Company
              </label>
              <select
                name="company_id"
                value={form.company_id}
                onChange={onFormChange}
                className="w-full border border-gray-300 rounded px-2 py-1 text-sm"
                required
              >
                <option value="">Select Company</option>
                {companies.map((c) => (
                  <option key={c.company_id} value={c.company_id}>
                    {c.company_name}
                  </option>
                ))}
              </select>
            </div>
            <div>
              <label className="block text-xs font-semibold text-gray-600 mb-1">
                Document Type
              </label>
              <select
                name="doc_type_id"
                value={form.doc_type_id}
                onChange={onFormChange}
                className="w-full border border-gray-300 rounded px-2 py-1 text-sm"
                required
              >
                <option value="">Select Document Type</option>
                {docTypes.map((d) => (
                  <option key={d.doc_type_id} value={d.doc_type_id}>
                    {d.type_name}
                  </option>
                ))}
              </select>
            </div>
            <div>
              <label className="block text-xs font-semibold text-gray-600 mb-1">
                Item Type
              </label>
              <select
                name="item_type"
                value={form.item_type}
                onChange={onFormChange}
                className="w-full border border-gray-300 rounded px-2 py-1 text-sm"
              >
                {ITEM_TYPE_OPTIONS.map((opt) => (
                  <option key={opt.value} value={opt.value}>
                    {opt.label}
                  </option>
                ))}
              </select>
            </div>
          </div>

          <div className="grid grid-cols-1 md:grid-cols-2 gap-4">
            <div>{renderUploadPlaceholder('Prompt File')}</div>
            <div>{renderUploadPlaceholder('Schema File')}</div>
          </div>

          <div className="grid grid-cols-1 md:grid-cols-2 gap-4">
            <div>
              <MasterCsvPicker
                value={form.master_csv_path}
                onChange={handleMasterCsvChange}
                onPreview={form.master_csv_path.trim() ? handlePreviewMasterCsv : undefined}
              />
              <div className="mt-2 flex items-center gap-2 min-h-[1rem]">
                {isPreviewingCsv && (
                  <span className="text-xs text-gray-500">Loading preview…</span>
                )}
                {csvPreview && (
                  <span className="text-xs text-gray-600">
                    {csvPreview.headers.length} columns · {csvPreview.row_count} rows
                  </span>
                )}
              </div>
            </div>
            <div>{renderUploadPlaceholder('Output Template File')}</div>
          </div>

          <div className="flex items-center gap-4">
            <div>
              <label className="block text-xs font-semibold text-gray-600 mb-1">
                Priority
              </label>
              <input
                type="number"
                name="priority"
                value={form.priority}
                onChange={onFormChange}
                className="w-28 border border-gray-300 rounded px-2 py-1 text-sm"
              />
            </div>
            <div className="flex items-center gap-2 mt-5">
              <input
                id="ocr-config-active"
                type="checkbox"
                name="active"
                checked={form.active}
                onChange={onFormChange}
                className="h-4 w-4"
              />
              <label htmlFor="ocr-config-active" className="text-sm text-gray-700">
                Active
              </label>
            </div>
          </div>

          {/* Mapping Configuration Section */}
          <div className="border-t pt-4 mt-4">
            <h2 className="text-lg font-semibold text-gray-800 mb-4">Mapping Configuration</h2>

            {/* Schema Field Explorer */}
            {form.schema_path && (
              <div className="mb-4">
                <SchemaFieldExplorer
                  schemaPath={form.schema_path}
                  configId={undefined}
                />
              </div>
            )}

            {/* Single Source Configuration */}
            {form.item_type === 'single_source' && (
              <div className="space-y-4 bg-gray-50 p-4 rounded">
                <h3 className="font-medium text-gray-700">Single Source Mapping</h3>

                <JoinKeySelector
                  label="External Join Keys"
                  value={form.single_external_join_keys.split(',').filter(Boolean)}
                  onChange={(keys) => {
                    setForm(prev => ({
                      ...prev,
                      single_external_join_keys: keys.join(',')
                    }));
                  }}
                  availableOcrColumns={form.schema_path ? [] : []}
                  availableMasterColumns={csvPreview?.headers || []}
                  columnAliases={Object.fromEntries(
                    form.single_column_aliases.split(',').map(pair => {
                      const [ocr, master] = pair.split(':');
                      return [ocr?.trim(), master?.trim()];
                    }).filter(([k]) => k)
                  )}
                  helpText="Select columns from OCR output to use as join keys"
                />

                <ColumnAliasEditor
                  value={Object.fromEntries(
                    form.single_column_aliases.split(',').map(pair => {
                      const [ocr, master] = pair.split(':');
                      return [ocr?.trim(), master?.trim()];
                    }).filter(([k]) => k)
                  )}
                  onChange={(aliases) => {
                    setForm(prev => ({
                      ...prev,
                      single_column_aliases: Object.entries(aliases)
                        .map(([ocr, master]) => `${ocr}:${master}`)
                        .join(',')
                    }));
                  }}
                  availableOcrColumns={[]}
                  availableMasterColumns={csvPreview?.headers || []}
                />

                <JoinNormalizeEditor
                  value={{
                    strip_non_digits: form.single_normalize_strip_non_digits,
                    zfill: form.single_normalize_zfill ? parseInt(form.single_normalize_zfill) : undefined,
                  }}
                  onChange={(config) => {
                    setForm(prev => ({
                      ...prev,
                      single_normalize_strip_non_digits: config.strip_non_digits || false,
                      single_normalize_zfill: config.zfill ? String(config.zfill) : '',
                    }));
                  }}
                />

                <div>
                  <label className="block text-xs font-semibold text-gray-600 mb-1">
                    Merge Suffix
                  </label>
                  <input
                    type="text"
                    value={form.single_merge_suffix}
                    onChange={(e) => {
                      setForm(prev => ({
                        ...prev,
                        single_merge_suffix: e.target.value
                      }));
                    }}
                    placeholder="e.g. _master"
                    className="w-full border border-gray-300 rounded px-2 py-1 text-sm"
                  />
                </div>
              </div>
            )}

            {/* Multi Source Configuration */}
            {form.item_type === 'multi_source' && (
              <div className="space-y-4 bg-gray-50 p-4 rounded">
                <h3 className="font-medium text-gray-700">Multi Source Mapping</h3>

                <div>
                  <label className="block text-xs font-semibold text-gray-600 mb-1">
                    Internal Join Key
                  </label>
                  <input
                    type="text"
                    value={form.multi_internal_join_key}
                    onChange={(e) => {
                      setForm(prev => ({
                        ...prev,
                        multi_internal_join_key: e.target.value
                      }));
                    }}
                    placeholder="e.g. invoice_number"
                    className="w-full border border-gray-300 rounded px-2 py-1 text-sm"
                  />
                  <p className="text-xs text-gray-500 mt-1">
                    Default join key for merging primary and attachment data
                  </p>
                </div>

                <div className="border-t pt-4">
                  <h4 className="font-medium text-gray-700 mb-3">Step 1: Month Excel Mapping</h4>

                  <JoinKeySelector
                    label="Step 1 Join Keys"
                    value={form.multi_step1_join_keys.split(',').filter(Boolean)}
                    onChange={(keys) => {
                      setForm(prev => ({
                        ...prev,
                        multi_step1_join_keys: keys.join(',')
                      }));
                    }}
                    availableOcrColumns={[]}
                    availableMasterColumns={[]}
                    helpText="Join keys for OCR to month Excel mapping"
                  />

                  <ColumnAliasEditor
                    value={Object.fromEntries(
                      form.multi_step1_column_aliases.split(',').map(pair => {
                        const [ocr, master] = pair.split(':');
                        return [ocr?.trim(), master?.trim()];
                      }).filter(([k]) => k)
                    )}
                    onChange={(aliases) => {
                      setForm(prev => ({
                        ...prev,
                        multi_step1_column_aliases: Object.entries(aliases)
                          .map(([ocr, master]) => `${ocr}:${master}`)
                          .join(',')
                      }));
                    }}
                    availableOcrColumns={[]}
                    availableMasterColumns={[]}
                  />
                </div>

                <div className="border-t pt-4">
                  <h4 className="font-medium text-gray-700 mb-3">Step 2: Master CSV Mapping</h4>

                  <JoinKeySelector
                    label="Step 2 Join Keys"
                    value={form.multi_step2_join_keys.split(',').filter(Boolean)}
                    onChange={(keys) => {
                      setForm(prev => ({
                        ...prev,
                        multi_step2_join_keys: keys.join(',')
                      }));
                    }}
                    availableOcrColumns={[]}
                    availableMasterColumns={csvPreview?.headers || []}
                    helpText="Join keys for month Excel to master CSV mapping"
                  />

                  <ColumnAliasEditor
                    value={Object.fromEntries(
                      form.multi_step2_column_aliases.split(',').map(pair => {
                        const [ocr, master] = pair.split(':');
                        return [ocr?.trim(), master?.trim()];
                      }).filter(([k]) => k)
                    )}
                    onChange={(aliases) => {
                      setForm(prev => ({
                        ...prev,
                        multi_step2_column_aliases: Object.entries(aliases)
                          .map(([ocr, master]) => `${ocr}:${master}`)
                          .join(',')
                      }));
                    }}
                    availableOcrColumns={[]}
                    availableMasterColumns={csvPreview?.headers || []}
                  />

                  <JoinNormalizeEditor
                    value={{
                      strip_non_digits: form.multi_step2_normalize_strip_non_digits,
                      zfill: form.multi_step2_normalize_zfill ? parseInt(form.multi_step2_normalize_zfill) : undefined,
                    }}
                    onChange={(config) => {
                      setForm(prev => ({
                        ...prev,
                        multi_step2_normalize_strip_non_digits: config.strip_non_digits || false,
                        multi_step2_normalize_zfill: config.zfill ? String(config.zfill) : '',
                      }));
                    }}
                  />
                </div>
              </div>
            )}
          </div>

          <div className="flex gap-3">
            <button
              type="submit"
              disabled={isSubmitting}
              className="bg-blue-600 text-white px-4 py-2 rounded hover:bg-blue-700 text-sm disabled:bg-gray-400"
            >
              {isSubmitting ? 'Creating...' : 'Create Config'}
            </button>
            <button
              type="button"
              onClick={() => router.back()}
              className="px-4 py-2 text-sm rounded border border-gray-300 text-gray-700 hover:bg-gray-100"
            >
              Cancel
            </button>
          </div>
        </form>
      </div>

      <OneDriveBrowserModal
        open={attachmentModalIndex !== null}
        onClose={closeAttachmentBrowser}
        onSelect={handleAttachmentPathSelect}
        initialPath={
          attachmentModalIndex !== null
            ? multiAttachmentRules[attachmentModalIndex]?.path || ''
            : ''
        }
        filterExt=".pdf,.xlsx,.csv"
      />
    </div>
  );
}

export default function CreateOcrConfigPage() {
  return (
    <Suspense fallback={<div className="text-center py-10">Loading...</div>}>
      <CreateOcrConfigPageContent />
    </Suspense>
  );
}
