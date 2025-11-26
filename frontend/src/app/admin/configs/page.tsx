'use client';

import { useState, useEffect } from 'react';
import { DocumentType, Company, uploadFile } from '@/lib/api';
import SmartDeleteDialog from '@/components/ui/SmartDeleteDialog';

interface ConfigType {
  config_id: number;
  company_id: number;
  doc_type_id: number;
  prompt_path: string;
  schema_path: string;
  active: boolean;
  created_at: string;
  updated_at: string;
  company_name?: string;
  type_name?: string;
}

const API_BASE_URL = '/api';

async function fetchConfigs(): Promise<ConfigType[]> {
  const res = await fetch(`${API_BASE_URL}/configs`);
  if (!res.ok) throw new Error('Failed to fetch configurations');
  return res.json();
}

async function createConfig(data: { company_id: number; doc_type_id: number; prompt_path: string; schema_path: string; active: boolean; }): Promise<ConfigType> {
  const res = await fetch(`${API_BASE_URL}/configs`, { method: 'POST', headers: { 'Content-Type': 'application/json' }, body: JSON.stringify(data) });
  if (!res.ok) throw new Error('Failed to create configuration');
  return res.json();
}

async function updateConfig(id: number, data: { prompt_path: string; schema_path: string; active: boolean; }): Promise<ConfigType> {
  const res = await fetch(`${API_BASE_URL}/configs/${id}`, { method: 'PUT', headers: { 'Content-Type': 'application/json' }, body: JSON.stringify(data) });
  if (!res.ok) throw new Error('Failed to update configuration');
  return res.json();
}

async function deleteConfig(id: number): Promise<void> {
  const res = await fetch(`${API_BASE_URL}/configs/${id}`, { method: 'DELETE' });
  if (!res.ok) throw new Error('Failed to delete configuration');
}

export default function ConfigsPage() {
  const [configs, setConfigs] = useState<ConfigType[]>([]);
  const [companies, setCompanies] = useState<Company[]>([]);
  const [documentTypes, setDocumentTypes] = useState<DocumentType[]>([]);
  const [isLoading, setIsLoading] = useState(true);
  const [error, setError] = useState('');
  const [isCreating, setIsCreating] = useState(false);
  const [editingConfig, setEditingConfig] = useState<ConfigType | null>(null);
  const [formData, setFormData] = useState({ company_id: 0, doc_type_id: 0, prompt_path: '', schema_path: '', active: true });
  const [promptFile, setPromptFile] = useState<File | null>(null);
  const [schemaFile, setSchemaFile] = useState<File | null>(null);
  const [deleteDialog, setDeleteDialog] = useState<{ isOpen: boolean; entity: { type: 'config'; id: number; name: string } | null; }>({ isOpen: false, entity: null });

  useEffect(() => {
    const load = async () => {
      try {
        setIsLoading(true);
        const [cfgs, comps, docs] = await Promise.all([
          fetchConfigs(),
          fetch(`${API_BASE_URL}/companies`).then(r => r.json()),
          fetch(`${API_BASE_URL}/document-types`).then(r => r.json()),
        ]);
        setConfigs(cfgs);
        setCompanies(comps);
        setDocumentTypes(docs);
      } catch (e) {
        setError('Failed to load data');
        console.error(e);
      } finally { setIsLoading(false); }
    };
    load();
  }, []);

  const handleInputChange = (e: React.ChangeEvent<HTMLInputElement | HTMLSelectElement>) => {
    const { name, value, type, checked } = e.target as HTMLInputElement;
    setFormData(prev => ({
      ...prev,
      [name]: type === 'checkbox' ? checked : (name === 'company_id' || name === 'doc_type_id') ? parseInt(value) : value,
    }));
  };

  const handleFileChange = (e: React.ChangeEvent<HTMLInputElement>, which: 'prompt'|'schema') => {
    if (!e.target.files || !e.target.files[0]) return;
    which === 'prompt' ? setPromptFile(e.target.files[0]) : setSchemaFile(e.target.files[0]);
  };

  const handleEdit = (cfg: ConfigType) => {
    setEditingConfig(cfg);
    setFormData({ company_id: cfg.company_id, doc_type_id: cfg.doc_type_id, prompt_path: cfg.prompt_path, schema_path: cfg.schema_path, active: cfg.active });
    setIsCreating(false);
    setPromptFile(null); setSchemaFile(null);
  };

  // Download current config's prompt/schema via backend endpoint
  const downloadConfigFile = async (type: 'prompt' | 'schema') => {
    try {
      if (!editingConfig) return;
      const res = await fetch(`${API_BASE_URL}/configs/${editingConfig.config_id}/download/${type}`);
      if (!res.ok) {
        const err = await res.json().catch(() => ({} as any));
        throw new Error(err.detail || `Failed to download ${type}`);
      }
      const blob = await res.blob();
      const url = window.URL.createObjectURL(blob);
      const a = document.createElement('a');
      a.href = url;
      // Prefer filename from Content-Disposition
      const cd = res.headers.get('content-disposition') || '';
      const m = /filename="?([^";]+)"?/i.exec(cd);
      const fallback = type === 'prompt'
        ? (editingConfig.prompt_path?.split('/').pop() || 'prompt.txt')
        : (editingConfig.schema_path?.split('/').pop() || 'schema.json');
      a.download = (m && m[1]) || fallback;
      document.body.appendChild(a);
      a.click();
      a.remove();
      window.URL.revokeObjectURL(url);
    } catch (e) {
      console.error(e);
      alert(e instanceof Error ? e.message : 'Download failed');
    }
  };

  const handleSubmit = async (e: React.FormEvent) => {
    e.preventDefault();
    try {
      const data = { ...formData } as typeof formData;
      if (promptFile) {
        const company = companies.find(c => c.company_id === formData.company_id);
        const doc = documentTypes.find(d => d.doc_type_id === formData.doc_type_id);
        if (!company || !doc) throw new Error('Invalid company/doc type');
        const p = `document_type/${doc.doc_type_id}/${company.company_id}/prompt/${promptFile.name}`;
        data.prompt_path = await uploadFile(promptFile, p);
      }
      if (schemaFile) {
        const company = companies.find(c => c.company_id === formData.company_id);
        const doc = documentTypes.find(d => d.doc_type_id === formData.doc_type_id);
        if (!company || !doc) throw new Error('Invalid company/doc type');
        const p = `document_type/${doc.doc_type_id}/${company.company_id}/schema/${schemaFile.name}`;
        data.schema_path = await uploadFile(schemaFile, p);
      }
      if (editingConfig) {
        const updated = await updateConfig(editingConfig.config_id, { prompt_path: data.prompt_path, schema_path: data.schema_path, active: data.active });
        setConfigs(prev => prev.map(c => c.config_id === editingConfig.config_id ? updated : c));
        setEditingConfig(null);
      } else {
        const created = await createConfig(data);
        setConfigs(prev => [...prev, created]);
        setIsCreating(false);
      }
      setFormData({ company_id: 0, doc_type_id: 0, prompt_path: '', schema_path: '', active: true });
      setPromptFile(null); setSchemaFile(null);
    } catch (e) {
      setError(`Failed to ${editingConfig ? 'update' : 'create'} configuration`);
      console.error(e);
    }
  };

  const handleDelete = (cfg: ConfigType) => {
    const name = `${cfg.company_name || 'Unknown Company'} - ${cfg.type_name || 'Unknown Type'}`;
    setDeleteDialog({ isOpen: true, entity: { type: 'config', id: cfg.config_id, name } });
  };
  const handleDeleteSuccess = () => setConfigs(prev => prev.filter(c => c.config_id !== deleteDialog.entity?.id));
  const handleDeleteCancel = () => setDeleteDialog({ isOpen: false, entity: null });
  const handleCancel = () => { setIsCreating(false); setEditingConfig(null); setFormData({ company_id: 0, doc_type_id: 0, prompt_path: '', schema_path: '', active: true }); setPromptFile(null); setSchemaFile(null); };
  const formatFilePath = (p?: string) => p || '—';
  const getCompanyName = (id: number) => companies.find(c => c.company_id === id)?.company_name || 'Unknown';
  const getDocTypeName = (id: number) => documentTypes.find(d => d.doc_type_id === id)?.type_name || 'Unknown';

  return (
    <div>
      <div className="flex justify-between items-center mb-6">
        <h1 className="text-2xl font-bold">Document Configuration Management</h1>
        {!isCreating && !editingConfig && (
          <button onClick={() => setIsCreating(true)} className="bg-blue-600 text-white px-4 py-2 rounded hover:bg-blue-700">Add New Configuration</button>
        )}
      </div>

      {error && (<div className="bg-red-100 border border-red-400 text-red-700 px-4 py-3 rounded mb-4">{error}</div>)}

      {(isCreating || editingConfig) && (
        <div className="bg-white shadow-md rounded-lg p-6 mb-6">
          <h2 className="text-lg font-medium mb-4">{editingConfig ? 'Edit Configuration' : 'Create New Configuration'}</h2>
          <form onSubmit={handleSubmit} className="space-y-4">
            <div className="grid grid-cols-2 gap-4">
              <div>
                <label className="block text-gray-700 mb-2">Company</label>
                <select name="company_id" value={formData.company_id} onChange={handleInputChange} className="w-full border border-gray-300 rounded px-3 py-2" required disabled={!!editingConfig}>
                  <option value="">Select Company</option>
                  {companies.map(c => (<option key={c.company_id} value={c.company_id}>{c.company_name}</option>))}
                </select>
              </div>
              <div>
                <label className="block text-gray-700 mb-2">Document Type</label>
                <select name="doc_type_id" value={formData.doc_type_id} onChange={handleInputChange} className="w-full border border-gray-300 rounded px-3 py-2" required disabled={!!editingConfig}>
                  <option value="">Select Document Type</option>
                  {documentTypes.map(d => (<option key={d.doc_type_id} value={d.doc_type_id}>{d.type_name}</option>))}
                </select>
              </div>
            </div>

            <div>
              <label className="block text-gray-700 mb-2">Prompt File(txt)</label>
              {editingConfig && !promptFile ? (
                <div className="flex items-center mb-2">
                  <span className="text-gray-600 mr-2 text-sm" title={formData.prompt_path}>Current: {formatFilePath(formData.prompt_path)}</span>
                  <button type="button" onClick={() => document.getElementById('prompt_file')?.click()} className="text-blue-600 hover:text-blue-800 text-sm mr-2">Replace</button>
                  <span className="text-gray-400 text-sm mr-2">|</span>
                  <button type="button" onClick={() => downloadConfigFile('prompt')} className="text-green-600 hover:text-green-800 text-sm">Download</button>
                </div>
              ) : null}
              <input id="prompt_file" type="file" accept=".txt" onChange={(e) => handleFileChange(e, 'prompt')} className="w-full border border-gray-300 rounded px-3 py-2" required={isCreating} style={{ display: editingConfig && !promptFile ? 'none' : 'block' }} />
              {promptFile && (<p className="mt-1 text-sm text-green-600">Selected: {promptFile.name}</p>)}
            </div>

            <div>
              <label className="block text-gray-700 mb-2">Schema File(json)</label>
              {editingConfig && !schemaFile ? (
                <div className="flex items-center mb-2">
                  <span className="text-gray-600 mr-2 text-sm" title={formData.schema_path}>Current: {formatFilePath(formData.schema_path)}</span>
                  <button type="button" onClick={() => document.getElementById('schema_file')?.click()} className="text-blue-600 hover:text-blue-800 text-sm mr-2">Replace</button>
                  <span className="text-gray-400 text-sm mr-2">|</span>
                  <button type="button" onClick={() => downloadConfigFile('schema')} className="text-green-600 hover:text-green-800 text-sm">Download</button>
                </div>
              ) : null}
              <input id="schema_file" type="file" accept=".json" onChange={(e) => handleFileChange(e, 'schema')} className="w-full border border-gray-300 rounded px-3 py-2" required={isCreating} style={{ display: editingConfig && !schemaFile ? 'none' : 'block' }} />
              {schemaFile && (<p className="mt-1 text-sm text-green-600">Selected: {schemaFile.name}</p>)}
            </div>

            <div className="flex items-center">
              <input type="checkbox" id="active" name="active" checked={formData.active} onChange={handleInputChange} className="mr-2" />
              <label htmlFor="active" className="text-gray-700">Active</label>
            </div>

            <div className="flex space-x-4">
              <button type="submit" className="bg-blue-600 text-white px-4 py-2 rounded hover:bg-blue-700">{editingConfig ? 'Update' : 'Create'}</button>
              <button type="button" onClick={handleCancel} className="bg-gray-300 text-gray-800 px-4 py-2 rounded hover:bg-gray-400">Cancel</button>
            </div>
          </form>
        </div>
      )}

      {isLoading ? (
        <div className="text-center py-10">Loading configurations...</div>
      ) : configs.length === 0 ? (
        <div className="text-center py-10"><p className="text-gray-500">No configurations found</p></div>
      ) : (
        <div className="bg-white shadow-md rounded-lg overflow-hidden">
          <table className="min-w-full divide-y divide-gray-200">
            <thead className="bg-gray-50">
              <tr>
                <th className="px-6 py-3 text-left text-xs font-medium text-gray-500 uppercase tracking-wider">ID</th>
                <th className="px-6 py-3 text-left text-xs font-medium text-gray-500 uppercase tracking-wider">Company</th>
                <th className="px-6 py-3 text-left text-xs font-medium text-gray-500 uppercase tracking-wider">Document Type</th>
                <th className="px-6 py-3 text-left text-xs font-medium text-gray-500 uppercase tracking-wider">Prompt Path</th>
                <th className="px-6 py-3 text-left text-xs font-medium text-gray-500 uppercase tracking-wider">Schema Path</th>
                <th className="px-6 py-3 text-left text-xs font-medium text-gray-500 uppercase tracking-wider">Status</th>
                <th className="px-6 py-3 text-left text-xs font-medium text-gray-500 uppercase tracking-wider">Actions</th>
              </tr>
            </thead>
            <tbody className="bg-white divide-y divide-gray-200">
              {configs.map(cfg => (
                <tr key={cfg.config_id}>
                  <td className="px-6 py-4 whitespace-nowrap text-sm text-gray-900">{cfg.config_id}</td>
                  <td className="px-6 py-4 whitespace-nowrap text-sm text-gray-900">{getCompanyName(cfg.company_id)}</td>
                  <td className="px-6 py-4 whitespace-nowrap text-sm text-gray-900">{getDocTypeName(cfg.doc_type_id)}</td>
                  <td className="px-6 py-4 text-sm text-gray-500 max-w-xs truncate" title={cfg.prompt_path}>{formatFilePath(cfg.prompt_path)}</td>
                  <td className="px-6 py-4 text-sm text-gray-500 max-w-xs truncate" title={cfg.schema_path}>{formatFilePath(cfg.schema_path)}</td>
                  <td className="px-6 py-4 whitespace-nowrap"><span className={`px-2 inline-flex text-xs leading-5 font-semibold rounded-full ${cfg.active ? 'bg-green-100 text-green-800' : 'bg-red-100 text-red-800'}`}>{cfg.active ? 'Active' : 'Inactive'}</span></td>
                  <td className="px-6 py-4 whitespace-nowrap text-right text-sm font-medium">
                    <button onClick={() => handleEdit(cfg)} className="text-indigo-600 hover:text-indigo-900 mr-4">Edit</button>
                    <button onClick={() => handleDelete(cfg)} className="text-red-600 hover:text-red-900">Delete</button>
                  </td>
                </tr>
              ))}
            </tbody>
          </table>
        </div>
      )}

      <SmartDeleteDialog isOpen={deleteDialog.isOpen} onClose={handleDeleteCancel} onSuccess={handleDeleteSuccess} entity={deleteDialog.entity!} />
    </div>
  );
}
