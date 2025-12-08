'use client';

import { useCallback, useEffect, useMemo, useState } from 'react';
import { OneDriveEntry, onedriveApi } from '@/lib/api';

interface OneDriveBrowserModalProps {
  open: boolean;
  onClose: () => void;
  onSelect: (path: string) => void;
  filterExt?: string;
  initialPath?: string;
  rootPath?: string;
  configId?: number;
}

type TreeNode = {
  path: string;
  name: string;
  expanded: boolean;
  children: string[];
  isLoaded: boolean;
  parent?: string;
};

const ROOT_KEY = '__root__';

const toKey = (path?: string | null) => {
  if (!path || path.trim() === '') {
    return ROOT_KEY;
  }
  return path.trim();
};

const deriveName = (path?: string | null) => {
  if (!path || path.trim() === '') {
    return 'Root';
  }
  const parts = path.split('/').filter(Boolean);
  return parts[parts.length - 1] || 'Root';
};

const createNode = (path?: string | null, parent?: string): TreeNode => ({
  path: path?.trim() || '',
  name: deriveName(path),
  expanded: path ? false : true,
  children: [],
  isLoaded: false,
  parent,
});

export default function OneDriveBrowserModal({
  open,
  onClose,
  onSelect,
  filterExt,
  initialPath,
  rootPath,
  configId,
}: OneDriveBrowserModalProps) {
  const [currentPath, setCurrentPath] = useState(initialPath?.trim() || '');
  const [entries, setEntries] = useState<OneDriveEntry[]>([]);
  const [parentPath, setParentPath] = useState<string | null>(null);
  const [isLoading, setIsLoading] = useState(false);
  const [error, setError] = useState<string | null>(null);
  const [searchTerm, setSearchTerm] = useState('');
  const [selectedEntry, setSelectedEntry] = useState<OneDriveEntry | null>(null);
  const [tree, setTree] = useState<Record<string, TreeNode>>({
    [ROOT_KEY]: createNode('', undefined),
  });

  const normalizedFilters = useMemo(() => {
    if (!filterExt) return [];
    return filterExt
      .split(',')
      .map((token) => token.trim().toLowerCase())
      .filter(Boolean);
  }, [filterExt]);

  const normalizedRootPath = useMemo(
    () => (rootPath || '').trim().replace(/\/+$/, ''),
    [rootPath],
  );

  const clampPathToRoot = useCallback(
    (path: string) => {
      const trimmed = (path || '').trim();
      if (!normalizedRootPath) return trimmed; // No rootPath set, keep original behavior

      if (!trimmed) return normalizedRootPath;
      if (
        trimmed === normalizedRootPath ||
        trimmed.startsWith(normalizedRootPath + '/')
      ) {
        return trimmed;
      }
      // Path is above root, force back to root
      return normalizedRootPath;
    },
    [normalizedRootPath],
  );

  const filteredEntries = useMemo(() => {
    const term = searchTerm.trim().toLowerCase();
    return entries.filter((entry) => {
      let match = true;
      if (term) {
        match = entry.name.toLowerCase().includes(term);
      }
      if (match && entry.type === 'file' && normalizedFilters.length > 0) {
        match = normalizedFilters.some((ext) => entry.name.toLowerCase().endsWith(ext));
      }
      return match;
    });
  }, [entries, normalizedFilters, searchTerm]);

  const updateTreeWithFolders = useCallback((path: string, folderEntries: OneDriveEntry[]) => {
    setTree((prev) => {
      const next = { ...prev };
      const key = toKey(path);
      const existing = next[key] || createNode(path);
      next[key] = {
        ...existing,
        children: folderEntries.map((item) => item.path),
        isLoaded: true,
      };

      if (path) {
        const parentPath = path.split('/').slice(0, -1).join('/');
        const parentKey = toKey(parentPath);
        const parentNode = next[parentKey] || createNode(parentPath || '', undefined);
        if (!parentNode.children.includes(path)) {
          next[parentKey] = {
            ...parentNode,
            children: [...parentNode.children, path],
          };
        }
      }

      folderEntries.forEach((folder) => {
        const childKey = toKey(folder.path);
        if (!next[childKey]) {
          next[childKey] = createNode(folder.path, path || undefined);
        } else {
          next[childKey] = { ...next[childKey], parent: path || undefined };
        }
      });

      return next;
    });
  }, []);

  const loadDirectory = useCallback(async (path: string, { focusPath = true }: { focusPath?: boolean } = {}) => {
    const apiPath = clampPathToRoot(path);

    if (focusPath) {
      setIsLoading(true);
      setError(null);
    }
    try {
      const response = await onedriveApi.listEntries({ path: apiPath, limit: 200, configId });
      const responsePath = response.path || apiPath || '';
      const safePath = clampPathToRoot(responsePath);

      if (focusPath) {
        setEntries(response.entries);
        setParentPath(response.parent_path || null);
        setCurrentPath(safePath);
        setSelectedEntry(null);
        setSearchTerm('');
      }
      const folders = response.entries.filter((entry) => entry.type === 'folder');
      updateTreeWithFolders(safePath, folders);
    } catch (err) {
      console.error(err);
      if (focusPath) {
        setError(err instanceof Error ? err.message : 'Failed to load OneDrive entries');
      }
    } finally {
      if (focusPath) {
        setIsLoading(false);
      }
    }
  }, [clampPathToRoot, configId, updateTreeWithFolders]);

  useEffect(() => {
    if (!open) return;
    const rawStart = initialPath?.trim() || normalizedRootPath || '';
    const startPath = clampPathToRoot(rawStart);
    setCurrentPath(startPath);
    loadDirectory(startPath);
  }, [open, initialPath, normalizedRootPath, clampPathToRoot, loadDirectory]);

  useEffect(() => {
    if (!open) {
      setSelectedEntry(null);
      setSearchTerm('');
    }
  }, [open]);

  useEffect(() => {
    if (!open) return;
    const handler = (event: KeyboardEvent) => {
      if (event.key === 'Escape') {
        onClose();
      }
    };
    window.addEventListener('keydown', handler);
    return () => window.removeEventListener('keydown', handler);
  }, [open, onClose]);

  const openFolder = useCallback((path: string) => {
    const safePath = clampPathToRoot(path);
    loadDirectory(safePath);
  }, [clampPathToRoot, loadDirectory]);

  const handleToggleNode = useCallback((path: string) => {
    const key = toKey(path);
    setTree((prev) => {
      const node = prev[key] || createNode(path);
      return { ...prev, [key]: { ...node, expanded: !node.expanded } };
    });

    const node = tree[key];
    const safePath = clampPathToRoot(path);
    if (!node || !node.isLoaded) {
      loadDirectory(safePath, { focusPath: false });
    }
  }, [tree, clampPathToRoot, loadDirectory]);

  const renderTree = useCallback((path: string, depth = 0): JSX.Element | null => {
    const key = toKey(path);
    const node = tree[key];
    if (!node) {
      return null;
    }
    return (
      <div key={key} className={depth === 0 ? '' : 'ml-3'}>
        <div className="flex items-center gap-1">
          <button
            type="button"
            onClick={() => handleToggleNode(path)}
            className="text-xs text-gray-500 w-5"
          >
            {node.children.length > 0 || !node.isLoaded ? (node.expanded ? '▾' : '▸') : '•'}
          </button>
          <button
            type="button"
            onClick={() => loadDirectory(path)}
            className={`flex-1 text-left text-sm rounded px-2 py-1 hover:bg-slate-100 ${
              currentPath === path ? 'bg-slate-200 font-semibold' : ''
            }`}
          >
            <span className="truncate">{node.name}</span>
          </button>
        </div>
        {node.expanded && node.children.length > 0 && (
          <div className="ml-4 border-l border-dashed border-gray-200 pl-2">
            {node.children.map((childPath) => renderTree(childPath, depth + 1))}
          </div>
        )}
      </div>
    );
  }, [currentPath, handleToggleNode, loadDirectory, tree]);

  const breadcrumbs = useMemo(() => {
    if (!normalizedRootPath) {
      // Original behavior when no rootPath is set
      const tokens = currentPath.split('/').filter(Boolean);
      const crumbs = [{ label: 'Root', path: '' }];
      tokens.forEach((segment, index) => {
        const path = tokens.slice(0, index + 1).join('/');
        crumbs.push({ label: segment, path });
      });
      return crumbs;
    }

    const rootTokens = normalizedRootPath.split('/').filter(Boolean);
    const tokens = currentPath.split('/').filter(Boolean);
    const rootLabel = deriveName(normalizedRootPath); // e.g., "Master Data"

    const crumbs: { label: string; path: string }[] = [
      { label: rootLabel, path: normalizedRootPath },
    ];

    const fullPath = tokens.join('/');
    const rootPrefix = rootTokens.join('/');

    if (fullPath === rootPrefix) {
      return crumbs; // At root
    }

    if (fullPath.startsWith(rootPrefix + '/')) {
      const relativeTokens = tokens.slice(rootTokens.length);
      relativeTokens.forEach((segment, index) => {
        const fullSegPath = [...rootTokens, ...relativeTokens.slice(0, index + 1)].join('/');
        crumbs.push({ label: segment, path: fullSegPath });
      });
    }

    return crumbs;
  }, [currentPath, normalizedRootPath]);

  const handleEntryClick = (entry: OneDriveEntry) => {
    setSelectedEntry(entry);
  };

  const handleDoubleClick = (entry: OneDriveEntry) => {
    if (entry.type === 'folder') {
      openFolder(entry.path);
    } else {
      onSelect(entry.path);
      onClose();
    }
  };

  const confirmSelection = () => {
    if (!selectedEntry || selectedEntry.type !== 'file') return;
    onSelect(selectedEntry.path);
    onClose();
  };

  if (!open) {
    return null;
  }

  return (
    <div className="fixed inset-0 z-50 flex items-center justify-center bg-black/40">
      <div className="bg-white rounded-xl shadow-2xl w-full max-w-5xl h-[80vh] flex flex-col">
        <div className="px-5 py-4 border-b border-gray-200 flex items-center justify-between">
          <div>
            <h2 className="text-lg font-semibold text-gray-900">Browse OneDrive</h2>
            <p className="text-xs text-gray-500">Select a file to link with this configuration</p>
          </div>
          <button
            type="button"
            onClick={onClose}
            className="text-gray-500 hover:text-gray-700"
            aria-label="Close"
          >
            ✕
          </button>
        </div>

        <div className="px-5 py-3 border-b border-gray-100">
          <nav className="flex flex-wrap items-center gap-1 text-sm text-gray-600">
            {breadcrumbs.map((crumb, index) => (
              <div key={crumb.path || 'root'} className="flex items-center gap-1">
                <button
                  type="button"
                  onClick={() => openFolder(crumb.path)}
                  className={`hover:text-blue-600 ${currentPath === crumb.path ? 'text-blue-600 font-medium' : ''}`}
                >
                  {crumb.label || 'Root'}
                </button>
                {index < breadcrumbs.length - 1 && <span>/</span>}
              </div>
            ))}
          </nav>
        </div>

        <div className="flex flex-1 overflow-hidden">
          <aside className="w-56 border-r border-gray-100 overflow-y-auto p-3 text-sm text-gray-700 bg-slate-50">
            {renderTree(normalizedRootPath || '')}
          </aside>

          <section className="flex-1 flex flex-col">
            <div className="p-4 flex items-center gap-3 border-b border-gray-100">
              <input
                type="text"
                placeholder="Search name..."
                value={searchTerm}
                onChange={(event) => setSearchTerm(event.target.value)}
                className="flex-1 border border-gray-300 rounded-md px-3 py-1.5 text-sm"
              />
              <button
                type="button"
                onClick={() => loadDirectory(currentPath)}
                className="px-3 py-1.5 text-sm font-medium text-gray-700 border border-gray-300 rounded"
              >
                Refresh
              </button>
            </div>

            <div className="flex-1 overflow-auto">
              {isLoading ? (
                <div className="h-full flex items-center justify-center text-gray-500 text-sm">
                  Loading OneDrive entries…
                </div>
              ) : error ? (
                <div className="h-full flex items-center justify-center">
                  <div className="text-center">
                    <p className="text-sm text-red-600">{error}</p>
                    <button
                      type="button"
                      onClick={() => loadDirectory(currentPath)}
                      className="mt-2 text-xs text-blue-600 underline"
                    >
                      Try again
                    </button>
                  </div>
                </div>
              ) : filteredEntries.length === 0 ? (
                <div className="h-full flex items-center justify-center text-sm text-gray-500">
                  No items match the current filters
                </div>
              ) : (
                <table className="min-w-full text-sm">
                  <thead className="sticky top-0 bg-slate-100 text-xs uppercase text-gray-500">
                    <tr>
                      <th className="text-left font-medium px-4 py-2">Name</th>
                      <th className="text-left font-medium px-4 py-2">Type</th>
                      <th className="text-left font-medium px-4 py-2">Last Modified</th>
                      <th className="text-right font-medium px-4 py-2">Size</th>
                    </tr>
                  </thead>
                  <tbody>
                    {filteredEntries.map((entry) => (
                      <tr
                        key={entry.path}
                        onClick={() => handleEntryClick(entry)}
                        onDoubleClick={() => handleDoubleClick(entry)}
                        className={`border-b border-gray-100 cursor-pointer hover:bg-blue-50 ${
                          selectedEntry?.path === entry.path ? 'bg-blue-100' : ''
                        }`}
                      >
                        <td className="px-4 py-2 font-medium text-gray-800">
                          {entry.type === 'folder' ? '📁' : '📄'} {entry.name}
                        </td>
                        <td className="px-4 py-2 capitalize text-gray-500">{entry.type}</td>
                        <td className="px-4 py-2 text-gray-500 text-xs">
                          {entry.last_modified ? new Date(entry.last_modified).toLocaleString() : '—'}
                        </td>
                        <td className="px-4 py-2 text-right text-gray-500 text-xs">
                          {entry.type === 'folder'
                            ? '—'
                            : entry.size
                            ? `${(entry.size / 1024).toFixed(1)} KB`
                            : '—'}
                        </td>
                      </tr>
                    ))}
                  </tbody>
                </table>
              )}
            </div>

            <div className="border-t border-gray-100 px-5 py-3 flex items-center justify-between">
              <div className="text-xs text-gray-500">
                {selectedEntry ? selectedEntry.path : parentPath || 'Root'}
              </div>
              <div className="flex gap-2">
                <button
                  type="button"
                  onClick={onClose}
                  className="px-4 py-1.5 text-sm font-medium text-gray-700 border border-gray-300 rounded"
                >
                  Cancel
                </button>
                <button
                  type="button"
                  onClick={confirmSelection}
                  disabled={!selectedEntry || selectedEntry.type !== 'file'}
                  className="px-4 py-1.5 text-sm font-semibold text-white bg-blue-600 disabled:bg-blue-200 rounded"
                >
                  Use Selected
                </button>
              </div>
            </div>
          </section>
        </div>
      </div>
    </div>
  );
}
