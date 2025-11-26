// API client for interacting with the backend

export interface Company {
  company_id: number;
  company_name: string;
  company_code: string;
  active: boolean;
  created_at: string;
  updated_at: string;
}

export interface DocumentType {
  doc_type_id: number;
  type_name: string;
  type_code: string;
  description: string | null;
  template_json_path: string | null;
  template_version: string | null;
  has_template: boolean;
  created_at: string;
  updated_at: string;
}

export interface Config {
  config_id: number;
  company_id: number;
  company_name: string;
  doc_type_id: number;
  type_name: string;
  prompt_path: string;
  schema_path: string;
  active: boolean;
  created_at: string;
  updated_at: string;
}

// Base API URL - use Next.js proxy path to avoid CORS issues
const API_BASE_URL = '/api';

// Enhanced error type for better error handling
export interface ApiError extends Error {
  status?: number;
  isDependencyError?: boolean;
  dependencyMessage?: string;
}

// Generic fetch function with enhanced error handling
export async function fetchApi<T>(url: string, options?: RequestInit): Promise<T> {
  const fullUrl = `${API_BASE_URL}${url.startsWith('/') ? url : `/${url}`}`;
  
  console.log(`Fetching from: ${fullUrl}`);
  
  const response = await fetch(fullUrl, {
    ...options,
    headers: {
      ...options?.headers,
    },
  });

  if (!response.ok) {
    const errorText = await response.text();
    console.error('API error:', errorText);
    
    try {
      const errorJson = JSON.parse(errorText);
      const errorMessage = errorJson.detail || 'API request failed';
      
      // Create enhanced error object
      const error = new Error(errorMessage) as ApiError;
      error.status = response.status;
      
      // Check if this is a dependency constraint error
      if (response.status === 400 && errorMessage.includes('Cannot delete')) {
        error.isDependencyError = true;
        error.dependencyMessage = errorMessage;
      }
      
      throw error;
    } catch (parseError) {
      if (parseError instanceof Error && 'isDependencyError' in parseError) {
        throw parseError; // Re-throw if it's already our enhanced error
      }
      
      const error = new Error(`API request failed: ${response.status} ${response.statusText}`) as ApiError;
      error.status = response.status;
      throw error;
    }
  }

  return response.json();
}

export function getFileDownloadUrl(fileId: number): string {
  return `${API_BASE_URL}/download/${fileId}`;
}

// Companies API
export const companiesApi = {
  getAll: () => fetchApi<Company[]>('/companies'),
  getById: (id: number) => fetchApi<Company>(`/companies/${id}`),
  create: (data: Omit<Company, 'company_id' | 'created_at' | 'updated_at'>) =>
    fetchApi<Company>('/companies', {
      method: 'POST',
      headers: { 'Content-Type': 'application/json' },
      body: JSON.stringify(data),
    }),
  update: (id: number, data: Partial<Omit<Company, 'company_id' | 'created_at' | 'updated_at'>>) =>
    fetchApi<Company>(`/companies/${id}`, {
      method: 'PUT',
      headers: { 'Content-Type': 'application/json' },
      body: JSON.stringify(data),
    }),
  delete: (id: number) =>
    fetchApi<{ message: string }>(`/companies/${id}`, {
      method: 'DELETE',
    }),
};

// Document Types API
export const documentTypesApi = {
  getAll: () => fetchApi<DocumentType[]>('/document-types'),
  getById: (id: number) => fetchApi<DocumentType>(`/document-types/${id}`),
  create: (
    data: Omit<
      DocumentType,
      'doc_type_id' | 'created_at' | 'updated_at' | 'template_json_path' | 'template_version' | 'has_template'
    >
  ) =>
    fetchApi<DocumentType>('/document-types', {
      method: 'POST',
      headers: { 'Content-Type': 'application/json' },
      body: JSON.stringify(data),
    }),
  update: (
    id: number,
    data: Partial<
      Omit<
        DocumentType,
        'doc_type_id' | 'created_at' | 'updated_at' | 'template_json_path' | 'template_version' | 'has_template'
      >
    >
  ) =>
    fetchApi<DocumentType>(`/document-types/${id}`, {
      method: 'PUT',
      headers: { 'Content-Type': 'application/json' },
      body: JSON.stringify(data),
    }),
  delete: (id: number) =>
    fetchApi<{ message: string }>(`/document-types/${id}`, {
      method: 'DELETE',
    }),
};

// Configurations API
export const configsApi = {
  getAll: () => fetchApi<Config[]>('/configs'),
  getById: (id: number) => fetchApi<Config>(`/configs/${id}`),
  create: (data: Omit<Config, 'config_id' | 'created_at' | 'updated_at' | 'company_name' | 'type_name'>) =>
    fetchApi<Config>('/configs', {
      method: 'POST',
      headers: { 'Content-Type': 'application/json' },
      body: JSON.stringify(data),
    }),
  update: (id: number, data: Partial<Omit<Config, 'config_id' | 'company_id' | 'doc_type_id' | 'created_at' | 'updated_at' | 'company_name' | 'type_name'>>) =>
    fetchApi<Config>(`/configs/${id}`, {
      method: 'PUT',
      headers: { 'Content-Type': 'application/json' },
      body: JSON.stringify(data),
    }),
  delete: (id: number) =>
    fetchApi<{ message: string }>(`/configs/${id}`, {
      method: 'DELETE',
    }),
};

// File upload API - shared helper for configs, etc.
export async function uploadFile(file: globalThis.File, path: string): Promise<string> {
  const formData = new FormData();
  formData.append('file', file);
  formData.append('path', path);

  const res = await fetchApi<{ file_path: string }>('/upload', {
    method: 'POST',
    body: formData,
  });

  return res.file_path;
}

// Dependency Management Interfaces
export interface DependencyInfo {
  exists: boolean;
  entity_name?: string;
  can_delete: boolean;
  total_dependencies: number;
  dependencies: {
    processing_jobs: number;
    company_configs: number;
    department_access?: number;
  };
  blocking_message?: string;
  detailed_dependencies?: {
    processing_jobs?: Array<{
      job_id: number;
      filename: string;
      status: string;
      company_id?: number;
      created_at: string;
    }>;
  };
  migration_targets?: MigrationTarget[];
}

export interface MigrationTarget {
  id: number;
  name: string;
  code: string;
}

export interface MigrationResult {
  message: string;
  processing_jobs_migrated?: number;
}

// Add this function to your api.ts file
export async function fetchCompaniesForDocType(docTypeId: number): Promise<Company[]> {
  // This endpoint will need to be implemented on the backend
  return fetchApi<Company[]>(`/document-types/${docTypeId}/companies`);
}

// Also make sure this function exists since it's imported in upload/page.tsx
export async function fetchDocumentTypes(): Promise<DocumentType[]> {
  return fetchApi<DocumentType[]>('/document-types');
}

// Add the fetchJobs function
export async function fetchJobs(
  params: { company_id?: number; doc_type_id?: number; status?: string; limit?: number; offset?: number } = {}
): Promise<Job[]> {
  const queryParams = new URLSearchParams();
  
  if (params.company_id) queryParams.append('company_id', params.company_id.toString());
  if (params.doc_type_id) queryParams.append('doc_type_id', params.doc_type_id.toString());
  if (params.status) queryParams.append('status', params.status);
  if (params.limit) queryParams.append('limit', params.limit.toString());
  if (params.offset) queryParams.append('offset', params.offset.toString());
  
  const queryString = queryParams.toString();
  const endpoint = `/jobs${queryString ? `?${queryString}` : ''}`;
  
  return fetchApi<Job[]>(endpoint);
}

// Batch job functions removed - using Orders pipeline instead

// Dependency Management API
export const dependencyApi = {
  // Get company dependencies
  getCompanyDependencies: (companyId: number): Promise<DependencyInfo> =>
    fetchApi<DependencyInfo>(`/companies/${companyId}/dependencies`),
  
  // Get document type dependencies
  getDocumentTypeDependencies: (docTypeId: number): Promise<DependencyInfo> =>
    fetchApi<DependencyInfo>(`/document-types/${docTypeId}/dependencies`),
  
  // Get configuration dependencies
  getConfigDependencies: (configId: number): Promise<DependencyInfo> =>
    fetchApi<DependencyInfo>(`/configs/${configId}/dependencies`),
};

// Force Delete API - Deletes entities and all their dependencies
export const forceDeleteApi = {
  // Force delete company and all its dependencies
  deleteCompanyWithDependencies: (companyId: number): Promise<any> =>
    fetchApi(`/companies/${companyId}/force-delete`, {
      method: 'DELETE',
    }),

  // Force delete document type and all its dependencies
  deleteDocumentTypeWithDependencies: (docTypeId: number): Promise<any> =>
    fetchApi(`/document-types/${docTypeId}/force-delete`, {
      method: 'DELETE',
    }),

  // Force delete configuration and its related files
  deleteConfigWithDependencies: (configId: number): Promise<any> =>
    fetchApi(`/configs/${configId}/force-delete`, {
      method: 'DELETE',
    }),
};

// AWB Processing API
export interface OneDriveSyncRecord {
  sync_id: number;
  last_sync_time: string;
  sync_status: string;
  files_processed: number;
  files_failed: number;
  error_message?: string;
  created_at: string;
  metadata?: Record<string, any>;
}

export interface OneDriveSyncResponse {
  success: boolean;
  syncs: OneDriveSyncRecord[];
}

export interface AWBProcessResponse {
  order_id: number;
  message: string;
}

export const awbApi = {
  // Process monthly AWB files
  processMonthly: (formData: FormData): Promise<AWBProcessResponse> =>
    fetchApi<AWBProcessResponse>('/awb/process-monthly', {
      method: 'POST',
      body: formData,
    }),

  // Get OneDrive sync status
  getSyncStatus: (limit: number = 10): Promise<OneDriveSyncResponse> =>
    fetchApi<OneDriveSyncResponse>(`/awb/sync-status?limit=${limit}`),

  // Trigger manual OneDrive sync with optional parameters
  triggerSync: (options?: {
    month?: string;
    force?: boolean;
    reconcile?: boolean;
    scan_processed?: boolean;
  }): Promise<{ success: boolean; message: string; month?: string; force?: boolean; reconcile?: boolean; scan_processed?: boolean }> => {
    const params = new URLSearchParams();
    if (options?.month) params.append('month', options.month);
    if (options?.force) params.append('force', 'true');
    if (options?.reconcile) params.append('reconcile', 'true');
    if (options?.scan_processed !== undefined) params.append('scan_processed', String(options.scan_processed));

    const queryString = params.toString();
    const url = `/awb/trigger-sync${queryString ? `?${queryString}` : ''}`;

    return fetchApi<{ success: boolean; message: string; month?: string; force?: boolean; reconcile?: boolean; scan_processed?: boolean }>(url, {
      method: 'POST',
    });
  },

};
