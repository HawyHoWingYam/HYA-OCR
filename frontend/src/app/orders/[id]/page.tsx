'use client';

import { useState, useEffect } from 'react';
import { useRouter, useParams } from 'next/navigation';
import Link from 'next/link';
import { DocumentType } from '@/lib/api';
import { applyOrderUpdateToOrder } from '@/lib/orderUpdateHelpers';

interface Company {
  company_id: number;
  company_name: string;
}

interface PrimaryDocTypeInfo {
  doc_type_id: number;
  type_name: string;
  type_code: string;
  template_json_path: string | null;
  template_version?: string | null;
  has_template?: boolean;
}

interface OrderFinalReportPaths {
  mapped_csv?: string;
  special_csv?: string;
  [key: string]: string | undefined;
}

  type MappingAttachmentSource = {
    kind: string;
    path: string;
    label?: string | null;
    metadata?: Record<string, any>;
    // New optional fields aligned with backend schema
    join_key?: string;
    filename_contains?: string;
  };

  interface ItemMappingConfig {
    item_type?: string;
    master_csv_path?: string;
    external_join_keys?: string[];
    internal_join_key?: string;
    column_aliases?: Record<string, string>;
    attachment_sources?: MappingAttachmentSource[];
    notes?: string | null;
    [key: string]: any;
  }

interface OrderItem {
  item_id: number;
  order_id: number;
  company_id: number;
  doc_type_id: number;
  item_name: string;
  status: string;
  item_type: string;
  file_count: number; // Now represents attachments only
  company_name: string;
  doc_type_name: string;
  primary_file?: {
    file_id: number;
    filename: string;
    file_size: number;
    file_type: string;
    uploaded_at: string;
  } | null;
  attachments?: Array<{
    file_id: number;
    filename: string;
    file_size: number;
    file_type: string;
    upload_order: number;
    uploaded_at: string;
  }>;
  attachment_count?: number;
  ocr_result_json_path: string | null;
  ocr_result_csv_path: string | null;
  mapping_config?: ItemMappingConfig | null;
  applied_template_id?: number | null;
  processing_started_at: string | null;
  processing_completed_at: string | null;
  processing_time_seconds: number | null;
  error_message: string | null;
  created_at: string;
  updated_at: string;
}

interface Order {
  order_id: number;
  order_name: string | null;
  status: string;
  total_items: number;
  completed_items: number;
  failed_items: number;
  total_attachments: number;
  completed_attachments: number;
  failed_attachments: number;
  mapping_file_path?: string | null;
  mapping_keys?: string[] | null;
  primary_doc_type_id: number | null;
  primary_doc_type: PrimaryDocTypeInfo | null;
  final_report_paths: OrderFinalReportPaths | null;
  error_message: string | null;
  items: OrderItem[];
  item_mapping_summary?: Array<{
    item_id: number;
    item_type: string | null;
    has_mapping_config: boolean;
    applied_template_id?: number | null;
  }>;
  created_at: string;
  updated_at: string;
}

export default function OrderDetailsPage() {
  const router = useRouter();
  const params = useParams();
  const orderId = parseInt(params.id as string);

  const [order, setOrder] = useState<Order | null>(null);
  const [companies, setCompanies] = useState<Company[]>([]);
  const [documentTypes, setDocumentTypes] = useState<DocumentType[]>([]);
  const [isLoading, setIsLoading] = useState(true);
  const [error, setError] = useState('');

  // Add Item Modal State
  const [showAddItemModal, setShowAddItemModal] = useState(false);
  const [selectedCompany, setSelectedCompany] = useState<number | null>(null);
  const [selectedDocType, setSelectedDocType] = useState<number | null>(null);
  const [itemName, setItemName] = useState('');
  const [newItemType, setNewItemType] = useState<'single_source' | 'multi_source'>('single_source');
  const [isAddingItem, setIsAddingItem] = useState(false);

  // File Upload State
  const [uploadingFiles, setUploadingFiles] = useState<{[key: number]: boolean}>({});
  const [fileInputKeys, setFileInputKeys] = useState<{[key: number]: string}>({});

  // File Delete State
  const [deletingFiles, setDeletingFiles] = useState<{[key: string]: boolean}>({});

  // Item Delete State
  const [deletingItems, setDeletingItems] = useState<{[key: number]: boolean}>({});

  // AWB Month Attach State
  const [awbMonthAttaching, setAwbMonthAttaching] = useState<{[key: number]: boolean}>({});
  const [awbMonths, setAwbMonths] = useState<{[key: number]: string}>({});
  const [modalAwbIncludeBill, setModalAwbIncludeBill] = useState(false);
  const [modalAwbBillFile, setModalAwbBillFile] = useState<File | null>(null);

  // Collapsible File List State
  const [expandedItemFiles, setExpandedItemFiles] = useState<{[key: number]: boolean}>({});

  // Download State
  const [downloadingFiles, setDownloadingFiles] = useState<{[key: string]: boolean}>({});

  const [isStartingMapping, setIsStartingMapping] = useState(false);

  // CSV Merge Modal State - REMOVED
  // const [showCsvMergeModal, setShowCsvMergeModal] = useState(false);
  // const [csvHeaders, setCsvHeaders] = useState<string[]>([]);
  // const [selectedJoinKey, setSelectedJoinKey] = useState('');
  // const [isMergingCsv, setIsMergingCsv] = useState(false);
  // const [mergeHeaderLoading, setMergeHeaderLoading] = useState(false);
  // const [currentItemId, setCurrentItemId] = useState<number | null>(null);

  // Conditional CSV Download State
  const [csvHeaders, setCsvHeaders] = useState<string[]>([]);
  const [selectedJoinKey, setSelectedJoinKey] = useState('');
  const [isLoadingCsvHeaders, setIsLoadingCsvHeaders] = useState(false);

  
  // Smart Recommendations functionality has been removed
  // const [smartRecommendations, setSmartRecommendations] = useState<{[key: number]: any[]}>({});
  // const [loadingRecommendations, setLoadingRecommendations] = useState<{[key: number]: boolean}>({});
  // const [orderLevelSuggestions, setOrderLevelSuggestions] = useState<any[]>([]);
  // const [loadingOrderSuggestions, setLoadingOrderSuggestions] = useState(false);

  // Order Management State
  const [isLockingOrder, setIsLockingOrder] = useState(false);
  const [isUnlockingOrder, setIsUnlockingOrder] = useState(false);
  const [isRestartingOcr, setIsRestartingOcr] = useState(false);
  const [isRestartingMapping, setIsRestartingMapping] = useState(false);
  // Auto-refresh watcher when background processing is running
  const [isWatchingProcessing, setIsWatchingProcessing] = useState(false);
  const processingPollRef = { current: null as any };

  // Mapping configuration modal state
  const [mappingModalItem, setMappingModalItem] = useState<OrderItem | null>(null);
  const [mappingForm, setMappingForm] = useState({
    item_type: 'single_source',
    master_csv_path: '',
    external_join_keys: '',
    internal_join_key: '',
    column_aliases: '',
    inherit_defaults: true,
    attachment_sources: [] as Array<{ path?: string; filename_contains?: string; join_key?: string; label?: string; metadata?: string }>,
  });
  const [mappingFormError, setMappingFormError] = useState('');
  const [isSavingMappingConfig, setIsSavingMappingConfig] = useState(false);
  const [csvPreview, setCsvPreview] = useState<{ headers: string[]; row_count: number } | null>(null);
  const [isPreviewingCsv, setIsPreviewingCsv] = useState(false);
  // Primary JSON deep-flattened headers preview
  const [primaryPreview, setPrimaryPreview] = useState<string[] | null>(null);
  const [isPreviewingPrimary, setIsPreviewingPrimary] = useState(false);

  useEffect(() => {
    loadOrder();
    loadCompanies();
    loadDocumentTypes();
  }, [orderId]);

  const loadOrder = async () => {
    try {
      const response = await fetch(`/api/orders/${orderId}`);
      if (!response.ok) {
        throw new Error('Failed to fetch order');
      }
      const data = await response.json();
      setOrder(data);

      setError('');
    } catch (error) {
      console.error('Error fetching order:', error);
      setError('Failed to load order');
    } finally {
      setIsLoading(false);
    }
  };

  const startProcessingWatcher = () => {
    try {
      if (processingPollRef.current) {
        clearInterval(processingPollRef.current);
        processingPollRef.current = null;
      }
      setIsWatchingProcessing(true);
      processingPollRef.current = setInterval(async () => {
        try {
          const resp = await fetch(`/api/orders/${orderId}`);
          if (!resp.ok) return;
          const latest = await resp.json();
          setOrder(latest);
          const s = latest.status as string;
          if (s !== 'PROCESSING' && s !== 'MAPPING') {
            clearInterval(processingPollRef.current);
            processingPollRef.current = null;
            setIsWatchingProcessing(false);
          }
        } catch {}
      }, 2500);
    } catch {
      setIsWatchingProcessing(false);
    }
  };

  // Prefer WebSocket over polling when API supports it
  const startProcessingWebSocket = () => {
    try {
      const httpBase = (process.env.NEXT_PUBLIC_API_URL || (process as any).env?.API_BASE_URL || 'http://localhost:8000');
      const apiUrl = new URL(httpBase);
      const wsProtocol = apiUrl.protocol === 'https:' ? 'wss:' : 'ws:';
      const wsUrl = `${wsProtocol}//${apiUrl.host}/ws/orders/${orderId}`;
      const ws = new WebSocket(wsUrl);
      ws.onopen = () => {
        // console.log('WS connected for order', orderId);
      };
      ws.onmessage = (event) => {
        try {
          const msg = JSON.parse(event.data);
          if (msg && msg.type === 'order_update' && msg.order_id === orderId) {
            // Shallow merge into current order state if available
            setOrder((prev) => {
              if (!prev) return prev;
              return applyOrderUpdateToOrder(prev, msg);
            });
            if (msg.status && msg.status !== 'PROCESSING' && msg.status !== 'MAPPING') {
              ws.close();
            }
          }
        } catch {}
      };
      ws.onerror = () => {
        // fallback to polling on error
        startProcessingWatcher();
      };
      ws.onclose = () => {
        // If closed while still running, fallback to polling
        setTimeout(() => {
          if (order && (order.status === 'PROCESSING' || order.status === 'MAPPING')) {
            startProcessingWatcher();
          }
        }, 1000);
      };
    } catch {
      startProcessingWatcher();
    }
  };

  const loadCompanies = async () => {
    try {
      const response = await fetch(`/api/companies`);
      if (!response.ok) throw new Error('Failed to fetch companies');
      const data = await response.json();
      setCompanies(data);
    } catch (error) {
      console.error('Error fetching companies:', error);
    }
  };

  const loadDocumentTypes = async () => {
    try {
      const response = await fetch(`/api/document-types`);
      if (!response.ok) throw new Error('Failed to fetch document types');
      const data = await response.json();
      setDocumentTypes(data);
    } catch (error) {
      console.error('Error fetching document types:', error);
    }
  };

    const addOrderItem = async () => {
    if (!selectedCompany || !selectedDocType) {
      setError('Please select both company and document type');
      return;
    }

    setIsAddingItem(true);
    try {
      const response = await fetch(`/api/orders/${orderId}/items`, {
        method: 'POST',
        headers: {
          'Content-Type': 'application/json',
        },
        body: JSON.stringify({
          company_id: selectedCompany,
          doc_type_id: selectedDocType,
          item_name: itemName || undefined,
          item_type: newItemType,
        }),
      });

      if (!response.ok) {
        throw new Error('Failed to add item');
      }

      // Reset form and close modal
      setSelectedCompany(null);
      setSelectedDocType(null);
      setItemName('');
      setNewItemType('single_source');
      setShowAddItemModal(false);

      // Reload order to show new item
      loadOrder();
    } catch (error) {
      console.error('Error adding item:', error);
      setError('Failed to add item');
    } finally {
      setIsAddingItem(false);
    }
  };

  const uploadFilesToItem = async (itemId: number, files: FileList) => {
    setUploadingFiles(prev => ({ ...prev, [itemId]: true }));

    try {
      const formData = new FormData();
      Array.from(files).forEach(file => {
        formData.append('files', file);
      });

      const response = await fetch(`/api/orders/${orderId}/items/${itemId}/files`, {
        method: 'POST',
        body: formData,
      });

      if (!response.ok) {
        throw new Error('Failed to upload files');
      }

      // Reload order to show updated file counts
      loadOrder();

      // Reset file input by changing its key to force re-render
      setFileInputKeys(prev => ({ ...prev, [itemId]: Date.now().toString() }));
    } catch (error) {
      console.error('Error uploading files:', error);
      setError('Failed to upload files');
    } finally {
      setUploadingFiles(prev => ({ ...prev, [itemId]: false }));
    }
  };

  // Upload primary file for an item (single file; used for the first/primary document)
  const uploadPrimaryFile = async (itemId: number, file: File) => {
    setUploadingFiles(prev => ({ ...prev, [itemId]: true }));

    try {
      const formData = new FormData();
      formData.append('file', file);

      const response = await fetch(`/api/orders/${orderId}/items/${itemId}/primary-file`, {
        method: 'POST',
        body: formData,
      });

      if (!response.ok) {
        throw new Error('Failed to upload primary file');
      }

      // Reload order to show updated files
      loadOrder();
    } catch (error) {
      console.error('Error uploading primary file:', error);
      setError('Failed to upload primary file');
    } finally {
      setUploadingFiles(prev => ({ ...prev, [itemId]: false }));
    }
  };

  // Upload multiple files for an item:
  // - First file becomes the primary file (if none exists yet)
  // - Remaining files are uploaded as attachments
  const uploadPrimaryAndAttachments = async (item: OrderItem, files: FileList) => {
    if (!files || files.length === 0) return;

    const [first, ...rest] = Array.from(files);
    setUploadingFiles(prev => ({ ...prev, [item.item_id]: true }));

    try {
      // If there's no primary file yet, use the first file as primary; otherwise treat all as attachments
      if (!item.primary_file) {
        await uploadPrimaryFile(item.item_id, first);
      } else {
        // If primary already exists, push the first file into attachments as well
        rest.unshift(first);
      }

      if (rest.length > 0) {
        const formData = new FormData();
        for (const f of rest) {
          formData.append('files', f);
        }

        const response = await fetch(`/api/orders/${orderId}/items/${item.item_id}/files`, {
          method: 'POST',
          body: formData,
        });

        if (!response.ok) {
          const errorData = await response.json().catch(() => ({}));
          throw new Error(errorData.detail || 'Failed to upload attachment files');
        }
      }

      // Reload order to show updated files
      loadOrder();
    } catch (error) {
      console.error('Error uploading primary/attachment files:', error);
      setError(error instanceof Error ? error.message : 'Failed to upload files');
    } finally {
      setUploadingFiles(prev => ({ ...prev, [item.item_id]: false }));
    }
  };

  // Replace existing primary + attachment files with a new multi-file set
  const replacePrimaryAndAttachments = async (item: OrderItem, files: FileList) => {
    if (!files || files.length === 0) return;

    setUploadingFiles(prev => ({ ...prev, [item.item_id]: true }));

    try {
      // 1) Delete existing primary file if present
      if (item.primary_file) {
        const resp = await fetch(`/api/orders/${orderId}/items/${item.item_id}/primary-file`, {
          method: 'DELETE',
        });
        if (!resp.ok) {
          const data = await resp.json().catch(() => ({}));
          throw new Error(data.detail || 'Failed to delete primary file before replace');
        }
      }

      // 2) Delete all existing attachments for this item
      if (item.attachments && item.attachments.length > 0) {
        for (const att of item.attachments) {
          const resp = await fetch(`/api/orders/${orderId}/items/${item.item_id}/files/${att.file_id}`, {
            method: 'DELETE',
          });
          if (!resp.ok) {
            const data = await resp.json().catch(() => ({}));
            throw new Error(data.detail || `Failed to delete attachment ${att.filename}`);
          }
        }
      }

      // 3) Upload new multi-file set as if first-time upload
      const emptyItem: OrderItem = {
        ...item,
        primary_file: null,
        attachments: [],
        attachment_count: 0,
      };
      await uploadPrimaryAndAttachments(emptyItem, files);
    } catch (error) {
      console.error('Error replacing primary/attachment files:', error);
      setError(error instanceof Error ? error.message : 'Failed to replace files');
    } finally {
      setUploadingFiles(prev => ({ ...prev, [item.item_id]: false }));
    }
  };

  // Delete primary file from an item
  const deletePrimaryFile = async (itemId: number) => {
    setUploadingFiles(prev => ({ ...prev, [itemId]: true }));

    try {
      const response = await fetch(`/api/orders/${orderId}/items/${itemId}/primary-file`, {
        method: 'DELETE',
      });

      if (!response.ok) {
        throw new Error('Failed to delete primary file');
      }

      // Reload order to show updated files
      loadOrder();
    } catch (error) {
      console.error('Error deleting primary file:', error);
      setError('Failed to delete primary file');
    } finally {
      setUploadingFiles(prev => ({ ...prev, [itemId]: false }));
    }
  };

  // Download attachment JSON result
  const downloadAttachmentJson = async (itemId: number, fileId: number) => {
    const downloadKey = `${itemId}-${fileId}-json`;
    setDownloadingFiles(prev => ({ ...prev, [downloadKey]: true }));

    try {
      const response = await fetch(`/api/orders/${orderId}/items/${itemId}/files/${fileId}/download/json`);

      if (!response.ok) {
        const errorData = await response.json().catch(() => ({}));
        throw new Error(errorData.detail || 'Failed to download attachment JSON');
      }

      const data = await response.json();

      // Create download link
      const element = document.createElement('a');
      element.setAttribute('href', 'data:text/json;charset=utf-8,' + encodeURIComponent(JSON.stringify(data.json_data, null, 2)));
      element.setAttribute('download', `item_${itemId}_file_${fileId}_result.json`);
      element.style.display = 'none';
      document.body.appendChild(element);
      element.click();
      document.body.removeChild(element);
    } catch (error) {
      console.error('Error downloading attachment JSON:', error);
      setError(error instanceof Error ? error.message : 'Failed to download attachment JSON');
    } finally {
      setDownloadingFiles(prev => ({ ...prev, [downloadKey]: false }));
    }
  };

  // Download attachment CSV result
  const downloadAttachmentCsv = async (itemId: number, fileId: number) => {
    const downloadKey = `${itemId}-${fileId}-csv`;
    setDownloadingFiles(prev => ({ ...prev, [downloadKey]: true }));

    try {
      const response = await fetch(`/api/orders/${orderId}/items/${itemId}/files/${fileId}/download/csv`);

      if (!response.ok) {
        const errorData = await response.json().catch(() => ({}));
        throw new Error(errorData.detail || 'Failed to download attachment CSV');
      }

      const blob = await response.blob();
      const url = window.URL.createObjectURL(blob);
      const link = document.createElement('a');
      link.href = url;

      // Get filename from response headers
      const contentDisposition = response.headers.get('content-disposition');
      let filename = `item_${itemId}_file_${fileId}_result.csv`;

      if (contentDisposition) {
        const filenameMatch = contentDisposition.match(/filename[^;=\n]*=((['"]).*?\2|[^;\n]*)/);
        if (filenameMatch) {
          filename = filenameMatch[1].replace(/['"]/g, '');
        }
      }

      link.download = filename;
      document.body.appendChild(link);
      link.click();

      // Cleanup
      document.body.removeChild(link);
      window.URL.revokeObjectURL(url);
    } catch (error) {
      console.error('Error downloading attachment CSV:', error);
      setError(error instanceof Error ? error.message : 'Failed to download attachment CSV');
    } finally {
      setDownloadingFiles(prev => ({ ...prev, [downloadKey]: false }));
    }
  };

  // CSV Merge Functions REMOVED - replaced with conditional download

  // Load CSV headers for join key selection
  const loadCsvHeaders = async (itemId: number) => {
    setIsLoadingCsvHeaders(true);
    try {
      const { fetchCsvHeaders } = await import('@/lib/api-csv');
      const headers = await fetchCsvHeaders(orderId, itemId);
      setCsvHeaders(headers.headers);
      setSelectedJoinKey(headers.headers.length > 0 ? headers.headers[0] : '');
    } catch (error) {
      console.error('Error loading CSV headers:', error);
      setError(error instanceof Error ? error.message : 'Failed to load CSV headers');
    } finally {
      setIsLoadingCsvHeaders(false);
    }
  };

  const downloadMergedCsv = async (itemId: number, joinKey: string) => {
    if (!joinKey) {
      setError('Please select a join key');
      return;
    }

    const downloadKey = `merge-${itemId}`;
    setDownloadingFiles(prev => ({ ...prev, [downloadKey]: true }));

    try {
      const { downloadMergedCsv } = await import('@/lib/api-csv');
      await downloadMergedCsv(orderId, itemId, joinKey, setError);
    } catch (error) {
      console.error('Error downloading merged CSV:', error);
      setError(error instanceof Error ? error.message : 'Failed to download merged CSV');
    } finally {
      setDownloadingFiles(prev => ({ ...prev, [downloadKey]: false }));
    }
  };

  // Return all headers for join-key selection so users can choose any column
  const getJoinKeyOptions = (): string[] => {
    return csvHeaders;
  };

  const attachAwbMonth = async (itemId: number) => {
    const month = awbMonths[itemId];
    if (!month) {
      setError('Please select a month');
      return;
    }

    setAwbMonthAttaching(prev => ({ ...prev, [itemId]: true }));

    try {
      const formData = new FormData();
      formData.append('month', month);

      const response = await fetch(`/api/orders/${orderId}/items/${itemId}/awb/attach-month`, {
        method: 'POST',
        body: formData,
      });

      if (!response.ok) {
        const errorData = await response.json().catch(() => ({}));
        throw new Error(errorData.detail || 'Failed to attach AWB month');
      }

      const result = await response.json();

      // Show success message
      setError(`✅ Attached ${result.added_files} files from ${month} (${result.skipped_duplicates} duplicates skipped)`);
      setTimeout(() => setError(''), 5000);

      // Reset form
      setAwbMonths(prev => ({ ...prev, [itemId]: '' }));

      // Reload order to show updated file counts
      loadOrder();
    } catch (error) {
      console.error('Error attaching AWB month:', error);
      setError(error instanceof Error ? error.message : 'Failed to attach AWB month');
    } finally {
      setAwbMonthAttaching(prev => ({ ...prev, [itemId]: false }));
    }
  };

  const openMappingModal = (item: OrderItem) => {
    const config = item.mapping_config || undefined;
    const baseType = (config?.item_type || item.item_type || 'single_source').toLowerCase();
    setMappingForm({
      item_type: baseType,
      master_csv_path: config?.master_csv_path || '',
      external_join_keys: (config?.external_join_keys || []).join(', '),
      internal_join_key: config?.internal_join_key || '',
      column_aliases: config?.column_aliases
        ? Object.entries(config.column_aliases)
            .map(([left, right]) => `${left}:${right}`)
            .join(', ')
        : '',
      inherit_defaults: !config,
      attachment_sources: (config?.attachment_sources || []).map((s: any) => ({
        path: s?.path || '',
        filename_contains: s?.filename_contains || '',
        join_key: s?.join_key || '',
        label: s?.label || '',
        metadata: s?.metadata ? JSON.stringify(s.metadata) : '',
      })),
    });
    setMappingFormError('');
    setMappingModalItem(item);
  };

  const closeMappingModal = () => {
    setMappingModalItem(null);
    setMappingFormError('');
  };

  const handleMappingFormChange = <K extends keyof typeof mappingForm>(field: K, value: typeof mappingForm[K]) => {
    setMappingForm((prev) => ({ ...prev, [field]: value }));
  };

  const parseColumnAliasInput = (value: string) => {
    const aliases: Record<string, string> = {};
    value
      .split(',')
      .map((token) => token.trim())
      .filter(Boolean)
      .forEach((token) => {
        const [left, right] = token.split(':').map((part) => part.trim());
        if (left && right) {
          aliases[left] = right;
        }
      });
    return aliases;
  };

  const saveMappingConfig = async () => {
    if (!mappingModalItem) {
      return;
    }

    if (!mappingForm.inherit_defaults) {
      if (!mappingForm.master_csv_path.trim()) {
        setMappingFormError('Master CSV path is required.');
        return;
      }

      if (mappingForm.item_type === 'multi_source') {
        const hasDefaultInternal = !!mappingForm.internal_join_key.trim();
        const cleanedRules = (mappingForm.attachment_sources || []).filter(r => (r.path || r.join_key || r.filename_contains));
        const hasAnyRule = cleanedRules.some(r => (r.join_key || '').trim().length > 0);
        if (!hasDefaultInternal && !hasAnyRule) {
          setMappingFormError('Provide a default internal join key or at least one attachment rule with join key.');
          return;
        }
        // If rules provided, require OneDrive path
        const missingPath = cleanedRules.some(r => !(r.path || '').trim().length);
        if (cleanedRules.length > 0 && missingPath) {
          setMappingFormError('Each attachment rule must include a OneDrive path.');
          return;
        }
      }
    }

    const payload: any = {
      item_type: mappingForm.item_type,
      inherit_defaults: mappingForm.inherit_defaults,
    };

    if (!mappingForm.inherit_defaults) {
      payload.mapping_config = {
        item_type: mappingForm.item_type,
        master_csv_path: mappingForm.master_csv_path.trim(),
        external_join_keys: mappingForm.external_join_keys
          .split(',')
          .map((key) => key.trim())
          .filter(Boolean),
        column_aliases: parseColumnAliasInput(mappingForm.column_aliases),
      };

      if (mappingForm.item_type === 'multi_source') {
        if (mappingForm.internal_join_key.trim()) {
          payload.mapping_config.internal_join_key = mappingForm.internal_join_key.trim();
        }
        if ((mappingForm.attachment_sources || []).length > 0) {
          // Validate metadata JSON
          for (const r of mappingForm.attachment_sources) {
            const text = (r.metadata || '').trim();
            if (text.length > 0) {
              try {
                const parsed = JSON.parse(text);
                if (!(parsed && typeof parsed === 'object' && !Array.isArray(parsed))) {
                  throw new Error('Metadata must be a JSON object');
                }
              } catch (e) {
                setMappingFormError('Invalid metadata JSON in attachment rules. Provide a valid JSON object.');
                setIsSavingMappingConfig(false);
                return;
              }
            }
          }

          const cleaned = (mappingForm.attachment_sources || [])
            .filter(r => (r.path || '').trim().length > 0 || (r.join_key || '').trim().length > 0 || (r.filename_contains || '').trim().length > 0)
            .map(r => {
              const obj: any = {
                kind: 'onedrive',
                path: (r.path || '').trim(),
                join_key: (r.join_key || '').trim() || undefined,
                filename_contains: (r.filename_contains || '').trim() || undefined,
              };
              const label = (r.label || '').trim();
              if (label) obj.label = label;
              const metaText = (r.metadata || '').trim();
              if (metaText) {
                try {
                  const parsed = JSON.parse(metaText);
                  if (parsed && typeof parsed === 'object' && !Array.isArray(parsed)) {
                    obj.metadata = parsed;
                  }
                } catch {}
              }
              return obj;
            })
            .filter(r => r.path.length > 0);

          if (cleaned.length > 0) {
            payload.mapping_config.attachment_sources = cleaned;
          }
        }
      }
    }

    setIsSavingMappingConfig(true);
    setMappingFormError('');
    try {
      const response = await fetch(`/api/orders/${orderId}/items/${mappingModalItem.item_id}/mapping-config`, {
        method: 'PUT',
        headers: {
          'Content-Type': 'application/json',
        },
        body: JSON.stringify(payload),
      });

      if (!response.ok) {
        const errorData = await response.json().catch(() => ({}));
        throw new Error(errorData.detail || 'Failed to save mapping configuration');
      }

      closeMappingModal();
      await loadOrder();
    } catch (error) {
      console.error('Error saving mapping configuration:', error);
      setMappingFormError(
        error instanceof Error ? error.message : 'Failed to save mapping configuration'
      );
    } finally {
      setIsSavingMappingConfig(false);
    }
  };

  const previewMasterCsv = async () => {
    if (!mappingForm.master_csv_path.trim()) {
      setMappingFormError('Please provide a Master CSV path to preview columns.');
      return;
    }
    setIsPreviewingCsv(true);
    setMappingFormError('');
    try {
      const resp = await fetch(`/api/mapping/master-csv/preview?path=${encodeURIComponent(mappingForm.master_csv_path.trim())}`);
      if (!resp.ok) {
        const data = await resp.json().catch(() => ({}));
        throw new Error(data.detail || 'Failed to preview master CSV');
      }
      const data = await resp.json();
      setCsvPreview({ headers: data.headers || [], row_count: data.row_count || 0 });
    } catch (err) {
      console.error('Preview master CSV error:', err);
      setCsvPreview(null);
      setMappingFormError(err instanceof Error ? err.message : 'Failed to preview master CSV');
    } finally {
      setIsPreviewingCsv(false);
    }
  };

  const previewPrimaryHeaders = async () => {
    if (!mappingModalItem) return;
    setIsPreviewingPrimary(true);
    setMappingFormError('');
    try {
      const resp = await fetch(`/api/orders/${orderId}/items/${mappingModalItem.item_id}/primary/csv/headers`);
      if (!resp.ok) {
        const data = await resp.json().catch(() => ({}));
        throw new Error(data.detail || 'Failed to load primary headers');
      }
      const data = await resp.json();
      const headers: string[] = Array.isArray(data.headers) ? data.headers : [];
      setPrimaryPreview(headers);
    } catch (err) {
      console.error('Preview primary headers error:', err);
      setPrimaryPreview(null);
      setMappingFormError(err instanceof Error ? err.message : 'Failed to load primary headers');
    } finally {
      setIsPreviewingPrimary(false);
    }
  };

  const parseExternalJoinKeys = (): string[] =>
    mappingForm.external_join_keys
      .split(',')
      .map((k) => k.trim())
      .filter(Boolean);

  const toggleExternalJoinKey = (key: string) => {
    const keys = new Set(parseExternalJoinKeys());
    if (keys.has(key)) {
      keys.delete(key);
    } else {
      keys.add(key);
    }
    handleMappingFormChange('external_join_keys', Array.from(keys).join(', '));
  };

  const addAttachmentRule = () => {
    const next = [...mappingForm.attachment_sources, { path: '', filename_contains: '', join_key: '', label: '', metadata: '' }];
    handleMappingFormChange('attachment_sources', next);
  };
  const updateAttachmentRule = (index: number, field: 'path' | 'filename_contains' | 'join_key' | 'label' | 'metadata', value: string) => {
    const next = mappingForm.attachment_sources.slice();
    next[index] = { ...next[index], [field]: value };
    handleMappingFormChange('attachment_sources', next);
  };
  const removeAttachmentRule = (index: number) => {
    const next = mappingForm.attachment_sources.slice();
    next.splice(index, 1);
    handleMappingFormChange('attachment_sources', next);
  };

  const formatMappingType = (value: string) => {
    return value === 'multi_source' ? 'Multiple Source' : 'Single Source';
  };

  const deleteFile = async (itemId: number, fileId: number, fileName: string) => {
    if (!window.confirm(`Are you sure you want to delete "${fileName}"?`)) {
      return;
    }

    const deleteKey = `${itemId}-${fileId}`;
    setDeletingFiles(prev => ({ ...prev, [deleteKey]: true }));

    try {
      const response = await fetch(`/api/orders/${orderId}/items/${itemId}/files/${fileId}`, {
        method: 'DELETE',
      });

      if (!response.ok) {
        throw new Error('Failed to delete file');
      }

      // Reload order to show updated file counts and lists
      loadOrder();

      // Reset file input by changing its key to force re-render and allow same filename re-upload
      setFileInputKeys(prev => ({ ...prev, [itemId]: Date.now().toString() }));
    } catch (error) {
      console.error('Error deleting file:', error);
      setError('Failed to delete file');
    } finally {
      setDeletingFiles(prev => ({ ...prev, [deleteKey]: false }));
    }
  };

  const deleteItem = async (itemId: number, itemName: string) => {
    if (!window.confirm(`Are you sure you want to delete "${itemName}" and all its files?`)) {
      return;
    }

    setDeletingItems(prev => ({ ...prev, [itemId]: true }));

    try {
      const response = await fetch(`/api/orders/${orderId}/items/${itemId}`, {
        method: 'DELETE',
      });

      if (!response.ok) {
        throw new Error('Failed to delete item');
      }

      // Reload order to show updated item counts and lists
      loadOrder();
    } catch (error) {
      console.error('Error deleting item:', error);
      setError('Failed to delete item');
    } finally {
      setDeletingItems(prev => ({ ...prev, [itemId]: false }));
    }
  };

  const downloadItemResult = async (
    itemId: number,
    format: 'json' | 'csv',
    itemName: string,
    scope: 'primary' | 'item' = 'item'
  ) => {
    // Use different keys for primary vs aggregated item downloads so their states don't conflict
    const keyPrefix = scope === 'primary' ? 'primary' : 'item';
    const downloadKey = `${keyPrefix}-${itemId}-${format}`;
    setDownloadingFiles(prev => ({ ...prev, [downloadKey]: true }));

    try {
      // Route to the appropriate backend endpoint based on requested scope
      const path =
        scope === 'primary'
          ? `/api/orders/${orderId}/items/${itemId}/primary/download/${format}`
          : `/api/orders/${orderId}/items/${itemId}/download/${format}`;

      const response = await fetch(path);

      if (!response.ok) {
        const errorData = await response.json().catch(() => ({}));
        throw new Error(errorData.detail || `Failed to download ${format.toUpperCase()} file`);
      }

      // Create download link
      const blob = await response.blob();
      const url = window.URL.createObjectURL(blob);
      const link = document.createElement('a');
      link.href = url;

      // Get filename from response headers or create default
      const contentDisposition = response.headers.get('content-disposition');
      let filename = `order_${orderId}_item_${itemId}_${itemName || 'result'}.${format}`;

      if (contentDisposition) {
        // Prefer RFC 5987 filename* (supports UTF-8 and URL-encoding)
        const starMatch = contentDisposition.match(/filename\*\s*=\s*([^;]+)/i);
        if (starMatch) {
          let value = starMatch[1].trim().replace(/^['"]|['"]$/g, '');

          // Strip UTF-8 prefix if present: utf-8''<urlencoded-filename>
          if (value.toLowerCase().startsWith("utf-8''")) {
            value = value.substring("utf-8''".length);
          }

          try {
            filename = decodeURIComponent(value);
          } catch {
            // Fallback: use raw value if decode fails
            filename = value;
          }
        } else {
          // Fallback to legacy filename= parsing
          const filenameMatch = contentDisposition.match(/filename[^;=\n]*=((['"]).*?\2|[^;\n]*)/i);
          if (filenameMatch) {
            filename = filenameMatch[1].replace(/['"]/g, '');
          }
        }
      }

      link.download = filename;
      document.body.appendChild(link);
      link.click();

      // Cleanup
      document.body.removeChild(link);
      window.URL.revokeObjectURL(url);

    } catch (error) {
      console.error(`Error downloading ${format} file:`, error);
      setError(error instanceof Error ? error.message : `Failed to download ${format.toUpperCase()} file`);
    } finally {
      setDownloadingFiles(prev => ({ ...prev, [downloadKey]: false }));
    }
  };

  const submitOrder = async () => {
    try {
      const response = await fetch(`/api/orders/${orderId}/submit`, {
        method: 'POST',
      });

      if (!response.ok) {
        const errorData = await response.json();
        throw new Error(errorData.detail || 'Failed to submit order');
      }

      loadOrder();
      startProcessingWebSocket();
    } catch (error) {
      console.error('Error submitting order:', error);
      setError(error instanceof Error ? error.message : 'Failed to submit order');
    }
  };

  const startOcrOnlyProcessing = async () => {
    try {
      const response = await fetch(`/api/orders/${orderId}/process-ocr-only`, {
        method: 'POST',
      });

      if (!response.ok) {
        const errorData = await response.json();
        throw new Error(errorData.detail || 'Failed to start OCR-only processing');
      }

      loadOrder();
      startProcessingWebSocket();
    } catch (error) {
      console.error('Error starting OCR-only processing:', error);
      setError(error instanceof Error ? error.message : 'Failed to start OCR-only processing');
    }
  };

  const startMappingProcessing = async () => {
    setIsStartingMapping(true);
    setError('');
    try {
      const response = await fetch(`/api/orders/${orderId}/process-mapping`, {
        method: 'POST',
      });

      if (!response.ok) {
        const errorData = await response.json();
        throw new Error(errorData.detail || 'Failed to start mapping processing');
      }

      // Show success feedback
      setError('✅ Mapping processing started successfully');
      setTimeout(() => setError(''), 3000);

      loadOrder();
      startProcessingWebSocket();
    } catch (error) {
      console.error('Error starting mapping processing:', error);
      setError(error instanceof Error ? error.message : 'Failed to start mapping processing');
    } finally {
      setIsStartingMapping(false);
    }
  };

  
  // Smart recommendations functionality has been removed
  const loadSmartRecommendations = async (itemId: number, csvHeaders?: string[]) => {
    // This functionality has been removed - mapping key recommender is no longer available
    console.log('Mapping key recommender functionality has been removed');
  };

  const loadOrderLevelSuggestions = async () => {
    // This functionality has been removed - mapping key recommender is no longer available
    console.log('Order-level suggestions functionality has been removed');
    // setLoadingOrderSuggestions(false); // Commented out as state is removed
  };

  // Smart recommendation functionality has been removed
  const applySmartRecommendation = (itemId: number, recommendedKey: string, keyIndex: number) => {
    console.log('Smart recommendation functionality has been removed');
  };

  const applyOrderLevelSuggestion = (keyIndex: number, suggestedKey: string) => {
    // No-op: order-level mapping keys workflow has been removed
    console.log('Order-level suggestion ignored');
  };

  
  
  const downloadFinalMappedResults = async (format: 'csv' | 'special-csv') => {
    const downloadKey = format === 'special-csv' ? 'final-special-csv' : `final-${format}`;
    setDownloadingFiles(prev => ({ ...prev, [downloadKey]: true }));

    try {
      const endpoint = format === 'special-csv' ? 'special-csv' : `mapped-${format}`;
      const response = await fetch(`/api/orders/${orderId}/download/${endpoint}`);

      if (!response.ok) {
        const errorData = await response.json().catch(() => ({}));
        throw new Error(
          errorData.detail ||
            `Failed to download final ${format === 'special-csv' ? 'special CSV' : format.toUpperCase()} results`
        );
      }

      // Create download link
      const blob = await response.blob();
      const url = window.URL.createObjectURL(blob);
      const link = document.createElement('a');
      link.href = url;

      // Get filename from response headers or create default
      const contentDisposition = response.headers.get('content-disposition');
      let filename = format === 'special-csv'
        ? `order_${orderId}_special.csv`
        : `order_${orderId}_mapped_results.csv`;

      if (contentDisposition) {
        const filenameMatch = contentDisposition.match(/filename[^;=\n]*=((['"]).*?\2|[^;\n]*)/);
        if (filenameMatch) {
          filename = filenameMatch[1].replace(/['"]/g, '');
        }
      }

      link.download = filename;
      document.body.appendChild(link);
      link.click();

      // Cleanup
      document.body.removeChild(link);
      window.URL.revokeObjectURL(url);

    } catch (error) {
      console.error(`Error downloading final ${format} results:`, error);
      setError(
        error instanceof Error
          ? error.message
          : `Failed to download final ${format === 'special-csv' ? 'special CSV' : format.toUpperCase()} results`
      );
    } finally {
      setDownloadingFiles(prev => ({ ...prev, [downloadKey]: false }));
    }
  };

  // Order Management Functions
  const lockOrder = async () => {
    if (!window.confirm('Are you sure you want to lock this order? Locked orders cannot be modified.')) {
      return;
    }

    setIsLockingOrder(true);
    setError('');
    try {
      const response = await fetch(`/api/orders/${orderId}/lock`, {
        method: 'POST',
      });

      if (!response.ok) {
        const errorData = await response.json();
        throw new Error(errorData.detail || 'Failed to lock order');
      }

      setError('✅ Order locked successfully');
      setTimeout(() => setError(''), 3000);
      loadOrder();
    } catch (error) {
      console.error('Error locking order:', error);
      setError(error instanceof Error ? error.message : 'Failed to lock order');
    } finally {
      setIsLockingOrder(false);
    }
  };

  const unlockOrder = async () => {
    if (!window.confirm('Are you sure you want to unlock this order? This will allow modifications again.')) {
      return;
    }

    setIsUnlockingOrder(true);
    setError('');
    try {
      const response = await fetch(`/api/orders/${orderId}/unlock`, {
        method: 'POST',
      });

      if (!response.ok) {
        const errorData = await response.json();
        throw new Error(errorData.detail || 'Failed to unlock order');
      }

      setError('✅ Order unlocked successfully');
      setTimeout(() => setError(''), 3000);
      loadOrder();
    } catch (error) {
      console.error('Error unlocking order:', error);
      setError(error instanceof Error ? error.message : 'Failed to unlock order');
    } finally {
      setIsUnlockingOrder(false);
    }
  };

  const restartOcr = async () => {
    if (!window.confirm('Are you sure you want to restart OCR processing? This will reprocess all items and overwrite existing OCR results.')) {
      return;
    }

    setIsRestartingOcr(true);
    setError('');
    try {
      const response = await fetch(`/api/orders/${orderId}/restart-ocr`, {
        method: 'POST',
      });

      if (!response.ok) {
        const errorData = await response.json();
        throw new Error(errorData.detail || 'Failed to restart OCR processing');
      }

      setError('✅ OCR processing restarted successfully');
      setTimeout(() => setError(''), 3000);
      loadOrder();
      startProcessingWebSocket();
    } catch (error) {
      console.error('Error restarting OCR:', error);
      setError(error instanceof Error ? error.message : 'Failed to restart OCR processing');
    } finally {
      setIsRestartingOcr(false);
    }
  };

  const restartMapping = async () => {
    if (!window.confirm('Are you sure you want to restart mapping processing? This will reprocess mapping with current configuration.')) {
      return;
    }

    setIsRestartingMapping(true);
    setError('');
    try {
      const response = await fetch(`/api/orders/${orderId}/restart-mapping`, {
        method: 'POST',
      });

      if (!response.ok) {
        const errorData = await response.json();
        throw new Error(errorData.detail || 'Failed to restart mapping processing');
      }

      setError('✅ Mapping processing restarted successfully');
      setTimeout(() => setError(''), 3000);
      loadOrder();
      startProcessingWebSocket();
    } catch (error) {
      console.error('Error restarting mapping:', error);
      setError(error instanceof Error ? error.message : 'Failed to restart mapping processing');
    } finally {
      setIsRestartingMapping(false);
    }
  };

  const getStatusColor = (status: string) => {
    switch (status) {
      case 'DRAFT':
        return 'bg-gray-100 text-gray-800';
      case 'PROCESSING':
        return 'bg-blue-100 text-blue-800';
      case 'OCR_COMPLETED':
        return 'bg-purple-100 text-purple-800';
      case 'MAPPING':
        return 'bg-yellow-100 text-yellow-800';
      case 'COMPLETED':
        return 'bg-green-100 text-green-800';
      case 'FAILED':
        return 'bg-red-100 text-red-800';
      case 'LOCKED':
        return 'bg-orange-100 text-orange-800';
      default:
        return 'bg-gray-100 text-gray-800';
    }
  };

  const getItemStatusColor = (status: string) => {
    switch (status) {
      case 'PENDING':
        return 'bg-gray-100 text-gray-800';
      case 'PROCESSING':
        return 'bg-blue-100 text-blue-800';
      case 'COMPLETED':
        return 'bg-green-100 text-green-800';
      case 'FAILED':
        return 'bg-red-100 text-red-800';
      default:
        return 'bg-gray-100 text-gray-800';
    }
  };

  if (isLoading) {
    return (
      <div className="container mx-auto px-4 py-8">
        <div className="text-center">Loading order details...</div>
      </div>
    );
  }

  if (!order) {
    return (
      <div className="container mx-auto px-4 py-8">
        <div className="text-center text-red-600">Order not found</div>
      </div>
    );
  }

  // Helper functions to determine processing state
  const hasDoneOcr = (status: string) => {
    return status === 'OCR_COMPLETED' || status === 'MAPPING' || status === 'COMPLETED' || status === 'FAILED';
  };

  const hasDoneMapping = (status: string) => {
    return status === 'COMPLETED' || status === 'FAILED';
  };

  const isLocked = order.status === 'LOCKED';
  const canEdit = order.status === 'DRAFT' && !isLocked;
  const canSubmit = order.status === 'DRAFT' && order.total_items > 0 && !isLocked;
  const canStartOcrOnly = order.status === 'DRAFT' && order.total_items > 0 && !isLocked;
  const canStartFullProcess = order.status === 'DRAFT' && order.total_items > 0 && !isLocked;
  const canStartMapping = hasDoneOcr(order.status) && !isLocked && order.items.length > 0 && !hasDoneMapping(order.status);
  const canConfigureMapping = (order.status === 'DRAFT' || order.status === 'OCR_COMPLETED' || order.status === 'COMPLETED' || order.status === 'MAPPING') && !isLocked;
  const canLock = !isLocked && (order.status === 'COMPLETED' || order.status === 'OCR_COMPLETED' || order.status === 'FAILED');
  const canUnlock = isLocked;
  const canRestartOcr = !isLocked && hasDoneOcr(order.status);
  const remapItemCount = (order as any).remap_item_count ?? 0;
  const canRemap = (order as any).can_remap !== false; // default true if field absent
  const canRestartMapping = !isLocked && hasDoneMapping(order.status) && canRemap;

  const remapDisableReason = () => {
    if (isLocked) return 'Order is locked';
    if (!hasDoneMapping(order.status)) return 'Mapping not attempted yet; use Start Mapping';
    if (!canRemap) return '缺少 OCR 结果，请先 Re‑OCR';
    return 'Restart mapping processing with current configuration';
  };

  return (
    <div className="container mx-auto px-4 py-8">
      {/* Header */}
      <div className="flex items-center justify-between mb-8">
        <div className="flex items-center gap-4">
          <Link
            href="/orders"
            className="text-blue-600 hover:text-blue-800"
          >
            ← Back to Orders
          </Link>
          <h1 className="text-2xl font-bold">
            {order.order_name || `Order ${order.order_id}`}
          </h1>
          <span className={`px-3 py-1 text-sm font-semibold rounded-full ${getStatusColor(order.status)}`}>
            {order.status}
          </span>
        </div>
        <div className="flex items-center gap-3">
          {/* Lock/Unlock Order Button */}
          {canLock && (
            <button
              onClick={lockOrder}
              disabled={isLockingOrder}
              className="bg-orange-600 hover:bg-orange-700 disabled:bg-gray-400 text-white py-2 px-4 rounded font-medium"
              title="Lock this order to prevent modifications"
            >
              {isLockingOrder ? '🔒 Locking...' : '🔒 Lock Order'}
            </button>
          )}
          {canUnlock && (
            <button
              onClick={unlockOrder}
              disabled={isUnlockingOrder}
              className="bg-green-600 hover:bg-green-700 disabled:bg-gray-400 text-white py-2 px-4 rounded font-medium"
              title="Unlock this order to allow modifications"
            >
              {isUnlockingOrder ? '🔓 Unlocking...' : '🔓 Unlock Order'}
            </button>
          )}

          {/* Restart Buttons - Only show when unlocked */}
          {canRestartOcr && (
            <button
              onClick={restartOcr}
              disabled={isRestartingOcr}
              className="bg-yellow-600 hover:bg-yellow-700 disabled:bg-gray-400 text-white py-2 px-4 rounded font-medium"
              title="Restart OCR processing for all items"
            >
              {isRestartingOcr ? '🔄 Restarting OCR...' : '🔄 Re-OCR'}
            </button>
          )}
          {hasDoneMapping(order.status) && (
            <button
              onClick={restartMapping}
              disabled={isRestartingMapping}
              className={`text-white py-2 px-4 rounded font-medium ${canRestartMapping ? 'bg-indigo-600 hover:bg-indigo-700' : 'bg-gray-400 cursor-not-allowed'}`}
              title={remapDisableReason()}
              {...(!canRestartMapping ? { onClick: undefined } : {})}
            >
              {isRestartingMapping ? '🔄 Restarting Mapping...' : '🔄 Re-Mapping'}
            </button>
          )}

          {/* Processing Buttons - Only show when unlocked */}
          {canStartOcrOnly && (
            <button
              onClick={startOcrOnlyProcessing}
              className="bg-blue-600 hover:bg-blue-700 text-white py-2 px-4 rounded font-medium"
              title="Start OCR processing only. You can configure mapping later."
            >
              Start OCR only
            </button>
          )}
          {canStartFullProcess && (
            <button
              onClick={submitOrder}
              className="bg-green-600 hover:bg-green-700 text-white py-2 px-4 rounded font-medium"
              title="Start OCR processing with mapping (requires mapping file and keys)"
            >
              OCR & Mapping
            </button>
          )}
          {canStartMapping && (
            <button
              onClick={startMappingProcessing}
              disabled={isStartingMapping}
              className="bg-purple-600 hover:bg-purple-700 disabled:bg-gray-400 text-white py-2 px-4 rounded font-medium"
              title="Start mapping processing (OCR already completed)"
            >
              {isStartingMapping ? 'Map Processing...' : 'Start Mapping'}
            </button>
          )}
        </div>
      </div>

      {order.primary_doc_type && (
        <div className="mb-6 text-sm text-gray-600">
          Primary Document Type:{' '}
          <span className="font-medium text-gray-900">
            {order.primary_doc_type.type_name} ({order.primary_doc_type.type_code})
          </span>
          {order.primary_doc_type.template_json_path ? (
            <span className="ml-2 text-xs text-green-600">
              Template configured
              {order.primary_doc_type.template_version ? ` (v${order.primary_doc_type.template_version})` : ''}
            </span>
          ) : (
            <span className="ml-2 text-xs text-gray-500">No template uploaded</span>
          )}
          <span className="ml-4 text-xs text-gray-600">Remappable Items: <span className="font-medium text-gray-900">{remapItemCount}</span></span>
        </div>
      )}

      {error && (
        <div className={`border-l-4 p-4 mb-6 ${
          error.startsWith('✅')
            ? 'bg-green-100 border-green-500 text-green-700'
            : 'bg-red-100 border-red-500 text-red-700'
        }`}>
          {error}
        </div>
      )}

      {/* Status Information */}
      {order.status === 'LOCKED' && (
        <div className="bg-orange-50 border border-orange-200 rounded-lg p-4 mb-6">
          <div className="flex items-center gap-2 mb-2">
            <span className="text-orange-600 font-semibold">🔒 Order Locked</span>
          </div>
          <p className="text-sm text-orange-700">
            This order is currently locked and cannot be modified. Auto-mapping configuration is applied from the document settings.
          </p>
          <ul className="text-sm text-orange-700 list-disc list-inside mt-1 space-y-1">
            <li>All processing operations are disabled</li>
            <li>File uploads and modifications are not allowed</li>
            <li>Use the "🔓 Unlock Order" button to enable modifications</li>
          </ul>
        </div>
      )}

      {order.status === 'OCR_COMPLETED' && (
        <div className="bg-purple-50 border border-purple-200 rounded-lg p-4 mb-6">
          <div className="flex items-center gap-2 mb-2">
            <span className="text-purple-600 font-semibold">🔍 OCR Processing Completed</span>
          </div>
          <p className="text-sm text-purple-700">
            All files have been processed with OCR. You can now:
          </p>
          <ul className="text-sm text-purple-700 list-disc list-inside mt-1 space-y-1">
            <li>Review the OCR results by downloading CSV files from individual items</li>
            <li>Configure mapping by uploading a mapping file and selecting keys</li>
            <li>Start mapping processing once configuration is complete</li>
          </ul>
        </div>
      )}

      {/* Order Summary */}
      <div className="bg-white rounded-lg shadow p-6 mb-8">
        <h2 className="text-lg font-semibold mb-4">Order Summary</h2>
        <div className="grid grid-cols-2 md:grid-cols-4 gap-4">
          {/* Item-level stats */}
          <div>
            <div className="text-sm text-gray-500">Total Items</div>
            <div className="text-2xl font-bold">{order.total_items}</div>
          </div>
          <div>
            <div className="text-sm text-gray-500">Items Completed</div>
            <div className="text-2xl font-bold text-green-600">{order.completed_items}</div>
          </div>
          <div>
            <div className="text-sm text-gray-500">Items Failed</div>
            <div className="text-2xl font-bold text-red-600">{order.failed_items}</div>
          </div>
          <div>
            <div className="text-sm text-gray-500">Item Progress</div>
            <div className="text-2xl font-bold">
              {order.total_items > 0 ? Math.round((order.completed_items / order.total_items) * 100) : 0}%
            </div>
          </div>
        </div>

        <div className="mt-4 grid grid-cols-2 md:grid-cols-4 gap-4">
          {/* Attachment-level stats */}
          <div>
            <div className="text-sm text-gray-500">Total Attachments</div>
            <div className={`text-2xl font-bold ${order.total_attachments === 0 ? 'text-gray-400' : ''}`}>
              {order.total_attachments}
            </div>
          </div>
          <div>
            <div className="text-sm text-gray-500">Attachments Completed</div>
            <div className={`text-2xl font-bold ${order.total_attachments === 0 ? 'text-gray-300' : 'text-green-600'}`}>
              {order.completed_attachments}
            </div>
          </div>
          <div>
            <div className="text-sm text-gray-500">Attachments Failed</div>
            <div className={`text-2xl font-bold ${order.total_attachments === 0 ? 'text-gray-300' : 'text-red-600'}`}>
              {order.failed_attachments}
            </div>
          </div>
          <div>
            <div className="text-sm text-gray-500">Attachment Progress</div>
            <div className={`text-2xl font-bold ${order.total_attachments === 0 ? 'text-gray-300' : ''}`}>
              {order.total_attachments > 0
                ? Math.round((order.completed_attachments / order.total_attachments) * 100)
                : 0
              }%
            </div>
          </div>
        </div>

        <div className="mt-6 grid gap-4 md:grid-cols-2">
          <div>
            <div className="text-sm text-gray-500">Primary Document Type</div>
            <div className="mt-1 text-base font-medium text-gray-900">
              {order.primary_doc_type
                ? `${order.primary_doc_type.type_name} (${order.primary_doc_type.type_code})`
                : 'Not selected'}
            </div>
            {order.primary_doc_type ? (
              order.primary_doc_type.template_json_path ? (
                <div className="mt-1 text-xs text-green-600">
                  Template configured
                  {order.primary_doc_type.template_version
                    ? ` (v${order.primary_doc_type.template_version})`
                    : ''}
                </div>
              ) : (
                <div className="mt-1 text-xs text-gray-500">
                  No template uploaded; special CSV export will be skipped.
                </div>
              )
            ) : (
              <div className="mt-1 text-xs text-gray-500">
                Select a primary document type when creating an order to enable template-driven outputs.
              </div>
            )}
          </div>
          <div>
            <div className="text-sm text-gray-500">Special CSV</div>
            <div className="mt-1 text-base font-medium text-gray-900">
              {order.final_report_paths?.special_csv ? 'Ready for download' : 'Not generated'}
            </div>
            {order.final_report_paths?.special_csv ? (
              <div className="mt-1 text-xs text-green-600">
                Generated using the configured template.
              </div>
            ) : (
              <div className="mt-1 text-xs text-gray-500">
                {order.primary_doc_type?.template_json_path
                  ? 'Special CSV will become available after mapping completes.'
                  : 'Upload a template for the primary document type to produce a special CSV.'}
              </div>
            )}
          </div>
        </div>
      </div>

      {/* Order Items Section */}
      <div className="bg-white rounded-lg shadow p-6 mb-8">
        <div className="flex items-center justify-between mb-4">
          <h2 className="text-lg font-semibold">Order Items</h2>
          {canEdit && (
            <button
              onClick={() => setShowAddItemModal(true)}
              className="bg-blue-600 hover:bg-blue-700 text-white py-2 px-4 rounded"
            >
              Add Item
            </button>
          )}
        </div>

        {order.items.length === 0 ? (
          <div className="text-center py-8 text-gray-500">
            No items added yet. Click "Add Item" to get started.
          </div>
        ) : (
          <div className="space-y-4">
            {order.items.map((item) => (
              <div key={item.item_id} className="border rounded-lg p-4">
                <div className="flex items-center justify-between mb-2">
                  <div className="flex items-center gap-4">
                    <h3 className="font-medium">{item.item_name}</h3>
                    <span className={`px-2 py-1 text-xs font-semibold rounded-full ${getItemStatusColor(item.status)}`}>
                      {item.status}
                    </span>
                  </div>
                  <div className="flex items-center gap-2">
                    {canEdit && (
                      <button
                        onClick={() => deleteItem(item.item_id, item.item_name)}
                        disabled={deletingItems[item.item_id]}
                        className="text-red-600 hover:text-red-800 disabled:text-gray-400 text-sm font-medium"
                        title={`Delete ${item.item_name}`}
                      >
                        {deletingItems[item.item_id] ? 'Deleting...' : 'Delete Item'}
                      </button>
                    )}
                  </div>
                </div>
                <div className="space-y-4">
                  {/* Primary File Section */}
                  {item.primary_file ? (
                    <div className="border-b pb-3">
                      <h4 className="font-medium text-gray-700 mb-2">📄 Primary File</h4>
                          <div className="flex justify-between items-center bg-blue-50 p-2 rounded">
                        <div className="flex-1">
                          <p className="text-sm font-medium">{item.primary_file.filename}</p>
                          <p className="text-xs text-gray-500">{(item.primary_file.file_size / 1024).toFixed(1)}KB</p>
                        </div>
                        <div className="flex gap-2">
                          {item.status === 'COMPLETED' && item.ocr_result_json_path && (
                            <>
                              <div className="flex items-center border-l pl-2 gap-1">
                                <span className="text-xs text-gray-500 font-medium">Primary:</span>
                                <button
                                  onClick={() => downloadItemResult(item.item_id, 'json', item.item_name, 'primary')}
                                  disabled={downloadingFiles[`primary-${item.item_id}-json`]}
                                  className="bg-blue-100 hover:bg-blue-200 disabled:bg-gray-200 text-blue-700 disabled:text-gray-500 px-2 py-1 rounded text-xs font-medium"
                                  title="Download primary JSON"
                                >
                                  {downloadingFiles[`primary-${item.item_id}-json`] ? '...' : '📄 JSON'}
                                </button>
                                <button
                                  onClick={() => downloadItemResult(item.item_id, 'csv', item.item_name, 'primary')}
                                  disabled={downloadingFiles[`primary-${item.item_id}-csv`]}
                                  className="bg-green-100 hover:bg-green-200 disabled:bg-gray-200 text-green-700 disabled:text-gray-500 px-2 py-1 rounded text-xs font-medium"
                                  title="Download primary CSV"
                                >
                                  {downloadingFiles[`primary-${item.item_id}-csv`] ? '...' : '📊 CSV'}
                                </button>
                              </div>
                            </>
                          )}
                          {canEdit && (
                            <>
                              <button
                                onClick={() => {
                                  const input = document.createElement('input');
                                  input.type = 'file';
                                  input.accept = '.pdf,.jpg,.jpeg,.png';
                                  input.multiple = true;
                                  input.onchange = (e) => {
                                    const selectedFiles = (e.target as HTMLInputElement).files;
                                    if (selectedFiles && selectedFiles.length > 0) {
                                      replacePrimaryAndAttachments(item, selectedFiles);
                                    }
                                  };
                                  input.click();
                                }}
                                className="text-blue-600 hover:text-blue-800 px-2 py-1 text-xs font-medium"
                                title="Replace primary file"
                              >
                                🔄 Replace
                              </button>
                              <button
                                onClick={() => {
                                  if (window.confirm('Delete primary file?')) {
                                    deletePrimaryFile(item.item_id);
                                  }
                                }}
                                className="text-red-600 hover:text-red-800 px-2 py-1 text-xs font-medium"
                                title="Delete primary file"
                              >
                                🗑️ Delete
                              </button>
                            </>
                          )}
                        </div>
                      </div>
                    </div>
                  ) : (
                    canEdit && (
                      <div className="border-b pb-3">
                        <h4 className="font-medium text-gray-700 mb-2">📄 Primary File</h4>
                        <button
                          onClick={() => {
                            const input = document.createElement('input');
                            input.type = 'file';
                            input.accept = '.pdf,.jpg,.jpeg,.png';
                            input.multiple = true;
                            input.onchange = (e) => {
                              const selectedFiles = (e.target as HTMLInputElement).files;
                              if (selectedFiles && selectedFiles.length > 0) {
                                uploadPrimaryAndAttachments(item, selectedFiles);
                              }
                            };
                            input.click();
                          }}
                          className="bg-blue-100 hover:bg-blue-200 text-blue-700 py-2 px-3 rounded text-sm font-medium"
                        >
                          📤 Upload Primary File
                        </button>
                      </div>
                    )
                  )}

                  {/* Attachments Section */}
                  <div>
                    <h4 className="font-medium text-gray-700 mb-2">📎 Attachments ({item.attachment_count || 0})</h4>
                    {item.attachments && item.attachments.length > 0 ? (
                      <div className="border rounded-lg p-3 bg-gray-50">
                        <button
                          onClick={() => setExpandedItemFiles(prev => ({
                            ...prev,
                            [item.item_id]: !prev[item.item_id]
                          }))}
                          className="flex items-center gap-2 font-medium text-gray-700 hover:text-gray-900 w-full text-left"
                        >
                          <span>{expandedItemFiles[item.item_id] ? '▼' : '▶'}</span>
                          <span>📎 Attached Files ({item.attachments.length})</span>
                        </button>
                        {expandedItemFiles[item.item_id] && (
                          <div className="text-xs text-gray-600 space-y-1 mt-2 pl-4">
                            {item.attachments.map((file: any) => {
                              const deleteKey = `${item.item_id}-${file.file_id}`;
                              return (
                                <div key={file.file_id} className="flex items-center justify-between py-1 hover:bg-white hover:px-2 hover:rounded transition">
                                  <div className="truncate flex-1">
                                    <span className="text-green-600">📎</span> {file.filename} ({(file.file_size / 1024).toFixed(1)}KB)
                                  </div>
                                  <div className="flex gap-1">
                                    {item.status === 'COMPLETED' && (
                                      <>
                                        <div className="flex items-center gap-1 border-l pl-2">
                                          <span className="text-xs text-gray-500 font-medium">Attachments:</span>
                                          <button
                                            onClick={() => downloadAttachmentJson(item.item_id, file.file_id)}
                                            disabled={downloadingFiles[`${item.item_id}-${file.file_id}-json`]}
                                            className="text-blue-600 hover:text-blue-800 disabled:text-gray-400 text-xs font-medium"
                                            title="Download attachment JSON"
                                          >
                                            {downloadingFiles[`${item.item_id}-${file.file_id}-json`] ? '...' : '📄 JSON'}
                                          </button>
                                          <button
                                            onClick={() => downloadAttachmentCsv(item.item_id, file.file_id)}
                                            disabled={downloadingFiles[`${item.item_id}-${file.file_id}-csv`]}
                                            className="text-green-600 hover:text-green-800 disabled:text-gray-400 text-xs font-medium"
                                            title="Download attachment CSV"
                                          >
                                            {downloadingFiles[`${item.item_id}-${file.file_id}-csv`] ? '...' : '📊 CSV'}
                                          </button>
                                        </div>
                                      </>
                                    )}
                                    {canEdit && (
                                      <button
                                        onClick={() => deleteFile(item.item_id, file.file_id, file.filename)}
                                        disabled={deletingFiles[deleteKey]}
                                        className="ml-1 text-red-600 hover:text-red-800 disabled:text-gray-400 text-xs font-medium"
                                        title={`Delete ${file.filename}`}
                                      >
                                        {deletingFiles[deleteKey] ? '...' : '✕'}
                                      </button>
                                    )}
                                  </div>
                                </div>
                              );
                            })}
                          </div>
                        )}
                      </div>
                    ) : (
                      <div className="text-gray-400 text-sm mb-3">No attachments yet</div>
                    )}
                  </div>
                </div>
                <div className="text-sm text-gray-600 mb-3">
                  {item.company_name} - {item.doc_type_name}
                </div>

                <div className="border-t border-gray-200 pt-3 mt-3">
                  <div className="flex items-start justify-between gap-4">
                      <div>
                        <div className="text-sm font-medium text-gray-700">
                          Mapping Mode: {formatMappingType(item.item_type)}
                        </div>
                        {item.mapping_config ? (
                        <div className="mt-1 text-xs text-gray-600 space-y-1">
                          <div>
                            <span className="font-medium">Master CSV:</span>{' '}
                            {item.mapping_config.master_csv_path || '—'}
                          </div>
                          {item.mapping_config.external_join_keys && item.mapping_config.external_join_keys.length > 0 && (
                            <div>
                              <span className="font-medium">External Join Keys:</span>{' '}
                              {item.mapping_config.external_join_keys.join(', ')}
                            </div>
                          )}
                          {item.mapping_config.internal_join_key && (
                            <div>
                              <span className="font-medium">Internal Join Key:</span>{' '}
                              {item.mapping_config.internal_join_key}
                            </div>
                          )}
                          {item.mapping_config.column_aliases && Object.keys(item.mapping_config.column_aliases).length > 0 && (
                            <div>
                              <span className="font-medium">Column Aliases:</span>{' '}
                              {Object.entries(item.mapping_config.column_aliases)
                                .map(([left, right]) => `${left} → ${right}`)
                                .join(', ')}
                            </div>
                          )}
                          {item.mapping_config.attachment_sources && item.mapping_config.attachment_sources.length > 0 && (
                            <div>
                              <span className="font-medium">Attachment Sources:</span>{' '}
                              {item.mapping_config.attachment_sources.map((source, index) => (
                                <span key={index}>
                                  {source.path}{source.metadata?.month ? ` (${source.metadata.month})` : ''}
                                  {index < ((item.mapping_config?.attachment_sources?.length ?? 0) - 1) ? ', ' : ''}
                                </span>
                              ))}
                            </div>
                          )}
                        </div>
                      ) : (
                        <div className="text-xs text-gray-500 mt-1">
                          No saved mapping configuration. Defaults will be applied if available.
                        </div>
                      )}
                      {item.applied_template_id && (
                        <div className="text-xs text-blue-600 mt-1">
                          Inherits Template #{item.applied_template_id}
                        </div>
                      )}
                    </div>
                    {canConfigureMapping && (
                      <button
                        onClick={() => openMappingModal(item)}
                        className="bg-blue-600 hover:bg-blue-700 text-white py-1.5 px-3 rounded text-sm font-medium whitespace-nowrap"
                      >
                        Configure Mapping
                      </button>
                    )}
                  </div>
                </div>

                {canEdit && (
                  <div className="space-y-3">
                    {/* The generic attachment upload button is intentionally removed
                        because the Primary File upload covers the same use case.
                        Keep the AWB month attach helper below. */}

                    {/* AWB Month Attach - Only for AIRWAY_BILL items */}
                    {item.doc_type_name && documentTypes.find(dt => dt.doc_type_id === item.doc_type_id)?.type_code === 'AIRWAY_BILL' && (
                      <div className="border-t pt-3 mt-3">
                        <div className="text-sm font-medium text-gray-700 mb-2">📅 Attach from Month</div>
                        <div className="space-y-2">
                          <div className="flex items-end gap-2">
                            <div className="flex-1">
                              <label className="block text-xs font-medium text-gray-600 mb-1">Month (YYYY-MM)</label>
                              <input
                                type="month"
                                value={awbMonths[item.item_id] || ''}
                                onChange={(e) => setAwbMonths(prev => ({ ...prev, [item.item_id]: e.target.value }))}
                                className="w-full border border-gray-300 rounded px-2 py-1 text-sm"
                                disabled={awbMonthAttaching[item.item_id]}
                              />
                            </div>
                            <button
                              onClick={() => attachAwbMonth(item.item_id)}
                              disabled={awbMonthAttaching[item.item_id] || !awbMonths[item.item_id]}
                              className="bg-blue-500 hover:bg-blue-600 disabled:bg-gray-400 text-white py-1 px-3 rounded text-sm font-medium whitespace-nowrap"
                            >
                              {awbMonthAttaching[item.item_id] ? 'Attaching...' : 'Attach'}
                            </button>
                          </div>
                        </div>
                      </div>
                    )}
                  </div>
                )}

                {/* Download Results Section - item-level combined results (primary + attachments) */}
                {(item.ocr_result_json_path || item.ocr_result_csv_path) && (
                  <div className="mt-4 pt-3 border-t border-gray-200">
                    <div className="text-sm font-medium text-gray-700 mb-2">Download Results:</div>
                    <div className="flex flex-wrap items-center gap-2">
                      <div className="flex items-center gap-2 flex-wrap">
                        {/* Keep JSON button for primary only */}
                        {item.ocr_result_json_path && (
                          <button
                            onClick={() => downloadItemResult(item.item_id, 'json', item.item_name, 'item')}
                            disabled={downloadingFiles[`item-${item.item_id}-json`]}
                            className="bg-blue-100 hover:bg-blue-200 disabled:bg-gray-200 text-blue-700 disabled:text-gray-500 py-1 px-3 rounded text-sm font-medium"
                            title="Download combined JSON results"
                          >
                            {downloadingFiles[`item-${item.item_id}-json`] ? 'Downloading...' : '📄 JSON'}
                          </button>
                        )}
                        {/* CSV button for primary */}
                        {item.ocr_result_csv_path && (
                          <button
                            onClick={() => downloadItemResult(item.item_id, 'csv', item.item_name, 'item')}
                            disabled={downloadingFiles[`item-${item.item_id}-csv`]}
                            className="bg-green-100 hover:bg-green-200 disabled:bg-gray-200 text-green-700 disabled:text-gray-500 py-1 px-3 rounded text-sm font-medium"
                            title="Download combined CSV results"
                          >
                            {downloadingFiles[`item-${item.item_id}-csv`] ? 'Downloading...' : '📊 CSV'}
                          </button>
                        )}
                      </div>

                      {/* Conditional CSV Merge Section removed per request */}
                    </div>
                  </div>
                )}

                
                {item.error_message && (
                  <div className="mt-2 text-sm text-red-600">
                    Error: {item.error_message}
                  </div>
                )}
              </div>
            ))}
          </div>
        )}
      </div>



      {/* Final Mapped Results Section - Show only for completed orders with mapped results */}
      {order.status === 'COMPLETED' && (
        order.final_report_paths?.mapped_csv ||
        order.final_report_paths?.special_csv
      ) && (
        <div className="bg-white rounded-lg shadow p-6 mb-8">
          <div className="mb-4">
            <h2 className="text-lg font-semibold text-green-600">📊 Final Mapped Results</h2>
            <p className="text-sm text-gray-600 mt-1">
              Download the complete results with mapping applied based on your configured keys.
            </p>
          </div>

          <div className="bg-green-50 border border-green-200 rounded-lg p-4">
            <div className="flex items-center gap-3 mb-3">
              <div className="flex items-center text-green-700">
                <svg className="w-5 h-5 mr-1" fill="currentColor" viewBox="0 0 20 20">
                  <path fillRule="evenodd" d="M16.707 5.293a1 1 0 010 1.414l-8 8a1 1 0 01-1.414 0l-4-4a1 1 0 011.414-1.414L8 12.586l7.293-7.293a1 1 0 011.414 0z" clipRule="evenodd" />
                </svg>
                <span className="font-medium">Mapping Completed Successfully</span>
              </div>
            </div>

            <div className="text-sm text-green-700 mb-4">
              All order items have been processed and mapped according to your configuration. Download the final results below:
            </div>

            <div className="flex items-center gap-3">
              {order.final_report_paths?.mapped_csv && (
                <button
                  onClick={() => downloadFinalMappedResults('csv')}
                  disabled={downloadingFiles['final-csv']}
                  className="bg-green-600 hover:bg-green-700 disabled:bg-gray-400 text-white py-2 px-4 rounded font-medium flex items-center gap-2"
                  title="Download final mapped results as CSV"
                >
                  {downloadingFiles['final-csv'] ? (
                    'Downloading...'
                  ) : (
                    <>
                      📊 Download CSV Results
                    </>
                  )}
                </button>
              )}

        
              {order.final_report_paths?.special_csv && (
                <button
                  onClick={() => downloadFinalMappedResults('special-csv')}
                  disabled={downloadingFiles['final-special-csv']}
                  className="bg-indigo-600 hover:bg-indigo-700 disabled:bg-gray-400 text-white py-2 px-4 rounded font-medium flex items-center gap-2"
                  title="Download template-driven special CSV output"
                >
                  {downloadingFiles['final-special-csv'] ? 'Downloading...' : '✨ Download Special CSV'}
                </button>
              )}
            </div>

            <div className="mt-3 text-xs text-green-600">
              💡 CSV delivers the raw mapped dataset, Excel provides analytics worksheets, and Special CSV applies template-defined formatting.
            </div>
          </div>
        </div>
      )}

      {/* Add Item Modal */}
      {showAddItemModal && (
        <div className="fixed inset-0 bg-black bg-opacity-50 flex items-center justify-center z-50">
          <div className="bg-white rounded-lg p-6 w-full max-w-md">
            <h3 className="text-lg font-semibold mb-4">Add Order Item</h3>

            <div className="space-y-4">
              <div>
                <label className="block text-sm font-medium text-gray-700 mb-1">
                  Company
                </label>
                <select
                  value={selectedCompany || ''}
                  onChange={(e) => setSelectedCompany(parseInt(e.target.value))}
                  className="w-full border border-gray-300 rounded px-3 py-2"
                >
                  <option value="">Select Company</option>
                  {companies.map((company) => (
                    <option key={company.company_id} value={company.company_id}>
                      {company.company_name}
                    </option>
                  ))}
                </select>
              </div>

              <div>
                <label className="block text-sm font-medium text-gray-700 mb-1">
                  Document Type
                </label>
                <select
                  value={selectedDocType || ''}
                  onChange={(e) => setSelectedDocType(parseInt(e.target.value))}
                  className="w-full border border-gray-300 rounded px-3 py-2"
                >
                  <option value="">Select Document Type</option>
                  {documentTypes.map((docType) => (
                    <option key={docType.doc_type_id} value={docType.doc_type_id}>
                      {docType.type_name}
                    </option>
                  ))}
                </select>
              </div>

              <div>
                <span className="block text-sm font-medium text-gray-700 mb-2">Mapping Mode</span>
                <div className="flex items-center gap-4">
                  <label className="flex items-center gap-2 text-sm">
                    <input
                      type="radio"
                      name="new_item_type"
                      value="single_source"
                      checked={newItemType === 'single_source'}
                      onChange={() => setNewItemType('single_source')}
                      className="text-blue-600 focus:ring-blue-500"
                    />
                    Single Source
                  </label>
                  <label className="flex items-center gap-2 text-sm">
                    <input
                      type="radio"
                      name="new_item_type"
                      value="multi_source"
                      checked={newItemType === 'multi_source'}
                      onChange={() => setNewItemType('multi_source')}
                      className="text-blue-600 focus:ring-blue-500"
                    />
                    Multiple Source
                  </label>
                </div>
                <p className="mt-1 text-xs text-gray-500">
                  Single Source joins the primary PDF directly to a master CSV. Multiple Source expects primary-plus-attachments and merges them before lookup.
                </p>
              </div>

              <div>
                <label className="block text-sm font-medium text-gray-700 mb-1">
                  Item Name (Optional)
                </label>
                <input
                  type="text"
                  value={itemName}
                  onChange={(e) => setItemName(e.target.value)}
                  placeholder="Leave empty for auto-generated name"
                  className="w-full border border-gray-300 rounded px-3 py-2"
                />
              </div>
            </div>

            <div className="flex justify-end gap-2 mt-6">
              <button
                onClick={() => setShowAddItemModal(false)}
                className="px-4 py-2 text-gray-700 border border-gray-300 rounded hover:bg-gray-50"
              >
                Cancel
              </button>
              <button
                onClick={addOrderItem}
                disabled={
                  isAddingItem ||
                  !selectedCompany ||
                  !selectedDocType
                }
                className="px-4 py-2 bg-blue-600 text-white rounded hover:bg-blue-700 disabled:bg-gray-400"
              >
                {isAddingItem ? 'Adding...' : 'Add Item'}
              </button>
            </div>
          </div>
        </div>
      )}

      {mappingModalItem && (
        <div className="fixed inset-0 bg-black bg-opacity-40 flex items-center justify-center z-50">
          <div className="bg-white rounded-lg shadow-lg w-full max-w-xl p-6">
            <h3 className="text-lg font-semibold mb-4">
              Configure Mapping – {mappingModalItem.item_name}
            </h3>
            {mappingModalItem.applied_template_id && (
              <div className="mb-4 text-xs text-blue-600">
                {mappingForm.inherit_defaults
                  ? `Will inherit template #${mappingModalItem.applied_template_id} (no overrides).`
                  : `Currently applying template #${mappingModalItem.applied_template_id} with overrides below.`}
              </div>
            )}
            <div className="space-y-4">
              <div>
                <span className="block text-sm font-medium text-gray-700 mb-2">Mapping Mode</span>
                <div className="flex items-center gap-4">
                  <label className="flex items-center gap-2 text-sm">
                    <input
                      type="radio"
                      name="item_type"
                      value="single_source"
                      checked={mappingForm.item_type === 'single_source'}
                      onChange={() => handleMappingFormChange('item_type', 'single_source')}
                      className="text-blue-600 focus:ring-blue-500"
                    />
                    Single Source
                  </label>
                  <label className="flex items-center gap-2 text-sm">
                    <input
                      type="radio"
                      name="item_type"
                      value="multi_source"
                      checked={mappingForm.item_type === 'multi_source'}
                      onChange={() => handleMappingFormChange('item_type', 'multi_source')}
                      className="text-blue-600 focus:ring-blue-500"
                    />
                    Multiple Source
                  </label>
                </div>
              </div>

              <label className="flex items-center gap-2 text-sm">
                <input
                  type="checkbox"
                  checked={mappingForm.inherit_defaults}
                  onChange={(e) => handleMappingFormChange('inherit_defaults', e.target.checked)}
                />
                Inherit company/document defaults (if available)
              </label>

              <div>
                <label className="block text-sm font-medium text-gray-700 mb-1">Master CSV Path</label>
                <input
                  type="text"
                  value={mappingForm.master_csv_path}
                  onChange={(e) => handleMappingFormChange('master_csv_path', e.target.value)}
                  disabled={mappingForm.inherit_defaults}
                  className="w-full border border-gray-300 rounded px-3 py-2 text-sm disabled:bg-gray-100"
                  placeholder="e.g. HYA-OCR/Master Data/TELECOM_USERS.csv"
                />
                <div className="mt-2 flex items-center gap-2">
                  <button
                    type="button"
                    onClick={previewMasterCsv}
                    disabled={mappingForm.inherit_defaults || isPreviewingCsv || !mappingForm.master_csv_path.trim()}
                    className="px-3 py-1 text-xs font-medium text-white bg-gray-700 hover:bg-gray-800 rounded disabled:bg-gray-300"
                    title="Preview columns from the master CSV"
                  >
                    {isPreviewingCsv ? 'Loading…' : 'Preview Columns'}
                  </button>
                  {csvPreview && (
                    <span className="text-xs text-gray-600">
                      {csvPreview.headers.length} columns · {csvPreview.row_count} rows
                    </span>
                  )}
                </div>
                {csvPreview && csvPreview.headers.length > 0 && (
                  <div className="mt-2 bg-gray-50 border border-gray-200 rounded p-2">
                    <div className="text-xs text-gray-600 mb-1">Columns (click to toggle in External Join Keys):</div>
                    <div className="flex flex-wrap gap-1">
                      {csvPreview.headers.slice(0, 24).map((h, i) => {
                        const selected = parseExternalJoinKeys().includes(h);
                        return (
                          <button
                            type="button"
                            key={i}
                            onClick={() => toggleExternalJoinKey(h)}
                            className={`text-[11px] border rounded px-2 py-0.5 ${selected ? 'bg-blue-600 text-white border-blue-700' : 'bg-white text-gray-800 border-gray-300'}`}
                            title={selected ? 'Remove from join keys' : 'Add to join keys'}
                          >
                            {h}
                          </button>
                        );
                      })}
                      {csvPreview.headers.length > 24 && (
                        <span className="text-[11px] text-gray-500">+{csvPreview.headers.length - 24} more</span>
                      )}
                    </div>
                  </div>
                )}
              </div>

              <div>
                <div className="flex items-center gap-2 mb-2">
                  <span className="block text-sm font-medium text-gray-700">Primary JSON Columns</span>
                  <button
                    type="button"
                    onClick={previewPrimaryHeaders}
                    disabled={isPreviewingPrimary}
                    className="px-3 py-1 text-xs font-medium text-white bg-gray-700 hover:bg-gray-800 rounded disabled:bg-gray-300"
                    title="Preview columns from the primary JSON (deep-flattened)"
                  >
                    {isPreviewingPrimary ? 'Loading…' : 'Preview Primary Columns'}
                  </button>
                </div>
                {primaryPreview && primaryPreview.length > 0 && (
                  <div className="bg-gray-50 border border-gray-200 rounded p-2">
                    <div className="text-xs text-gray-600 mb-1">Click to toggle in External Join Keys:</div>
                    <div className="flex flex-wrap gap-1">
                      {primaryPreview.slice(0, 24).map((h, i) => {
                        const selected = parseExternalJoinKeys().includes(h);
                        return (
                          <button
                            type="button"
                            key={i}
                            onClick={() => toggleExternalJoinKey(h)}
                            className={`text-[11px] border rounded px-2 py-0.5 ${selected ? 'bg-blue-600 text-white border-blue-700' : 'bg-white text-gray-800 border-gray-300'}`}
                            title={selected ? 'Remove from join keys' : 'Add to join keys'}
                          >
                            {h}
                          </button>
                        );
                      })}
                      {primaryPreview.length > 24 && (
                        <span className="text-[11px] text-gray-500">+{primaryPreview.length - 24} more</span>
                      )}
                    </div>
                    {mappingForm.item_type === 'multi_source' && (
                      <div className="mt-2">
                        <div className="text-xs text-gray-600 mb-1">Set Internal Join Key:</div>
                        <div className="flex flex-wrap gap-1">
                          {primaryPreview.slice(0, 24).map((h, i) => (
                            <button
                              type="button"
                              key={`internal-${i}`}
                              onClick={() => handleMappingFormChange('internal_join_key', h)}
                              className={`text-[11px] border rounded px-2 py-0.5 ${mappingForm.internal_join_key === h ? 'bg-indigo-600 text-white border-indigo-700' : 'bg-white text-gray-800 border-gray-300'}`}
                              title="Set as internal join key"
                              disabled={mappingForm.inherit_defaults}
                            >
                              {h}
                            </button>
                          ))}
                          {primaryPreview.length > 24 && (
                            <span className="text-[11px] text-gray-500">+{primaryPreview.length - 24} more</span>
                          )}
                        </div>
                      </div>
                    )}
                  </div>
                )}
              </div>

              {mappingForm.item_type === 'multi_source' && (
                <div>
                  <label className="block text-sm font-medium text-gray-700 mb-1">Attachment Rules</label>
                  <div className="space-y-2">
                    {(mappingForm.attachment_sources || []).map((rule, idx) => (
                      <div key={idx} className="grid md:grid-cols-5 gap-3 items-end">
                        <div>
                          <label className="block text-xs text-gray-600 mb-1">OneDrive Path</label>
                          <input
                            type="text"
                            value={rule.path || ''}
                            onChange={(e) => updateAttachmentRule(idx, 'path', e.target.value)}
                            disabled={mappingForm.inherit_defaults}
                            className="w-full border border-gray-300 rounded px-3 py-2 text-sm disabled:bg-gray-100"
                            placeholder="e.g. HYA-OCR/Attachments/2025-10/"
                          />
                        </div>
                        <div>
                          <label className="block text-xs text-gray-600 mb-1">Filename Contains</label>
                          <input
                            type="text"
                            value={rule.filename_contains || ''}
                            onChange={(e) => updateAttachmentRule(idx, 'filename_contains', e.target.value)}
                            disabled={mappingForm.inherit_defaults}
                            className="w-full border border-gray-300 rounded px-3 py-2 text-sm disabled:bg-gray-100"
                            placeholder="e.g. INV-"
                          />
                        </div>
                        <div>
                          <label className="block text-xs text-gray-600 mb-1">Join Key</label>
                          <input
                            type="text"
                            value={rule.join_key || ''}
                            onChange={(e) => updateAttachmentRule(idx, 'join_key', e.target.value)}
                            disabled={mappingForm.inherit_defaults}
                            className="w-full border border-gray-300 rounded px-3 py-2 text-sm disabled:bg-gray-100"
                            placeholder="e.g. invoice_no"
                          />
                        </div>
                        <div>
                          <label className="block text-xs text-gray-600 mb-1">Label</label>
                          <input
                            type="text"
                            value={rule.label || ''}
                            onChange={(e) => updateAttachmentRule(idx, 'label', e.target.value)}
                            disabled={mappingForm.inherit_defaults}
                            className="w-full border border-gray-300 rounded px-3 py-2 text-sm disabled:bg-gray-100"
                            placeholder="Optional label"
                          />
                        </div>
                        <div>
                          <label className="block text-xs text-gray-600 mb-1">Metadata (JSON)</label>
                          <input
                            type="text"
                            value={rule.metadata || ''}
                            onChange={(e) => updateAttachmentRule(idx, 'metadata', e.target.value)}
                            disabled={mappingForm.inherit_defaults}
                            className="w-full border border-gray-300 rounded px-3 py-2 text-sm disabled:bg-gray-100"
                            placeholder='e.g. {"month":"202510"}'
                          />
                        </div>
                        <button
                          type="button"
                          onClick={() => removeAttachmentRule(idx)}
                          disabled={mappingForm.inherit_defaults}
                          className="px-3 py-2 text-sm bg-red-600 text-white rounded disabled:bg-gray-300"
                          title="Remove rule"
                        >
                          Remove
                        </button>
                      </div>
                    ))}
                    <button
                      type="button"
                      onClick={addAttachmentRule}
                      disabled={mappingForm.inherit_defaults}
                      className="px-3 py-2 text-sm bg-gray-700 text-white rounded disabled:bg-gray-300"
                    >
                      + Add Rule
                    </button>
                  </div>
                </div>
              )}

              <div>
                <label className="block text-sm font-medium text-gray-700 mb-1">External Join Keys</label>
                <input
                  type="text"
                  value={mappingForm.external_join_keys}
                  onChange={(e) => handleMappingFormChange('external_join_keys', e.target.value)}
                  disabled={mappingForm.inherit_defaults}
                  className="w-full border border-gray-300 rounded px-3 py-2 text-sm disabled:bg-gray-100"
                  placeholder="Comma separated, e.g. phone_number, account_id"
                />
              </div>

              {mappingForm.item_type === 'multi_source' && (
                <div>
                  <label className="block text-sm font-medium text-gray-700 mb-1">Internal Join Key</label>
                  <input
                    type="text"
                    value={mappingForm.internal_join_key}
                    onChange={(e) => handleMappingFormChange('internal_join_key', e.target.value)}
                    disabled={mappingForm.inherit_defaults}
                    className="w-full border border-gray-300 rounded px-3 py-2 text-sm disabled:bg-gray-100"
                    placeholder="Common field between primary and attachments"
                  />
                </div>
              )}

              <div>
                <label className="block text-sm font-medium text-gray-700 mb-1">Column Aliases</label>
                <textarea
                  value={mappingForm.column_aliases}
                  onChange={(e) => handleMappingFormChange('column_aliases', e.target.value)}
                  disabled={mappingForm.inherit_defaults}
                  className="w-full border border-gray-300 rounded px-3 py-2 text-sm disabled:bg-gray-100"
                  placeholder="Format: ocr_field:master_field, ..."
                  rows={2}
                />
              </div>

              {mappingFormError && (
                <div className="text-sm text-red-600">{mappingFormError}</div>
              )}
            </div>

            <div className="mt-6 flex justify-end gap-3">
              <button
                onClick={closeMappingModal}
                className="px-4 py-2 text-sm font-medium text-gray-700 border border-gray-300 rounded hover:bg-gray-50"
                disabled={isSavingMappingConfig}
              >
                Cancel
              </button>
              <button
                onClick={saveMappingConfig}
                disabled={isSavingMappingConfig}
                className="px-4 py-2 text-sm font-medium text-white bg-blue-600 hover:bg-blue-700 rounded disabled:bg-blue-300"
              >
                {isSavingMappingConfig ? 'Saving...' : 'Save Mapping'}
              </button>
            </div>
          </div>
        </div>
      )}
    </div>
  );
}
