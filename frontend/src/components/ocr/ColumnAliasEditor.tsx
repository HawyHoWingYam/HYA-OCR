'use client';

import { useCallback } from 'react';

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
  const handleAddRow = useCallback(() => {
    const newAliases = { ...value, '': '' };
    onChange(newAliases);
  }, [value, onChange]);

  const handleUpdateRow = useCallback((oldOcr: string, newOcr: string, newMaster: string) => {
    const newAliases = { ...value };
    if (oldOcr !== newOcr) {
      delete newAliases[oldOcr];
    }
    if (newOcr) {
      newAliases[newOcr] = newMaster;
    }
    onChange(newAliases);
  }, [value, onChange]);

  const handleRemoveRow = useCallback((ocrColumn: string) => {
    const newAliases = { ...value };
    delete newAliases[ocrColumn];
    onChange(newAliases);
  }, [value, onChange]);

  return (
    <div className="space-y-2">
      <div className="flex items-center justify-between">
        <label className="text-xs font-semibold text-gray-600">Column Aliases</label>
        <button
          type="button"
          onClick={handleAddRow}
          disabled={disabled}
          className="text-xs text-blue-600 hover:text-blue-800"
        >
          + Add Alias
        </button>
      </div>
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
                <tr key={ocrCol || 'new'}>
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
                    <select
                      value={masterCol}
                      onChange={(e) => handleUpdateRow(ocrCol, ocrCol, e.target.value)}
                      disabled={disabled}
                      className="w-full border border-gray-300 rounded px-2 py-1 text-sm"
                    >
                      <option value="">Select master column...</option>
                      {availableMasterColumns.map(col => (
                        <option key={col} value={col}>{col}</option>
                      ))}
                    </select>
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
    </div>
  );
}
