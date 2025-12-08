'use client';

import { Suspense, useEffect, useMemo, useState } from 'react';
import { useRouter, useSearchParams } from 'next/navigation';
import type {
  Company,
  DocumentType,
  CompanyDocTypeConfig,
} from '@/lib/api';
import { companiesApi, documentTypesApi, ocrConfigsApi } from '@/lib/api';
import { useOcrConfigQueryParams } from '@/hooks/useOcrConfigQueryParams';
import SmartDeleteDialog from '@/components/ui/SmartDeleteDialog';
import Pagination from '@/components/ui/Pagination';
import type { PaginationInfo } from '@/types/pagination';

function OcrConfigsAdminPageContent() {
  const router = useRouter();
  const searchParams = useSearchParams();
  const queryParams = useOcrConfigQueryParams();

  const [configs, setConfigs] = useState<CompanyDocTypeConfig[]>([]);
  const [companies, setCompanies] = useState<Company[]>([]);
  const [docTypes, setDocTypes] = useState<DocumentType[]>([]);
  const [isLoading, setIsLoading] = useState(true);
  const [error, setError] = useState<string>('');
  const [pagination, setPagination] = useState<PaginationInfo | null>(null);

  const [deleteDialog, setDeleteDialog] = useState<{
    isOpen: boolean;
    entity: { type: 'config'; id: number; name: string } | null;
  }>({ isOpen: false, entity: null });

  const [searchInput, setSearchInput] = useState(queryParams.search);

  const companyLookup = useMemo(() => {
    const map = new Map<number, Company>();
    companies.forEach((c) => map.set(c.company_id, c));
    return map;
  }, [companies]);

  const docTypeLookup = useMemo(() => {
    const map = new Map<number, DocumentType>();
    docTypes.forEach((d) => map.set(d.doc_type_id, d));
    return map;
  }, [docTypes]);

  const getCompanyName = (id: number) =>
    companyLookup.get(id)?.company_name || `ID ${id}`;
  const getDocTypeName = (id: number) =>
    docTypeLookup.get(id)?.type_name || `ID ${id}`;

  // Load data on mount and when query params change
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

        // Fetch configs with pagination
        const response = await ocrConfigsApi.getAll({
          company_id: queryParams.filters.company_id,
          doc_type_id: queryParams.filters.doc_type_id,
          active: queryParams.filters.active,
          search: queryParams.search,
          limit: queryParams.limit,
          offset: queryParams.offset,
        });

        // Handle both old (list) and new (paginated) response shapes
        if (Array.isArray(response)) {
          setConfigs(response);
          setPagination(null);
        } else if (response && typeof response === 'object' && 'data' in response) {
          setConfigs(response.data);
          setPagination(response.pagination);
        }
      } catch (err) {
        console.error(err);
        setError('Failed to load OCR configs');
      } finally {
        setIsLoading(false);
      }
    };

    loadData();
  }, [queryParams.filters, queryParams.search, queryParams.limit, queryParams.offset]);

  const handleSearchChange = (value: string) => {
    setSearchInput(value);
  };

  const handleSearchSubmit = () => {
    queryParams.setSearch(searchInput);
  };

  const handleCreateClick = () => {
    router.push(`/admin/ocr-configs/new?${searchParams.toString()}`);
  };

  const handleEditClick = (config: CompanyDocTypeConfig) => {
    router.push(`/admin/ocr-configs/${config.config_id}?${searchParams.toString()}`);
  };

  const openDeleteDialog = (cfg: CompanyDocTypeConfig) => {
    setDeleteDialog({
      isOpen: true,
      entity: {
        type: 'config',
        id: cfg.config_id,
        name: `${cfg.company_name || cfg.company_id} / ${cfg.doc_type_name || cfg.doc_type_id} / ${cfg.item_type}`,
      },
    });
  };

  const handleDeleteSuccess = () => {
    if (deleteDialog.entity) {
      setConfigs((prev) => prev.filter((c) => c.config_id !== deleteDialog.entity!.id));
    }
    setDeleteDialog({ isOpen: false, entity: null });
  };

  const handleDeleteCancel = () => {
    setDeleteDialog({ isOpen: false, entity: null });
  };

  return (
    <div className="max-w-7xl mx-auto">
      <div className="flex items-center justify-between mb-6">
        <h1 className="text-2xl font-bold">Unified OCR Configs</h1>
        <button
          onClick={handleCreateClick}
          className="bg-blue-600 text-white px-4 py-2 rounded hover:bg-blue-700 text-sm"
        >
          Create New Config
        </button>
      </div>

      <p className="mb-4 text-sm text-gray-600">
        These configs unify prompt/schema paths and mapping defaults per Company + Document
        Type + Item Type. Legacy &quot;Configurations&quot; and &quot;Mapping Templates&quot; remain
        as fallbacks.
      </p>

      {error && (
        <div className="mb-4 bg-red-100 text-red-800 px-4 py-2 rounded text-sm">
          {error}
        </div>
      )}

      {/* Search and Filters */}
      <div className="mb-6 space-y-4">
        {/* Search Bar */}
        <div className="flex gap-2">
          <input
            type="text"
            placeholder="Search by company or document type..."
            value={searchInput}
            onChange={(e) => handleSearchChange(e.target.value)}
            onKeyDown={(e) => e.key === 'Enter' && handleSearchSubmit()}
            className="flex-1 border border-gray-300 rounded px-3 py-2 text-sm"
          />
          <button
            onClick={handleSearchSubmit}
            className="bg-gray-600 text-white px-4 py-2 rounded hover:bg-gray-700 text-sm"
          >
            Search
          </button>
        </div>

        {/* Filter Dropdowns */}
        <div className="grid grid-cols-1 md:grid-cols-4 gap-4">
          <div>
            <label className="block text-xs font-semibold text-gray-600 mb-1">
              Company
            </label>
            <select
              className="w-full border border-gray-300 rounded px-2 py-1 text-sm"
              value={queryParams.filters.company_id || ''}
              onChange={(e) =>
                queryParams.setFilters({
                  ...queryParams.filters,
                  company_id: e.target.value ? parseInt(e.target.value, 10) : undefined,
                })
              }
            >
              <option value="">All</option>
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
              className="w-full border border-gray-300 rounded px-2 py-1 text-sm"
              value={queryParams.filters.doc_type_id || ''}
              onChange={(e) =>
                queryParams.setFilters({
                  ...queryParams.filters,
                  doc_type_id: e.target.value ? parseInt(e.target.value, 10) : undefined,
                })
              }
            >
              <option value="">All</option>
              {docTypes.map((d) => (
                <option key={d.doc_type_id} value={d.doc_type_id}>
                  {d.type_name}
                </option>
              ))}
            </select>
          </div>
          <div>
            <label className="block text-xs font-semibold text-gray-600 mb-1">
              Status
            </label>
            <select
              className="w-full border border-gray-300 rounded px-2 py-1 text-sm"
              value={queryParams.filters.active === undefined ? '' : queryParams.filters.active ? 'true' : 'false'}
              onChange={(e) =>
                queryParams.setFilters({
                  ...queryParams.filters,
                  active: e.target.value === '' ? undefined : e.target.value === 'true',
                })
              }
            >
              <option value="">All</option>
              <option value="true">Active</option>
              <option value="false">Inactive</option>
            </select>
          </div>
          <div className="flex items-end">
            <button
              onClick={queryParams.clearFilters}
              className="w-full px-3 py-1 text-sm border border-gray-300 rounded text-gray-700 hover:bg-gray-100"
            >
              Clear Filters
            </button>
          </div>
        </div>
      </div>

      {/* List */}
      {isLoading ? (
        <div className="text-center py-10 text-gray-500">Loading OCR configs...</div>
      ) : configs.length === 0 ? (
        <div className="text-center py-10 text-gray-500">
          No OCR configs found. Click &quot;Create New Config&quot; to create one.
        </div>
      ) : (
        <div className="space-y-4">
          {configs.map((cfg) => (
            <div
              key={cfg.config_id}
              className="bg-white border border-gray-200 rounded-lg p-4 shadow-sm hover:shadow-md transition-shadow"
            >
              {/* Top Row: Badges and Actions */}
              <div className="flex items-center justify-between mb-3 flex-wrap gap-2">
                <div className="flex items-center gap-2 flex-wrap">
                  <span className="px-2 py-0.5 rounded-full text-xs font-semibold bg-gray-200 text-gray-800">
                    #{cfg.config_id}
                  </span>
                  <span
                    className={`px-2 py-0.5 rounded-full text-xs font-semibold ${
                      cfg.item_type === 'multi_source'
                        ? 'bg-purple-100 text-purple-800'
                        : 'bg-blue-100 text-blue-800'
                    }`}
                  >
                    {cfg.item_type === 'multi_source' ? 'Multi Source' : 'Single Source'}
                  </span>
                  <span
                    className={`px-2 py-0.5 rounded-full text-xs font-semibold ${
                      cfg.active
                        ? 'bg-green-100 text-green-800'
                        : 'bg-red-100 text-red-800'
                    }`}
                  >
                    {cfg.active ? 'Active' : 'Inactive'}
                  </span>
                </div>
                <div className="flex items-center gap-3">
                  <button
                    onClick={() => handleEditClick(cfg)}
                    className="text-indigo-600 hover:text-indigo-900 text-sm font-medium"
                  >
                    Edit
                  </button>
                  <button
                    onClick={() => openDeleteDialog(cfg)}
                    className="text-red-600 hover:text-red-900 text-sm font-medium"
                  >
                    Delete
                  </button>
                </div>
              </div>

              {/* Company and Document Type */}
              <div className="mb-3">
                <div className="text-sm font-semibold text-gray-900">
                  {cfg.company_name || getCompanyName(cfg.company_id)}
                </div>
                <div className="text-sm text-gray-700">
                  {cfg.doc_type_name || getDocTypeName(cfg.doc_type_id)}
                </div>
              </div>

              {/* Paths */}
              <div className="grid grid-cols-1 md:grid-cols-3 gap-3 text-xs text-gray-600">
                <div>
                  <div className="font-semibold text-gray-700 mb-0.5">Master CSV</div>
                  <div
                    className="truncate text-gray-800"
                    title={cfg.master_csv_path || ''}
                  >
                    {cfg.master_csv_path || '—'}
                  </div>
                </div>
                <div>
                  <div className="font-semibold text-gray-700 mb-0.5">Prompt</div>
                  <div
                    className="truncate text-gray-800"
                    title={cfg.prompt_path || ''}
                  >
                    {cfg.prompt_path || '—'}
                  </div>
                </div>
                <div>
                  <div className="font-semibold text-gray-700 mb-0.5">Schema</div>
                  <div
                    className="truncate text-gray-800"
                    title={cfg.schema_path || ''}
                  >
                    {cfg.schema_path || '—'}
                  </div>
                </div>
              </div>
            </div>
          ))}
        </div>
      )}

      {/* Pagination */}
      {pagination && (
        <div className="mt-6 space-y-4">
          <div className="flex items-center gap-2">
            <label className="text-sm text-gray-600">Per page:</label>
            <select
              value={queryParams.perPage}
              onChange={(e) => queryParams.setPerPage(parseInt(e.target.value, 10))}
              className="border border-gray-300 rounded px-2 py-1 text-sm"
            >
              <option value={5}>5</option>
              <option value={10}>10</option>
              <option value={20}>20</option>
            </select>
          </div>

          <Pagination
            pagination={pagination}
            onPageChange={(newPage) => queryParams.setPage(newPage)}
          />
        </div>
      )}

      <SmartDeleteDialog
        isOpen={deleteDialog.isOpen}
        onClose={handleDeleteCancel}
        onSuccess={handleDeleteSuccess}
        entity={deleteDialog.entity!}
      />
    </div>
  );
}

export default function OcrConfigsAdminPage() {
  return (
    <Suspense fallback={<div className="text-center py-10 text-gray-500">Loading...</div>}>
      <OcrConfigsAdminPageContent />
    </Suspense>
  );
}
