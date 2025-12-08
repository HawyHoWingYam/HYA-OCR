'use client';

import { useCallback, useMemo } from 'react';

interface JoinKeySelectorProps {
  label: string;
  value: string[];
  onChange: (keys: string[]) => void;
  availableOcrColumns: string[];
  availableMasterColumns: string[];
  columnAliases?: Record<string, string>;
  disabled?: boolean;
  helpText?: string;
}

export default function JoinKeySelector({
  label,
  value,
  onChange,
  availableOcrColumns,
  availableMasterColumns,
  columnAliases = {},
  disabled = false,
  helpText,
}: JoinKeySelectorProps) {
  const handleAddKey = useCallback((key: string) => {
    if (key && !value.includes(key)) {
      onChange([...value, key]);
    }
  }, [value, onChange]);

  const handleRemoveKey = useCallback((key: string) => {
    onChange(value.filter(k => k !== key));
  }, [value, onChange]);

  const getMasterColumnName = useCallback((ocrKey: string) => {
    return columnAliases[ocrKey] || ocrKey;
  }, [columnAliases]);

  const unusedColumns = useMemo(() => {
    return availableOcrColumns.filter(col => !value.includes(col));
  }, [availableOcrColumns, value]);

  return (
    <div className="space-y-2">
      <label className="block text-xs font-semibold text-gray-600">{label}</label>
      {helpText && <p className="text-xs text-gray-500">{helpText}</p>}

      {/* Selected keys */}
      <div className="flex flex-wrap gap-2">
        {value.map((key) => {
          const masterCol = getMasterColumnName(key);
          const inMaster = availableMasterColumns.includes(masterCol);
          return (
            <div
              key={key}
              className={`flex items-center gap-1 px-2 py-1 rounded text-sm ${
                inMaster ? 'bg-blue-100 text-blue-800' : 'bg-yellow-100 text-yellow-800'
              }`}
            >
              <span className="font-mono">{key}</span>
              {key !== masterCol && (
                <span className="text-xs opacity-75">→ {masterCol}</span>
              )}
              {!inMaster && (
                <span className="text-xs" title="Column not found in master CSV">⚠️</span>
              )}
              <button
                type="button"
                onClick={() => handleRemoveKey(key)}
                disabled={disabled}
                className="ml-1 text-gray-500 hover:text-red-600"
              >
                ×
              </button>
            </div>
          );
        })}
      </div>

      {/* Add new key */}
      {unusedColumns.length > 0 && (
        <select
          value=""
          onChange={(e) => handleAddKey(e.target.value)}
          disabled={disabled}
          className="w-full border border-gray-300 rounded px-2 py-1 text-sm"
        >
          <option value="">+ Add join key...</option>
          {unusedColumns.map(col => (
            <option key={col} value={col}>{col}</option>
          ))}
        </select>
      )}
    </div>
  );
}
