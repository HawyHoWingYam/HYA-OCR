'use client';

import type { JoinNormalizeConfig } from '@/types/mapping-config';

interface JoinNormalizeEditorProps {
  value: JoinNormalizeConfig;
  onChange: (config: JoinNormalizeConfig) => void;
  disabled?: boolean;
}

export default function JoinNormalizeEditor({
  value,
  onChange,
  disabled = false,
}: JoinNormalizeEditorProps) {
  const handleToggle = (key: keyof JoinNormalizeConfig) => {
    const newValue = { ...value };
    if (typeof newValue[key] === 'boolean') {
      newValue[key] = !newValue[key] as any;
    } else {
      newValue[key] = true as any;
    }
    // Clean up false values
    Object.keys(newValue).forEach(k => {
      if (newValue[k as keyof JoinNormalizeConfig] === false) {
        delete newValue[k as keyof JoinNormalizeConfig];
      }
    });
    onChange(newValue);
  };

  const handleZfillChange = (val: string) => {
    const newValue = { ...value };
    const num = parseInt(val, 10);
    if (!isNaN(num) && num >= 0) {
      newValue.zfill = num;
    } else {
      delete newValue.zfill;
    }
    onChange(newValue);
  };

  return (
    <div className="space-y-3">
      <label className="block text-xs font-semibold text-gray-600">Join Value Normalization</label>
      <p className="text-xs text-gray-500">
        Transform join key values before matching (applied to both OCR and master data)
      </p>

      <div className="grid grid-cols-2 gap-3">
        <label className="flex items-center gap-2 text-sm">
          <input
            type="checkbox"
            checked={!!value.strip_non_digits}
            onChange={() => handleToggle('strip_non_digits')}
            disabled={disabled}
            className="h-4 w-4"
          />
          <span>Strip non-digits</span>
        </label>

        <label className="flex items-center gap-2 text-sm">
          <input
            type="checkbox"
            checked={!!value.lower}
            onChange={() => handleToggle('lower')}
            disabled={disabled}
            className="h-4 w-4"
          />
          <span>Lowercase</span>
        </label>

        <label className="flex items-center gap-2 text-sm">
          <input
            type="checkbox"
            checked={!!value.normalize_ws}
            onChange={() => handleToggle('normalize_ws')}
            disabled={disabled}
            className="h-4 w-4"
          />
          <span>Normalize whitespace</span>
        </label>

        <label className="flex items-center gap-2 text-sm">
          <input
            type="checkbox"
            checked={!!value.nfkc}
            onChange={() => handleToggle('nfkc')}
            disabled={disabled}
            className="h-4 w-4"
          />
          <span>Unicode NFKC</span>
        </label>
      </div>

      <div className="flex items-center gap-2">
        <label className="text-sm">Zero-fill to length:</label>
        <input
          type="number"
          min="0"
          max="20"
          value={typeof value.zfill === 'number' ? value.zfill : ''}
          onChange={(e) => handleZfillChange(e.target.value)}
          disabled={disabled}
          placeholder="e.g. 9"
          className="w-20 border border-gray-300 rounded px-2 py-1 text-sm"
        />
      </div>
    </div>
  );
}
