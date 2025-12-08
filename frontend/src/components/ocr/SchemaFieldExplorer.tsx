'use client';

import { useEffect, useState } from 'react';
import type { SchemaFieldInfo } from '@/types/mapping-config';

interface SchemaFieldExplorerProps {
  schemaPath?: string;
  configId?: number;
  onFieldsLoaded?: (fields: string[]) => void;
}

export default function SchemaFieldExplorer({
  schemaPath,
  configId,
  onFieldsLoaded,
}: SchemaFieldExplorerProps) {
  const [fields, setFields] = useState<SchemaFieldInfo[]>([]);
  const [isLoading, setIsLoading] = useState(false);
  const [error, setError] = useState<string | null>(null);
  const [isExpanded, setIsExpanded] = useState(false);

  useEffect(() => {
    if (!schemaPath || !configId) {
      setFields([]);
      return;
    }

    const collectFields = (
      schema: any,
      path: string,
      required: boolean,
      acc: SchemaFieldInfo[],
    ) => {
      if (!schema || typeof schema !== 'object') {
        return;
      }

      const type = schema.type as string | undefined;

      // If this is a primitive leaf (no nested properties/items), record it
      const hasNestedProps = !!schema.properties || !!schema.items;
      if (type && type !== 'object' && type !== 'array') {
        acc.push({
          name: path,
          type,
          description: schema.description,
          required,
        });
        if (!hasNestedProps) {
          return;
        }
      }

      // Object properties
      if (schema.properties && typeof schema.properties === 'object') {
        const requiredList: string[] = Array.isArray(schema.required) ? schema.required : [];
        for (const [propName, propDef] of Object.entries<any>(schema.properties)) {
          const nextPath = path ? `${path}.${propName}` : propName;
          const isRequired = requiredList.includes(propName);
          collectFields(propDef, nextPath, isRequired, acc);
        }
      }

      // Array items – continue traversal using the same path so it matches flattened JSON columns
      if (schema.items && typeof schema.items === 'object') {
        collectFields(schema.items, path, required, acc);
      }
    };

    const loadSchema = async () => {
      setIsLoading(true);
      setError(null);
      try {
        // Fetch schema content via config files API
        const response = await fetch(
          `/api/company-doc-type-configs/${configId}/config-files/download?kind=schema`
        );
        if (!response.ok) {
          throw new Error('Failed to load schema');
        }
        const schemaJson = await response.json();

        const props = schemaJson.properties || {};
        const required = Array.isArray(schemaJson.required) ? schemaJson.required : [];

        const fieldList: SchemaFieldInfo[] = [];
        Object.entries<any>(props).forEach(([name, def]) => {
          const isRequired = required.includes(name);
          collectFields(def, name, isRequired, fieldList);
        });

        setFields(fieldList);
        onFieldsLoaded?.(fieldList.map(f => f.name));
      } catch (err) {
        setError(err instanceof Error ? err.message : 'Failed to parse schema');
        setFields([]);
      } finally {
        setIsLoading(false);
      }
    };

    loadSchema();
  }, [schemaPath, configId, onFieldsLoaded]);

  if (!schemaPath) {
    return null;
  }

  return (
    <div className="border border-gray-200 rounded-lg p-3 bg-gray-50">
      <button
        type="button"
        onClick={() => setIsExpanded(!isExpanded)}
        className="flex items-center justify-between w-full text-left"
      >
        <span className="text-xs font-semibold text-gray-600">
          Schema Fields ({fields.length})
        </span>
        <span className="text-gray-400">{isExpanded ? '▼' : '▶'}</span>
      </button>

      {isExpanded && (
        <div className="mt-2">
          {isLoading && <p className="text-xs text-gray-500">Loading schema...</p>}
          {error && <p className="text-xs text-red-600">{error}</p>}
          {fields.length > 0 && (
            <div className="max-h-48 overflow-y-auto">
              <table className="w-full text-xs">
                <thead>
                  <tr className="text-left text-gray-500">
                    <th className="py-1">Field</th>
                    <th className="py-1">Type</th>
                    <th className="py-1">Required</th>
                  </tr>
                </thead>
                <tbody>
                  {fields.map(field => (
                    <tr key={field.name} className="border-t border-gray-200">
                      <td className="py-1 font-mono text-gray-700">{field.name}</td>
                      <td className="py-1 text-gray-500">{field.type}</td>
                      <td className="py-1">{field.required ? '✓' : '—'}</td>
                    </tr>
                  ))}
                </tbody>
              </table>
            </div>
          )}
          {fields.length === 0 && !isLoading && !error && (
            <p className="text-xs text-gray-400 italic py-2">No fields found in schema</p>
          )}
        </div>
      )}
    </div>
  );
}
