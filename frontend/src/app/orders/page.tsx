'use client';

import { useState, useEffect } from 'react';
import Link from 'next/link';
import { useRouter } from 'next/navigation';
import { DocumentType } from '@/lib/api';
import { applyOrderUpdateToOrder } from '@/lib/orderUpdateHelpers';

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
  mapping_file_path: string | null;
  mapping_keys: string[] | null;
  final_report_paths: any | null;
  created_at: string;
  updated_at: string;
}

interface PaginationInfo {
  total_count: number;
  total_pages: number;
  current_page: number;
  page_size: number;
  has_next: boolean;
  has_prev: boolean;
}

export default function OrdersPage() {
  const router = useRouter();
  const [orders, setOrders] = useState<Order[]>([]);
  const [pagination, setPagination] = useState<PaginationInfo | null>(null);
  const [currentPage, setCurrentPage] = useState(1);
  const [isLoading, setIsLoading] = useState(true);
  const [isPageLoading, setIsPageLoading] = useState(false);
  const [error, setError] = useState('');
  const [statusFilter, setStatusFilter] = useState('');
  const [showCreateOrderModal, setShowCreateOrderModal] = useState(false);
  const [newOrderName, setNewOrderName] = useState('');
  const [primaryDocTypeId, setPrimaryDocTypeId] = useState<number | ''>('');
  const [docTypes, setDocTypes] = useState<DocumentType[]>([]);
  const [isLoadingDocTypes, setIsLoadingDocTypes] = useState(false);
  const [isCreatingOrder, setIsCreatingOrder] = useState(false);
  const [createOrderError, setCreateOrderError] = useState('');

  const loadOrders = async (page: number = 1, status: string = '', isPageChange: boolean = false) => {
    if (isPageChange) {
      setIsPageLoading(true);
    } else {
      setIsLoading(true);
    }

    try {
      const offset = (page - 1) * 20;
      const params = new URLSearchParams({
        limit: '20',
        offset: offset.toString()
      });

      if (status) {
        params.append('status', status);
      }

      const response = await fetch(`/api/orders?${params}`);
      if (!response.ok) {
        throw new Error('Failed to fetch orders');
      }

      const data = await response.json();
      setOrders(data.data);
      setPagination(data.pagination);
      setCurrentPage(page);
      setError('');
    } catch (error) {
      console.error('Error fetching orders:', error);
      setError('Failed to load orders');
    } finally {
      setIsLoading(false);
      setIsPageLoading(false);
    }
  };

  const loadDocumentTypes = async () => {
    try {
      setIsLoadingDocTypes(true);
      const response = await fetch('/api/document-types');
      if (!response.ok) {
        throw new Error('Failed to fetch document types');
      }
      const data = await response.json();
      setDocTypes(data);
    } catch (err) {
      console.error('Error fetching document types:', err);
    } finally {
      setIsLoadingDocTypes(false);
    }
  };

  useEffect(() => {
    loadOrders(currentPage, statusFilter);

    // Set up refresh interval
    const refreshInterval = setInterval(() => {
      const offset = (currentPage - 1) * 20;
      const params = new URLSearchParams({
        limit: '20',
        offset: offset.toString()
      });

      if (statusFilter) {
        params.append('status', statusFilter);
      }

      fetch(`/api/orders?${params}`)
        .then(response => response.json())
        .then(data => {
          setOrders(data.data);
          setPagination(data.pagination);
        })
        .catch(err => console.error('Error refreshing orders:', err));
    }, 30000);

    return () => clearInterval(refreshInterval);
  }, [currentPage, statusFilter]);

  // WebSocket for real-time order summary updates
  useEffect(() => {
    let ws: WebSocket | null = null;
    try {
      const httpBase =
        process.env.NEXT_PUBLIC_API_URL ||
        (process as any).env?.API_BASE_URL ||
        'http://localhost:8000';
      const apiUrl = new URL(httpBase);
      const wsProtocol = apiUrl.protocol === 'https:' ? 'wss:' : 'ws:';
      const wsUrl = `${wsProtocol}//${apiUrl.host}/ws/orders/summary`;
      ws = new WebSocket(wsUrl);

      ws.onmessage = (event) => {
        try {
          const msg = JSON.parse(event.data);
          if (!msg || msg.type !== 'order_update' || typeof msg.order_id !== 'number') {
            return;
          }
          setOrders((prev) =>
            prev.map((order) => applyOrderUpdateToOrder(order, msg))
          );
        } catch {
          // ignore malformed messages
        }
      };
    } catch {
      // ignore WS failures; HTTP polling remains as fallback
    }

    return () => {
      if (ws) {
        try {
          ws.close();
        } catch {
          // ignore
        }
      }
    };
  }, []);

  useEffect(() => {
    loadDocumentTypes();
  }, []);

  const handlePageChange = (page: number) => {
    if (page !== currentPage && page >= 1 && (!pagination || page <= pagination.total_pages)) {
      loadOrders(page, statusFilter, true);
    }
  };

  const handleStatusFilterChange = (status: string) => {
    setStatusFilter(status);
    setCurrentPage(1);
    loadOrders(1, status);
  };

  const openCreateOrderModal = () => {
    const defaultName = `Order ${new Date().toLocaleDateString()}`;
    setNewOrderName(defaultName);
    setPrimaryDocTypeId('');
    setCreateOrderError('');
    setShowCreateOrderModal(true);
  };

  const closeCreateOrderModal = () => {
    setShowCreateOrderModal(false);
    setCreateOrderError('');
  };

  const handleCreateOrder = async () => {
    setCreateOrderError('');
    setIsCreatingOrder(true);
    try {
      const payload: Record<string, unknown> = {};
      const trimmedName = newOrderName.trim();
      if (trimmedName) {
        payload.order_name = trimmedName;
      }
      if (typeof primaryDocTypeId === 'number') {
        payload.primary_doc_type_id = primaryDocTypeId;
      }

      const response = await fetch(`/api/orders`, {
        method: 'POST',
        headers: {
          'Content-Type': 'application/json',
        },
        body: JSON.stringify(payload),
      });

      if (!response.ok) {
        const errorBody = await response.json().catch(() => ({}));
        throw new Error(errorBody.detail || 'Failed to create order');
      }

      const data = await response.json();
      setShowCreateOrderModal(false);
      setNewOrderName('');
      setPrimaryDocTypeId('');
      router.push(`/orders/${data.order_id}`);
    } catch (error) {
      console.error('Error creating order:', error);
      setCreateOrderError(error instanceof Error ? error.message : 'Failed to create order');
    } finally {
      setIsCreatingOrder(false);
    }
  };

  const getStatusColor = (status: string) => {
    switch (status) {
      case 'DRAFT':
        return 'bg-gray-100 text-gray-800';
      case 'PROCESSING':
        return 'bg-blue-100 text-blue-800';
      case 'MAPPING':
        return 'bg-yellow-100 text-yellow-800';
      case 'COMPLETED':
        return 'bg-green-100 text-green-800';
      case 'FAILED':
        return 'bg-red-100 text-red-800';
      default:
        return 'bg-gray-100 text-gray-800';
    }
  };

  const getProgressPercentage = (order: Order) => {
    if (order.total_items === 0) return 0;
    return Math.round((order.completed_items / order.total_items) * 100);
  };

  const getAttachmentProgress = (order: Order) => {
    if (!order.total_attachments || order.total_attachments === 0) return 0;
    return Math.round((order.completed_attachments / order.total_attachments) * 100);
  };

  const selectedPrimaryDocType =
    typeof primaryDocTypeId === 'number'
      ? docTypes.find((dt) => dt.doc_type_id === primaryDocTypeId)
      : null;

  return (
    <div className="container mx-auto px-4 py-8">
      <div className="flex items-center justify-between mb-8">
        <h1 className="text-2xl font-bold">OCR Orders</h1>
        <button
          onClick={openCreateOrderModal}
          className="bg-blue-600 hover:bg-blue-700 text-white py-2 px-6 rounded"
        >
          Create New Order
        </button>
      </div>

      {error && (
        <div className="bg-red-100 border-l-4 border-red-500 text-red-700 p-4 mb-6">
          {error}
        </div>
      )}

      {/* Status Filter */}
      <div className="mb-6">
        <div className="flex items-center gap-4">
          <span className="text-sm font-medium text-gray-700">Filter by status:</span>
          <select
            value={statusFilter}
            onChange={(e) => handleStatusFilterChange(e.target.value)}
            className="border border-gray-300 rounded px-3 py-1 text-sm"
          >
            <option value="">All Status</option>
            <option value="DRAFT">Draft</option>
            <option value="PROCESSING">Processing</option>
            <option value="MAPPING">Mapping</option>
            <option value="COMPLETED">Completed</option>
            <option value="FAILED">Failed</option>
          </select>
        </div>
      </div>

      <div className="bg-white rounded-lg shadow overflow-hidden">
        {isLoading && orders.length === 0 ? (
          <div className="text-center py-10">Loading orders...</div>
        ) : (
          <>
            {isPageLoading && (
              <div className="bg-blue-50 border-l-4 border-blue-400 text-blue-700 p-4">
                <div className="flex items-center">
                  <div className="animate-spin rounded-full h-4 w-4 border-b-2 border-blue-600 mr-2"></div>
                  Loading page {currentPage}...
                </div>
              </div>
            )}
            <table className="min-w-full divide-y divide-gray-200">
              <thead className="bg-gray-50">
                <tr>
                  <th scope="col" className="px-6 py-3 text-left text-xs font-medium text-gray-500 uppercase tracking-wider">
                    Order ID
                  </th>
                  <th scope="col" className="px-6 py-3 text-left text-xs font-medium text-gray-500 uppercase tracking-wider">
                    Order Name
                  </th>
                  <th scope="col" className="px-6 py-3 text-left text-xs font-medium text-gray-500 uppercase tracking-wider">
                    Status
                  </th>
                  <th scope="col" className="px-6 py-3 text-left text-xs font-medium text-gray-500 uppercase tracking-wider">
                    Progress
                  </th>
                  <th scope="col" className="px-6 py-3 text-left text-xs font-medium text-gray-500 uppercase tracking-wider">
                    Items
                  </th>
                  <th scope="col" className="px-6 py-3 text-left text-xs font-medium text-gray-500 uppercase tracking-wider">
                    Created
                  </th>
                  <th scope="col" className="px-6 py-3 text-left text-xs font-medium text-gray-500 uppercase tracking-wider">
                    Actions
                  </th>
                </tr>
              </thead>
              <tbody className="bg-white divide-y divide-gray-200">
                {orders.map((order) => (
                  <tr key={order.order_id} className="hover:bg-gray-50">
                    <td className="px-6 py-4 whitespace-nowrap text-gray-900 font-medium">
                      {order.order_id}
                    </td>
                    <td className="px-6 py-4 whitespace-nowrap text-gray-500">
                      {order.order_name || `Order ${order.order_id}`}
                    </td>
                    <td className="px-6 py-4 whitespace-nowrap">
                      <span className={`px-2 py-1 inline-flex text-xs leading-5 font-semibold rounded-full ${getStatusColor(order.status)}`}>
                        {order.status}
                      </span>
                    </td>
                    <td className="px-6 py-4 whitespace-nowrap">
                      <div className="flex flex-col text-sm">
                        <div className="flex items-center mb-1">
                          <span className="text-gray-900">
                            {order.completed_items}/{order.total_items}
                          </span>
                          {order.status === 'PROCESSING' && order.total_items > 0 && (
                            <div className="ml-2 w-16 bg-gray-200 rounded-full h-2">
                              <div
                                className="bg-blue-600 h-2 rounded-full transition-all duration-300"
                                style={{ width: `${getProgressPercentage(order)}%` }}
                              ></div>
                            </div>
                          )}
                        </div>
                        {order.total_attachments > 0 && (
                          <div className="text-xs text-gray-500">
                            Attach: {order.completed_attachments}/{order.total_attachments} (
                            {getAttachmentProgress(order)}%)
                          </div>
                        )}
                      </div>
                    </td>
                    <td className="px-6 py-4 whitespace-nowrap text-gray-500">
                      <div className="text-sm">
                        <div>Total: {order.total_items}</div>
                        {order.failed_items > 0 && (
                          <div className="text-red-600">Failed: {order.failed_items}</div>
                        )}
                        {order.total_attachments > 0 && (
                          <div className="text-xs text-gray-500 mt-1">
                            Attachments: {order.total_attachments}{" "}
                            {order.failed_attachments > 0 && (
                              <span className="text-red-600">
                                (Failed: {order.failed_attachments})
                              </span>
                            )}
                          </div>
                        )}
                      </div>
                    </td>
                    <td className="px-6 py-4 whitespace-nowrap text-gray-500">
                      {new Date(order.created_at).toLocaleString()}
                    </td>
                    <td className="px-6 py-4 whitespace-nowrap text-blue-600 hover:text-blue-800">
                      <Link href={`/orders/${order.order_id}`}>
                        View Details
                      </Link>
                    </td>
                  </tr>
                ))}
                {!isLoading && orders.length === 0 && (
                  <tr>
                    <td colSpan={7} className="px-6 py-4 text-center text-sm text-gray-500">
                      No orders found. Create your first order to get started!
                    </td>
                  </tr>
                )}
              </tbody>
            </table>

            {/* Pagination */}
            {pagination && pagination.total_pages > 1 && (
              <div className="bg-white px-4 py-3 flex items-center justify-between border-t border-gray-200 sm:px-6">
                <div className="flex-1 flex justify-between sm:hidden">
                  <button
                    onClick={() => handlePageChange(currentPage - 1)}
                    disabled={!pagination.has_prev || isPageLoading}
                    className="relative inline-flex items-center px-4 py-2 border border-gray-300 text-sm font-medium rounded-md text-gray-700 bg-white hover:bg-gray-50 disabled:opacity-50 disabled:cursor-not-allowed"
                  >
                    Previous
                  </button>
                  <button
                    onClick={() => handlePageChange(currentPage + 1)}
                    disabled={!pagination.has_next || isPageLoading}
                    className="ml-3 relative inline-flex items-center px-4 py-2 border border-gray-300 text-sm font-medium rounded-md text-gray-700 bg-white hover:bg-gray-50 disabled:opacity-50 disabled:cursor-not-allowed"
                  >
                    Next
                  </button>
                </div>
                <div className="hidden sm:flex-1 sm:flex sm:items-center sm:justify-between">
                  <div>
                    <p className="text-sm text-gray-700">
                      Showing <span className="font-medium">{((currentPage - 1) * 20) + 1}</span> to{' '}
                      <span className="font-medium">
                        {Math.min(currentPage * 20, pagination.total_count)}
                      </span>{' '}
                      of <span className="font-medium">{pagination.total_count}</span> results
                    </p>
                  </div>
                  <div>
                    <nav className="relative z-0 inline-flex rounded-md shadow-sm -space-x-px" aria-label="Pagination">
                      <button
                        onClick={() => handlePageChange(currentPage - 1)}
                        disabled={!pagination.has_prev || isPageLoading}
                        className="relative inline-flex items-center px-2 py-2 rounded-l-md border border-gray-300 bg-white text-sm font-medium text-gray-500 hover:bg-gray-50 disabled:opacity-50 disabled:cursor-not-allowed"
                      >
                        Previous
                      </button>
                      {Array.from({ length: Math.min(pagination.total_pages, 5) }, (_, i) => {
                        const page = i + Math.max(1, currentPage - 2);
                        if (page > pagination.total_pages) return null;
                        return (
                          <button
                            key={page}
                            onClick={() => handlePageChange(page)}
                            disabled={isPageLoading}
                            className={`relative inline-flex items-center px-4 py-2 border text-sm font-medium ${
                              page === currentPage
                                ? 'z-10 bg-blue-50 border-blue-500 text-blue-600'
                                : 'bg-white border-gray-300 text-gray-500 hover:bg-gray-50'
                            } disabled:opacity-50 disabled:cursor-not-allowed`}
                          >
                            {page}
                          </button>
                        );
                      })}
                      <button
                        onClick={() => handlePageChange(currentPage + 1)}
                        disabled={!pagination.has_next || isPageLoading}
                        className="relative inline-flex items-center px-2 py-2 rounded-r-md border border-gray-300 bg-white text-sm font-medium text-gray-500 hover:bg-gray-50 disabled:opacity-50 disabled:cursor-not-allowed"
                      >
                        Next
                      </button>
                    </nav>
                  </div>
                </div>
              </div>
            )}
          </>
        )}
      </div>

      {showCreateOrderModal && (
        <div className="fixed inset-0 z-50 flex items-center justify-center bg-black bg-opacity-50">
          <div className="bg-white rounded-lg shadow-xl w-full max-w-lg p-6">
            <div className="flex items-center justify-between mb-4">
              <h2 className="text-xl font-semibold text-gray-900">Create New Order</h2>
              <button
                onClick={closeCreateOrderModal}
                className="text-gray-500 hover:text-gray-700"
                aria-label="Close create order modal"
              >
                ✕
              </button>
            </div>

            <div className="space-y-4">
              <div>
                <label className="block text-sm font-medium text-gray-700 mb-1">Order Name</label>
                <input
                  type="text"
                  value={newOrderName}
                  onChange={(e) => setNewOrderName(e.target.value)}
                  className="w-full border border-gray-300 rounded px-3 py-2"
                />
                <p className="text-xs text-gray-500 mt-1">Optional. Leave blank for an auto-generated name.</p>
              </div>

              <div>
                <label className="block text-sm font-medium text-gray-700 mb-1">Primary Document Type</label>
                <select
                  value={primaryDocTypeId === '' ? '' : primaryDocTypeId}
                  onChange={(e) => {
                    const value = e.target.value;
                    if (value === '') {
                      setPrimaryDocTypeId('');
                    } else {
                      setPrimaryDocTypeId(parseInt(value, 10));
                    }
                  }}
                  disabled={isLoadingDocTypes}
                  className="w-full border border-gray-300 rounded px-3 py-2"
                >
                  <option value="">None selected</option>
                  {docTypes.map((docType) => (
                    <option key={docType.doc_type_id} value={docType.doc_type_id}>
                      {docType.type_name} {docType.has_template ? '(Template ready)' : '(No template)'}
                    </option>
                  ))}
                </select>
                {isLoadingDocTypes && (
                  <p className="text-xs text-gray-500 mt-1">Loading document types…</p>
                )}
                <p className="text-xs text-gray-500 mt-1">
                  Selecting a type with a template will enable automated special CSV generation.
                </p>
                {selectedPrimaryDocType && (
                  <div className={`mt-2 text-xs ${selectedPrimaryDocType.has_template ? 'text-green-600' : 'text-gray-500'}`}>
                    {selectedPrimaryDocType.has_template
                      ? `Template configured${selectedPrimaryDocType.template_version ? ` (v${selectedPrimaryDocType.template_version})` : ''}`
                      : 'This document type does not currently have a template.'}
                  </div>
                )}
              </div>

              {createOrderError && (
                <div className="bg-red-100 border border-red-400 text-red-700 px-4 py-2 rounded text-sm">
                  {createOrderError}
                </div>
              )}
            </div>

            <div className="flex justify-end gap-3 mt-6">
              <button
                onClick={closeCreateOrderModal}
                className="px-4 py-2 text-gray-700 border border-gray-300 rounded hover:bg-gray-50"
              >
                Cancel
              </button>
              <button
                onClick={handleCreateOrder}
                disabled={isCreatingOrder}
                className="px-4 py-2 bg-blue-600 text-white rounded hover:bg-blue-700 disabled:bg-gray-400"
              >
                {isCreatingOrder ? 'Creating...' : 'Create Order'}
              </button>
            </div>
          </div>
        </div>
      )}
    </div>
  );
}
