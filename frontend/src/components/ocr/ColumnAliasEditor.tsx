'use client';

import { useCallback, useMemo, useState } from 'react';

interface ColumnAliasEditorProps {
  value: Record<string, string>;
  onChange: (aliases: Record<string, string>) => void;
  availableOcrColumns: string[];
  availableMasterColumns: string[];
  disabled?: boolean;
}

export default function ColumnAliasEditor({
  value,
  onChange,
  availableOcrColumns,
  availableMasterColumns,
  disabled = false,
}: ColumnAliasEditorProps) {
  const [newOcrColumn, setNewOcrColumn] = useState('');
  const [newMasterColumn, setNewMasterColumn] = useState('');

  const masterOptions = useMemo(() => {
    const set = new Set<string>();
    availableMasterColumns.forEach(col => {
      if (col) set.add(col);
    });
    Object.values(value).forEach(col => {
      if (col) set.add(col);
    });
    return Array.from(set);
  }, [availableMasterColumns, value]);

  const handleAddRow = useCallback(() => {
    const ocr = newOcrColumn.trim();
    const master = newMasterColumn.trim();
    if (!ocr || !master) {
      return;
    }
    const newAliases = { ...value, [ocr]: master };
    onChange(newAliases);
    setNewOcrColumn('');
    setNewMasterColumn('');
  }, [newOcrColumn, newMasterColumn, value, onChange]);

  const handleUpdateRow = useCallback((oldOcr: string, newOcr: string, newMaster: string) => {
    const next = { ...value };
    if (oldOcr && oldOcr !== newOcr) {
      delete next[oldOcr];
    }
    const trimmedOcr = newOcr.trim();
    if (trimmedOcr) {
      next[trimmedOcr] = newMaster.trim();
    }
    onChange(next);
  }, [value, onChange]);

  const handleRemoveRow = useCallback((ocrColumn: string) => {
    const next = { ...value };
    delete next[ocrColumn];
    onChange(next);
  }, [value, onChange]);

  const availableOcrForNew = useMemo(
    () => availableOcrColumns.filter(col => !(col in value)),
    [availableOcrColumns, value],
  );

  const canAdd = !!newOcrColumn && !!newMasterColumn && !disabled;

  return (
    <div className="space-y-2">
      <label className="text-xs font-semibold text-gray-600">Column Aliases</label>
      <p className="text-xs text-gray-500">
        Map OCR output column names to master CSV column names for joining
      </p>

      {Object.keys(value).length > 0 ? (
        <div className="border border-gray-200 rounded overflow-hidden">
          <table className="w-full text-sm">
            <thead className="bg-gray-50">
              <tr>
                <th className="px-3 py-2 text-left text-xs font-medium text-gray-500">OCR Column</th>
                <th className="px-3 py-2 text-center text-xs font-medium text-gray-500">→</th>
                <th className="px-3 py-2 text-left text-xs font-medium text-gray-500">Master Column</th>
                <th className="px-3 py-2 w-10"></th>
              </tr>
            </thead>
            <tbody className="divide-y divide-gray-200">
              {Object.entries(value).map(([ocrCol, masterCol]) => (
                <tr key={ocrCol}>
                  <td className="px-3 py-2">
                    <select
                      value={ocrCol}
                      onChange={(e) => handleUpdateRow(ocrCol, e.target.value, masterCol)}
                      disabled={disabled}
                      className="w-full border border-gray-300 rounded px-2 py-1 text-sm"
                    >
                      <option value="">Select OCR column...</option>
                      {availableOcrColumns.map(col => (
                        <option key={col} value={col}>{col}</option>
                      ))}
                    </select>
                  </td>
                  <td className="px-3 py-2 text-center text-gray-400">→</td>
              <td className="px-3 py-2">
                {masterOptions.length > 0 ? (
                  <select
                    value={masterCol}
                    onChange={(e) => handleUpdateRow(ocrCol, ocrCol, e.target.value)}
                    disabled={disabled}
                    className="w-full border border-gray-300 rounded px-2 py-1 text-sm"
                  >
                    <option value="">Select master column...</option>
                    {masterOptions.map(col => (
                      <option key={col} value={col}>{col}</option>
                    ))}
                  </select>
                ) : (
                  <input
                    type="text"
                    value={masterCol}
                    onChange={(e) => handleUpdateRow(ocrCol, ocrCol, e.target.value)}
                    disabled={disabled}
                    className="w-full border border-gray-300 rounded px-2 py-1 text-sm"
                    placeholder="Enter master column name"
                  />
                )}
                  </td>
                  <td className="px-3 py-2">
                    <button
                      type="button"
                      onClick={() => handleRemoveRow(ocrCol)}
                      disabled={disabled}
                      className="text-red-500 hover:text-red-700"
                    >
                      ×
                    </button>
                  </td>
                </tr>
              ))}
            </tbody>
          </table>
        </div>
      ) : (
        <div className="text-xs text-gray-400 italic py-2">
          No column aliases configured. Add aliases if OCR column names differ from master CSV.
        </div>
      )}

      <div className="flex items-center gap-2">
        <select
          value={newOcrColumn}
          onChange={(e) => setNewOcrColumn(e.target.value)}
          disabled={disabled || availableOcrForNew.length === 0}
          className="flex-1 border border-gray-300 rounded px-2 py-1 text-sm"
        >
          <option value="">Select OCR column...</option>
          {availableOcrForNew.map(col => (
            <option key={col} value={col}>{col}</option>
          ))}
        </select>
        <span className="text-gray-400">→</span>
        {masterOptions.length > 0 ? (
          <select
            value={newMasterColumn}
            onChange={(e) => setNewMasterColumn(e.target.value)}
            disabled={disabled}
            className="flex-1 border border-gray-300 rounded px-2 py-1 text-sm"
          >
            <option value="">Select master column...</option>
            {masterOptions.map(col => (
              <option key={col} value={col}>{col}</option>
            ))}
          </select>
        ) : (
          <input
            type="text"
            value={newMasterColumn}
            onChange={(e) => setNewMasterColumn(e.target.value)}
            disabled={disabled}
            className="flex-1 border border-gray-300 rounded px-2 py-1 text-sm"
            placeholder="Master column (free text)"
          />
        )}
        <button
          type="button"
          onClick={handleAddRow}
          disabled={!canAdd}
          className="text-xs text-blue-600 hover:text-blue-800 disabled:opacity-50 disabled:cursor-not-allowed"
        >
          + Add Alias
        </button>
      </div>
    </div>
  );
}
