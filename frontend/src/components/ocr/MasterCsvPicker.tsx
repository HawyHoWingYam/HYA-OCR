'use client';

import { useCallback, useEffect, useMemo, useState } from 'react';
import OneDriveBrowserModal from './OneDriveBrowserModal';
import type { MasterCsvPreview } from '@/types/mapping-config';

interface MasterCsvPickerProps {
  value: string;
  onChange: (path: string) => void;
  onPreview?: () => void;
  onHeadersLoaded?: (headers: string[]) => void;
  configId?: number;
}

const RECENT_KEY = 'hya-ocr:master-csv-recent';
const MASTER_CSV_ROOT = 'HYA-OCR/Master Data';

export default function MasterCsvPicker({ value, onChange, onPreview, onHeadersLoaded, configId }: MasterCsvPickerProps) {
  const [inputValue, setInputValue] = useState(value);
  const [recentPaths, setRecentPaths] = useState<string[]>([]);
  const [isModalOpen, setIsModalOpen] = useState(false);
  const [validationError, setValidationError] = useState<string>('');
  const [previewData, setPreviewData] = useState<MasterCsvPreview | null>(null);
  const [isLoadingPreview, setIsLoadingPreview] = useState(false);
  const [previewError, setPreviewError] = useState<string | null>(null);
  const [isPreviewExpanded, setIsPreviewExpanded] = useState(false);

  useEffect(() => {
    setInputValue(value);
  }, [value]);

  useEffect(() => {
    if (typeof window === 'undefined') return;
    try {
      const stored = window.localStorage.getItem(RECENT_KEY);
      if (stored) {
        const parsed = JSON.parse(stored);
        if (Array.isArray(parsed)) {
          setRecentPaths(parsed);
        }
      }
    } catch (err) {
      console.warn('Failed to parse recent master CSV paths', err);
    }
  }, []);

  const isValidPath = useCallback((path: string): boolean => {
    const normalized = path.trim();
    if (!normalized) return true; // Allow empty
    return normalized.startsWith(MASTER_CSV_ROOT + '/') || normalized === MASTER_CSV_ROOT;
  }, []);

  const persistRecent = useCallback((path: string) => {
    const normalized = path.trim();
    if (!normalized || !isValidPath(normalized)) return;
    setRecentPaths((prev) => {
      const next = [normalized, ...prev.filter((item) => item !== normalized)].slice(0, 8);
      if (typeof window !== 'undefined') {
        window.localStorage.setItem(RECENT_KEY, JSON.stringify(next));
      }
      return next;
    });
  }, [isValidPath]);

  const handleInputChange = useCallback((next: string) => {
    setInputValue(next);
    onChange(next);

    // Validate path
    if (next.trim() && !isValidPath(next)) {
      setValidationError(`Path must start with '${MASTER_CSV_ROOT}'`);
    } else {
      setValidationError('');
    }
  }, [onChange, isValidPath]);

  const handleBlurPersist = useCallback(() => {
    if (inputValue.trim()) {
      persistRecent(inputValue.trim());
    }
  }, [inputValue, persistRecent]);

  const handleRecentSelect = useCallback((path: string) => {
    setInputValue(path);
    onChange(path);
    persistRecent(path);
  }, [onChange, persistRecent]);

  const handleBrowseSelect = useCallback((path: string) => {
    setIsModalOpen(false);
    setInputValue(path);
    onChange(path);
    persistRecent(path);
  }, [onChange, persistRecent]);

  const handlePreviewClick = useCallback(async () => {
    if (!inputValue.trim() || validationError) return;

    setIsLoadingPreview(true);
    setPreviewError(null);
    setPreviewData(null);

    try {
      const encodedPath = encodeURIComponent(inputValue.trim());
      const response = await fetch(`/api/mapping/master-csv/preview?path=${encodedPath}`);

      if (!response.ok) {
        const data = await response.json().catch(() => ({}));
        throw new Error(data.detail || `Failed to preview: ${response.statusText}`);
      }

      const data = await response.json();
      setPreviewData(data);
      onHeadersLoaded?.(data.headers || []);
      setIsPreviewExpanded(true);
    } catch (err) {
      setPreviewError(err instanceof Error ? err.message : 'Failed to load preview');
    } finally {
      setIsLoadingPreview(false);
    }

    onPreview?.();
  }, [inputValue, validationError, onPreview, onHeadersLoaded]);

  const initialFolder = useMemo(() => {
    if (!value) return '';
    const parts = value.split('/');
    if (parts.length <= 1) return '';
    return parts.slice(0, -1).join('/');
  }, [value]);

  return (
    <div className="space-y-2">
      <div className="flex items-center justify-between">
        <label className="text-xs font-semibold text-gray-600">Master CSV Path</label>
        {value && (
          <span className="text-[11px] text-gray-500">{value.length} chars</span>
        )}
      </div>
      <div className="flex flex-col md:flex-row gap-2">
        <div className="flex-1">
          <input
            type="text"
            value={inputValue}
            onChange={(event) => handleInputChange(event.target.value)}
            onBlur={handleBlurPersist}
            list="master-csv-recent"
            placeholder="e.g. HYA-OCR/Master Data/telecom_users.csv"
            className={`w-full border rounded px-3 py-2 text-sm ${validationError ? 'border-red-500' : 'border-gray-300'}`}
          />
          <datalist id="master-csv-recent">
            {recentPaths.map((path) => (
              <option key={path} value={path} />
            ))}
          </datalist>
        </div>
        <div className="flex gap-2">
          <button
            type="button"
            onClick={() => setIsModalOpen(true)}
            className="px-3 py-2 text-sm font-medium text-gray-700 border border-gray-300 rounded hover:bg-gray-50"
          >
            Browse OneDrive…
          </button>
          <button
            type="button"
            onClick={handlePreviewClick}
            disabled={!!validationError || isLoadingPreview}
            className="px-3 py-2 text-sm font-semibold text-white bg-gray-800 rounded disabled:bg-gray-300"
            title={validationError || undefined}
          >
            {isLoadingPreview ? 'Loading...' : 'Preview Columns'}
          </button>
      </div>
    </div>
    {validationError && (
      <div className="text-xs text-red-600 mt-1">{validationError}</div>
    )}
    {previewError && (
      <div className="text-xs text-red-600 mt-1">{previewError}</div>
    )}
    {previewData && (
      <div className="border border-gray-200 rounded-lg p-3 bg-gray-50 mt-2">
        <button
          type="button"
          onClick={() => setIsPreviewExpanded(!isPreviewExpanded)}
          className="flex items-center justify-between w-full text-left"
        >
          <span className="text-xs font-semibold text-gray-600">
            Preview: {previewData.headers.length} columns · {previewData.row_count} rows
          </span>
          <span className="text-gray-400">{isPreviewExpanded ? '▼' : '▶'}</span>
        </button>

        {isPreviewExpanded && (
          <div className="mt-2 space-y-2">
            <div>
              <p className="text-xs font-medium text-gray-600 mb-1">Columns:</p>
              <div className="flex flex-wrap gap-1">
                {previewData.headers.map((header) => (
                  <span
                    key={header}
                    className="px-2 py-1 bg-white border border-gray-300 rounded text-xs font-mono"
                  >
                    {header}
                  </span>
                ))}
              </div>
            </div>

            {previewData.sample && previewData.sample.length > 0 && (
              <div>
                <p className="text-xs font-medium text-gray-600 mb-1">Sample Row:</p>
                <div className="max-h-32 overflow-y-auto border border-gray-300 rounded bg-white">
                  <table className="w-full text-xs">
                    <thead className="bg-gray-50 sticky top-0">
                      <tr>
                        {previewData.headers.map((header) => (
                          <th
                            key={header}
                            className="px-2 py-1 text-left font-medium text-gray-600 border-r border-gray-200"
                          >
                            {header}
                          </th>
                        ))}
                      </tr>
                    </thead>
                    <tbody>
                      {previewData.sample.map((row, idx) => (
                        <tr key={idx}>
                          {previewData.headers.map((header) => (
                            <td
                              key={`${idx}-${header}`}
                              className="px-2 py-1 border-r border-gray-200 text-gray-700"
                            >
                              {String(row[header] || '—').substring(0, 50)}
                            </td>
                          ))}
                        </tr>
                      ))}
                    </tbody>
                  </table>
                </div>
              </div>
            )}
          </div>
        )}
      </div>
    )}
      {recentPaths.length > 0 && (
        <div className="text-xs text-gray-500 flex items-center gap-2 flex-wrap">
          <span className="uppercase tracking-wide text-[10px] text-gray-400">Recent</span>
          {recentPaths.map((path) => (
            <button
              type="button"
              key={path}
              onClick={() => handleRecentSelect(path)}
              className="px-2 py-1 border border-gray-200 rounded-full bg-gray-50 hover:bg-gray-100"
            >
              {path}
            </button>
          ))}
        </div>
      )}

      <OneDriveBrowserModal
        open={isModalOpen}
        onClose={() => setIsModalOpen(false)}
        onSelect={handleBrowseSelect}
        filterExt=".csv,.xlsx"
        initialPath={initialFolder}
        rootPath={MASTER_CSV_ROOT}
        configId={configId}
      />
    </div>
  );
}
