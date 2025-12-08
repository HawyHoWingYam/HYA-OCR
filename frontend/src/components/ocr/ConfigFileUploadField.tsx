'use client';

import { useCallback, useEffect, useRef, useState } from 'react';
import { ocrConfigsApi, uploadFile } from '@/lib/api';

export type ConfigFileType = 'prompt' | 'schema' | 'output_template';

interface ConfigFileUploadFieldProps {
  configId: number;
  fileType: ConfigFileType;
  currentPath?: string;
  originalFilename?: string;
  onUploadComplete: (path: string) => void;
}

const FILE_FIELD_MAP: Record<ConfigFileType, 'prompt_path' | 'schema_path' | 'output_template_path'> = {
  prompt: 'prompt_path',
  schema: 'schema_path',
  output_template: 'output_template_path',
};

const ACCEPT_MAP: Record<ConfigFileType, string> = {
  prompt: '.md,.txt,.json,.yaml,.yml',
  schema: '.json',
  output_template: '.json,.yaml,.yml',
};

const LABEL_MAP: Record<ConfigFileType, string> = {
  prompt: 'Prompt',
  schema: 'Schema',
  output_template: 'Output Template',
};

export default function ConfigFileUploadField({
  configId,
  fileType,
  currentPath,
  originalFilename,
  onUploadComplete,
}: ConfigFileUploadFieldProps) {
  const fileInputRef = useRef<HTMLInputElement | null>(null);
  const [isDragging, setIsDragging] = useState(false);
  const [isUploading, setIsUploading] = useState(false);
  const [uploadProgress, setUploadProgress] = useState(0);
  const [error, setError] = useState<string | null>(null);
  const [statusMessage, setStatusMessage] = useState<string | null>(null);

  const label = LABEL_MAP[fileType];
  const accept = ACCEPT_MAP[fileType];

  const resetProgress = useCallback(() => {
    setTimeout(() => setUploadProgress(0), 600);
  }, []);

  useEffect(() => {
    if (!isUploading && uploadProgress === 100) {
      resetProgress();
    }
  }, [isUploading, uploadProgress, resetProgress]);

  const handleFileChoose = useCallback(() => {
    fileInputRef.current?.click();
  }, []);

  const buildTargetPath = useCallback((fileName: string) => {
    const normalized = fileName.replace(/[^A-Za-z0-9._-]+/g, '_');
    const timestamp = new Date().toISOString().replace(/[:.]/g, '-');
    return `company-configs/${configId}/${fileType}/${timestamp}-${normalized}`;
  }, [configId, fileType]);

  const pushUpdate = useCallback(async (path: string | null) => {
    const fieldName = FILE_FIELD_MAP[fileType];
    await ocrConfigsApi.update(configId, { [fieldName]: path } as Record<string, string | null>);
  }, [configId, fileType]);

  const handleUpload = useCallback(async (fileList: FileList | null) => {
    if (!fileList || fileList.length === 0) {
      return;
    }

    const file = fileList[0];
    setError(null);
    setStatusMessage(null);
    setIsUploading(true);
    setUploadProgress(15);

    try {
      const targetPath = buildTargetPath(file.name);
      const storedPath = await uploadFile(file, targetPath);
      setUploadProgress(85);
      await pushUpdate(storedPath);
      setUploadProgress(100);
      onUploadComplete(storedPath);
      setStatusMessage(`${file.name} uploaded successfully`);
    } catch (err) {
      console.error(err);
      setError(err instanceof Error ? err.message : 'File upload failed');
      setStatusMessage(null);
    } finally {
      setIsUploading(false);
      if (fileInputRef.current) {
        fileInputRef.current.value = '';
      }
    }
  }, [buildTargetPath, onUploadComplete, pushUpdate]);

  const onDrop = useCallback((event: React.DragEvent<HTMLLabelElement>) => {
    event.preventDefault();
    if (event.dataTransfer?.files) {
      handleUpload(event.dataTransfer.files);
    }
    setIsDragging(false);
  }, [handleUpload]);

  const onDragOver = useCallback((event: React.DragEvent<HTMLLabelElement>) => {
    event.preventDefault();
    if (!isDragging) {
      setIsDragging(true);
    }
  }, [isDragging]);

  const handleDragLeave = useCallback((event: React.DragEvent<HTMLLabelElement>) => {
    event.preventDefault();
    setIsDragging(false);
  }, []);

  const handleDownload = useCallback(() => {
    if (!currentPath) return;
    const fallbackUrl = `/api/company-doc-type-configs/${configId}/config-files/download?kind=${fileType}`;
    const downloadUrl = currentPath.startsWith('http') ? currentPath : fallbackUrl;
    window.open(downloadUrl, '_blank', 'noopener,noreferrer');
  }, [configId, currentPath, fileType]);

  const handleDelete = useCallback(async () => {
    if (!currentPath) return;
    const confirmed = window.confirm(`Remove the ${label} file from this config?`);
    if (!confirmed) return;
    try {
      await pushUpdate(null);
      onUploadComplete('');
      setStatusMessage(`${label} reference cleared`);
    } catch (err) {
      console.error(err);
      setError(err instanceof Error ? err.message : 'Failed to clear file reference');
    }
  }, [currentPath, label, onUploadComplete, pushUpdate]);

  return (
    <div className="border border-gray-200 rounded-lg p-4 bg-white shadow-sm">
      <div className="flex flex-col sm:flex-row sm:items-center sm:justify-between gap-2 mb-3">
        <div>
          <p className="text-sm font-semibold text-gray-900">{label} File</p>
          <p className="text-xs text-gray-500">Upload or replace the {label.toLowerCase()} definition</p>
        </div>
        <button
          type="button"
          onClick={handleFileChoose}
          className="px-3 py-1.5 text-sm font-medium text-white bg-blue-600 hover:bg-blue-700 rounded"
          disabled={isUploading}
        >
          {isUploading ? 'Uploading…' : 'Choose File'}
        </button>
      </div>

      <input
        ref={fileInputRef}
        type="file"
        className="hidden"
        accept={accept}
        onChange={(event) => handleUpload(event.target.files)}
      />

      <label
        className={`flex flex-col items-center justify-center text-center border-2 border-dashed rounded-md px-4 py-6 cursor-pointer transition-colors ${
          isDragging ? 'border-blue-500 bg-blue-50' : 'border-gray-300 hover:border-blue-400'
        } ${isUploading ? 'opacity-60 pointer-events-none' : ''}`}
        onDragOver={onDragOver}
        onDrop={onDrop}
        onDragLeave={handleDragLeave}
      >
        <span className="text-sm text-gray-700">
          Drag & drop a file here, or <span className="text-blue-600">browse</span>
        </span>
        <span className="text-xs text-gray-500 mt-1">Supported: {accept || 'Any file'}</span>
      </label>

      {uploadProgress > 0 && (
        <div className="mt-4">
          <div className="h-2 bg-gray-100 rounded-full overflow-hidden">
            <div
              className="h-2 bg-blue-600 transition-all"
              style={{ width: `${uploadProgress}%` }}
            />
          </div>
          {statusMessage && !error && (
            <p className="text-xs text-green-600 mt-1">{statusMessage}</p>
          )}
        </div>
      )}

      {currentPath && (
        <div className="mt-4 flex flex-col sm:flex-row sm:items-center sm:justify-between gap-3 bg-gray-50 border border-gray-200 rounded-md p-3">
          <div>
            <p className="text-xs font-semibold text-gray-600 uppercase tracking-wide">Current File</p>
            <p className="text-sm font-medium text-gray-900">
              {originalFilename || currentPath.split('/').pop() || 'Untitled'}
            </p>
            <p className="text-xs text-gray-500 break-all">{currentPath}</p>
          </div>
          <div className="flex gap-2">
            <button
              type="button"
              onClick={handleDownload}
              className="px-3 py-1.5 text-sm font-medium text-gray-700 border border-gray-300 rounded hover:bg-white"
            >
              Download
            </button>
            <button
              type="button"
              onClick={handleDelete}
              className="px-3 py-1.5 text-sm font-medium text-white bg-red-600 hover:bg-red-700 rounded"
            >
              Delete
            </button>
          </div>
        </div>
      )}

      {error && <p className="text-xs text-red-600 mt-2">{error}</p>}
    </div>
  );
}
