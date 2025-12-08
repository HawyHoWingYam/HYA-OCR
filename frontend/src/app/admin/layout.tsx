import Link from 'next/link';

export default function AdminLayout({
  children,
}: {
  children: React.ReactNode
}) {
  return (
    <div className="flex">
      {/* Admin Sidebar */}
      <aside className="w-64 bg-slate-800 text-white min-h-screen p-4">
        <div className="mb-8">
          <h2 className="text-xl font-bold">Admin Portal</h2>
        </div>
        <nav className="space-y-1">
          <Link 
            href="/admin/companies" 
            className="block py-2 px-4 rounded hover:bg-slate-700"
          >
            Companies
          </Link>
          <Link
            href="/admin/document-types"
            className="block py-2 px-4 rounded hover:bg-slate-700"
          >
            Document Types
          </Link>
          <Link
            href="/admin/ocr-configs"
            className="block py-2 px-4 rounded hover:bg-slate-700"
          >
            OCR Configs (Unified)
          </Link>
          <Link
            href="/admin/usage"
            className="block py-2 px-4 rounded hover:bg-slate-700"
          >
            API Usage
          </Link>
          <Link
            href="/admin/awb/sync"
            className="block py-2 px-4 rounded hover:bg-slate-700"
          >
            OneDrive Sync
          </Link>
          <div className="pt-4 mt-4 border-t border-slate-600">
            <Link 
              href="/" 
              className="block py-2 px-4 rounded hover:bg-slate-700"
            >
              Back to Main Site
            </Link>
          </div>
        </nav>
      </aside>
      
      {/* Main Content */}
      <main className="flex-1 p-8">
        {children}
      </main>
    </div>
  );
} 
