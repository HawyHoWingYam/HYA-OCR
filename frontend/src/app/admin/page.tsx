export default function AdminHome() {
  return (
    <div>
      <h1 className="text-2xl font-bold mb-6">Admin Dashboard</h1>
      
      <div className="grid grid-cols-1 md:grid-cols-2 lg:grid-cols-3 gap-6">
        <div className="bg-white shadow-md rounded-lg p-6">
          <h2 className="text-lg font-medium mb-2">Companies</h2>
          <p className="text-gray-600 mb-4">Manage companies and their details.</p>
          <a 
            href="/admin/companies" 
            className="text-blue-600 hover:text-blue-800 font-medium"
          >
            Manage Companies →
          </a>
        </div>
        
        <div className="bg-white shadow-md rounded-lg p-6">
          <h2 className="text-lg font-medium mb-2">Document Types</h2>
          <p className="text-gray-600 mb-4">Manage document types and their schemas.</p>
          <a 
            href="/admin/document-types" 
            className="text-blue-600 hover:text-blue-800 font-medium"
          >
            Manage Document Types →
          </a>
        </div>

        <div className="bg-white shadow-md rounded-lg p-6">
          <h2 className="text-lg font-medium mb-2">Configurations</h2>
          <p className="text-gray-600 mb-4">Link companies with document types and configure processing parameters.</p>
          <a 
            href="/admin/ocr-configs" 
            className="text-blue-600 hover:text-blue-800 font-medium"
          >
            Manage Configurations →
          </a>
        </div>

      </div>
    </div>
  );
}
