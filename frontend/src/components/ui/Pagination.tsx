'use client';

import type { PaginationInfo } from '@/types/pagination';

interface PaginationProps {
  pagination: PaginationInfo;
  onPageChange: (page: number) => void;
  maxPages?: number;
  disabled?: boolean;
}

export default function Pagination({ pagination, onPageChange, maxPages = 5, disabled = false }: PaginationProps) {
  const { current_page, total_pages, has_prev, has_next, page_size, total_count } = pagination;
  
  // Limit total pages to maxPages
  const effectiveTotalPages = Math.min(total_pages, maxPages);
  
  // Generate page numbers to display
  const generatePageNumbers = () => {
    const pages: number[] = [];
    const startPage = Math.max(1, Math.min(current_page - 2, effectiveTotalPages - 4));
    const endPage = Math.min(effectiveTotalPages, startPage + 4);
    
    for (let i = startPage; i <= endPage; i++) {
      pages.push(i);
    }
    
    return pages;
  };

  const pageNumbers = generatePageNumbers();

  if (effectiveTotalPages <= 1) {
    return null; // Don't show pagination if there's only one page or less
  }

  return (
    <div className="flex items-center justify-between px-4 py-3 bg-white border-t border-gray-200 sm:px-6">
      <div className="flex justify-between flex-1 sm:hidden">
        {/* Mobile pagination */}
        <button
          onClick={() => onPageChange(current_page - 1)}
          disabled={!has_prev || disabled}
          className="relative inline-flex items-center px-4 py-2 text-sm font-medium text-gray-700 bg-white border border-gray-300 rounded-md hover:bg-gray-50 disabled:opacity-50 disabled:cursor-not-allowed"
        >
          Previous
        </button>
        <button
          onClick={() => onPageChange(current_page + 1)}
          disabled={!has_next || current_page >= maxPages || disabled}
          className="relative ml-3 inline-flex items-center px-4 py-2 text-sm font-medium text-gray-700 bg-white border border-gray-300 rounded-md hover:bg-gray-50 disabled:opacity-50 disabled:cursor-not-allowed"
        >
          Next
        </button>
      </div>

      <div className="hidden sm:flex sm:flex-1 sm:items-center sm:justify-between">
        <div>
          <p className="text-sm text-gray-700">
            Showing{' '}
            <span className="font-medium">{Math.min((current_page - 1) * page_size + 1, total_count || 0)}</span>{' '}
            to{' '}
            <span className="font-medium">
              {Math.min(current_page * page_size, total_count || 0)}
            </span>{' '}
            of{' '}
            <span className="font-medium">
              {Math.min(total_count || 0, maxPages * page_size)}
            </span>{' '}
            results
            {(total_count || 0) > maxPages * page_size && (
              <span className="text-gray-500"> (showing first {maxPages * page_size})</span>
            )}
          </p>
        </div>

        <div>
          <nav className="relative z-0 inline-flex rounded-md shadow-sm -space-x-px" aria-label="Pagination">
            {/* Previous button */}
            <button
              onClick={() => onPageChange(current_page - 1)}
              disabled={!has_prev || disabled}
              className="relative inline-flex items-center px-2 py-2 text-sm font-medium text-gray-500 bg-white border border-gray-300 rounded-l-md hover:bg-gray-50 disabled:opacity-50 disabled:cursor-not-allowed"
            >
              <span className="sr-only">Previous</span>
              <svg className="w-5 h-5" xmlns="http://www.w3.org/2000/svg" viewBox="0 0 20 20" fill="currentColor">
                <path fillRule="evenodd" d="M12.707 5.293a1 1 0 010 1.414L9.414 10l3.293 3.293a1 1 0 01-1.414 1.414l-4-4a1 1 0 010-1.414l4-4a1 1 0 011.414 0z" clipRule="evenodd" />
              </svg>
            </button>

            {/* Page numbers */}
            {pageNumbers.map((pageNum) => (
              <button
                key={pageNum}
                onClick={() => onPageChange(pageNum)}
                disabled={disabled}
                className={`relative inline-flex items-center px-4 py-2 text-sm font-medium border disabled:opacity-50 disabled:cursor-not-allowed ${
                  pageNum === current_page
                    ? 'z-10 bg-blue-50 border-blue-500 text-blue-600'
                    : 'bg-white border-gray-300 text-gray-500 hover:bg-gray-50'
                }`}
              >
                {pageNum}
              </button>
            ))}

            {/* Show ellipsis and max page if we're truncated */}
            {effectiveTotalPages > 5 && current_page < effectiveTotalPages - 2 && (
              <>
                <span className="relative inline-flex items-center px-4 py-2 text-sm font-medium text-gray-700 bg-white border border-gray-300">
                  ...
                </span>
                <button
                  onClick={() => onPageChange(effectiveTotalPages)}
                  disabled={disabled}
                  className="relative inline-flex items-center px-4 py-2 text-sm font-medium text-gray-500 bg-white border border-gray-300 hover:bg-gray-50 disabled:opacity-50 disabled:cursor-not-allowed"
                >
                  {effectiveTotalPages}
                </button>
              </>
            )}

            {/* Next button */}
            <button
              onClick={() => onPageChange(current_page + 1)}
              disabled={!has_next || current_page >= maxPages || disabled}
              className="relative inline-flex items-center px-2 py-2 text-sm font-medium text-gray-500 bg-white border border-gray-300 rounded-r-md hover:bg-gray-50 disabled:opacity-50 disabled:cursor-not-allowed"
            >
              <span className="sr-only">Next</span>
              <svg className="w-5 h-5" xmlns="http://www.w3.org/2000/svg" viewBox="0 0 20 20" fill="currentColor">
                <path fillRule="evenodd" d="M7.293 14.707a1 1 0 010-1.414L10.586 10 7.293 6.707a1 1 0 011.414-1.414l4 4a1 1 0 010 1.414l-4 4a1 1 0 01-1.414 0z" clipRule="evenodd" />
              </svg>
            </button>
          </nav>
        </div>
      </div>
    </div>
  );
}
