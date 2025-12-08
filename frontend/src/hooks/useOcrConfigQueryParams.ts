'use client';

import { useSearchParams, useRouter } from 'next/navigation';
import { useCallback, useMemo } from 'react';

export interface OcrConfigFilters {
  company_id?: number;
  doc_type_id?: number;
  item_type?: string;
  active?: boolean;
}

export interface OcrConfigQueryParams {
  filters: OcrConfigFilters;
  page: number;
  perPage: number;
  search: string;
  setFilters: (filters: OcrConfigFilters) => void;
  setPage: (page: number) => void;
  setPerPage: (perPage: number) => void;
  setSearch: (search: string) => void;
  clearFilters: () => void;
  limit: number;
  offset: number;
}

/**
 * Hook for managing OCR Config list page query parameters
 * Syncs filters, pagination, and search with URL query string
 */
export function useOcrConfigQueryParams(): OcrConfigQueryParams {
  const searchParams = useSearchParams();
  const router = useRouter();

  // Parse current query params
  const filters = useMemo<OcrConfigFilters>(() => {
    const company_id = searchParams.get('company_id');
    const doc_type_id = searchParams.get('doc_type_id');
    const item_type = searchParams.get('item_type');
    const active = searchParams.get('active');

    return {
      ...(company_id && { company_id: parseInt(company_id, 10) }),
      ...(doc_type_id && { doc_type_id: parseInt(doc_type_id, 10) }),
      ...(item_type && { item_type }),
      ...(active !== null && { active: active === 'true' }),
    };
  }, [searchParams]);

  const page = useMemo(() => {
    const p = searchParams.get('page');
    return p ? Math.max(1, parseInt(p, 10)) : 1;
  }, [searchParams]);

  const perPage = useMemo(() => {
    const pp = searchParams.get('perPage');
    const parsed = pp ? parseInt(pp, 10) : 10;
    return [5, 10, 20].includes(parsed) ? parsed : 10;
  }, [searchParams]);

  const search = useMemo(() => searchParams.get('search') || '', [searchParams]);

  // Compute API parameters
  const limit = perPage;
  const offset = (page - 1) * perPage;

  // Helper to build query string
  const buildQueryString = useCallback(
    (updates: Partial<OcrConfigQueryParams>) => {
      const params = new URLSearchParams();

      // Add filters
      const newFilters = updates.filters || filters;
      if (newFilters.company_id) params.append('company_id', String(newFilters.company_id));
      if (newFilters.doc_type_id) params.append('doc_type_id', String(newFilters.doc_type_id));
      if (newFilters.item_type) params.append('item_type', newFilters.item_type);
      if (newFilters.active !== undefined) params.append('active', String(newFilters.active));

      // Add search
      const newSearch = updates.search !== undefined ? updates.search : search;
      if (newSearch) params.append('search', newSearch);

      // Add pagination
      const newPage = updates.page !== undefined ? updates.page : page;
      const newPerPage = updates.perPage !== undefined ? updates.perPage : perPage;
      if (newPage > 1) params.append('page', String(newPage));
      if (newPerPage !== 10) params.append('perPage', String(newPerPage));

      return params.toString();
    },
    [filters, search, page, perPage]
  );

  // Update functions
  const setFilters = useCallback(
    (newFilters: OcrConfigFilters) => {
      const qs = buildQueryString({ filters: newFilters, page: 1 });
      router.push(`/admin/ocr-configs${qs ? `?${qs}` : ''}`);
    },
    [buildQueryString, router]
  );

  const setPage = useCallback(
    (newPage: number) => {
      const qs = buildQueryString({ page: newPage });
      router.push(`/admin/ocr-configs${qs ? `?${qs}` : ''}`);
    },
    [buildQueryString, router]
  );

  const setPerPage = useCallback(
    (newPerPage: number) => {
      const qs = buildQueryString({ perPage: newPerPage, page: 1 });
      router.push(`/admin/ocr-configs${qs ? `?${qs}` : ''}`);
    },
    [buildQueryString, router]
  );

  const setSearch = useCallback(
    (newSearch: string) => {
      const qs = buildQueryString({ search: newSearch, page: 1 });
      router.push(`/admin/ocr-configs${qs ? `?${qs}` : ''}`);
    },
    [buildQueryString, router]
  );

  const clearFilters = useCallback(() => {
    router.push('/admin/ocr-configs');
  }, [router]);

  return {
    filters,
    page,
    perPage,
    search,
    setFilters,
    setPage,
    setPerPage,
    setSearch,
    clearFilters,
    limit,
    offset,
  };
}
