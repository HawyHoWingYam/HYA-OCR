'use client';

import { useState, useEffect } from 'react';
import { DocumentType } from '@/lib/api';

// --- Types ---

interface OcrSchedule {
    schedule_id: number;
    name: string;
    enabled: boolean;
    company_id: number | null;
    doc_type_id: number | null;
    material_root_path: string;
    history_root_path: string;
    output_root_path: string;
    failed_subfolder_name: string;
    schedule_mode: 'INTERVAL' | 'WINDOWED_INTERVAL';
    start_at: string | null;
    interval_seconds: number;
    period_unit: string | null;
    period_value: number | null;
    runs_per_period: number | null;
    window_start_time: string | null;
    window_end_time: string | null;
    allowed_weekdays: string | null;
    max_files_per_cycle: number | null;
    last_run_at: string | null;
    next_run_at: string | null;
    created_at: string | null;
    updated_at: string | null;
}

interface OcrScheduledFileView {
    id: number;
    schedule_id: number;
    month_str: string;
    onedrive_path: string;
    filename: string;
    status: 'PENDING' | 'PROCESSING' | 'WAITING_FOR_EXCEL' | 'COMPLETED' | 'ERROR';
    error_message: string | null;
    attempt_count: number;
    ocr_json_path: string | null;
    output_excel_path: string | null;
    excel_row_index: number | null;
    created_at: string | null;
    updated_at: string | null;
}

// --- API Helpers ---

const API_BASE_URL = '/api';

async function fetchSchedules(): Promise<OcrSchedule[]> {
    const res = await fetch(`${API_BASE_URL}/ocr-schedules`);
    if (!res.ok) throw new Error('Failed to fetch schedules');
    return res.json();
}

async function createSchedule(payload: any): Promise<OcrSchedule> {
    const res = await fetch(`${API_BASE_URL}/ocr-schedules`, {
        method: 'POST',
        headers: { 'Content-Type': 'application/json' },
        body: JSON.stringify(payload),
    });
    if (!res.ok) throw new Error('Failed to create schedule');
    return res.json();
}

async function updateSchedule(id: number, payload: any): Promise<OcrSchedule> {
    const res = await fetch(`${API_BASE_URL}/ocr-schedules/${id}`, {
        method: 'PUT',
        headers: { 'Content-Type': 'application/json' },
        body: JSON.stringify(payload),
    });
    if (!res.ok) throw new Error('Failed to update schedule');
    return res.json();
}

async function runScheduleNow(id: number): Promise<void> {
    const res = await fetch(`${API_BASE_URL}/ocr-schedules/${id}/run-now`, {
        method: 'POST',
    });
    if (!res.ok) throw new Error('Failed to trigger run now');
}

async function fetchScheduleFiles(id: number, status?: string): Promise<OcrScheduledFileView[]> {
    const params = new URLSearchParams();
    if (status) params.append('status', status);
    const res = await fetch(`${API_BASE_URL}/ocr-schedules/${id}/files?${params}`);
    if (!res.ok) throw new Error('Failed to fetch schedule files');
    return res.json();
}

async function fetchDocumentTypes(): Promise<DocumentType[]> {
    const res = await fetch(`${API_BASE_URL}/document-types`);
    if (!res.ok) throw new Error('Failed to fetch document types');
    return res.json();
}

// --- Components ---

export default function OcrSchedulesPage() {
    // State
    const [schedules, setSchedules] = useState<OcrSchedule[]>([]);
    const [isLoading, setIsLoading] = useState(true);
    const [error, setError] = useState('');

    // Form State
    const [isCreating, setIsCreating] = useState(false);
    const [editingSchedule, setEditingSchedule] = useState<OcrSchedule | null>(null);
    const [docTypes, setDocTypes] = useState<DocumentType[]>([]);

    // File History State
    const [selectedScheduleForFiles, setSelectedScheduleForFiles] = useState<OcrSchedule | null>(null);
    const [scheduleFiles, setScheduleFiles] = useState<OcrScheduledFileView[]>([]);
    const [filesLoading, setFilesLoading] = useState(false);
    const [fileStatusFilter, setFileStatusFilter] = useState('');

    // Initial Data Load
    useEffect(() => {
        loadSchedules();
        loadDocTypes();
    }, []);

    const loadSchedules = async () => {
        try {
            setIsLoading(true);
            const data = await fetchSchedules();
            setSchedules(data);
            setError('');
        } catch (err) {
            console.error(err);
            setError('Failed to load schedules');
        } finally {
            setIsLoading(false);
        }
    };

    const loadDocTypes = async () => {
        try {
            const data = await fetchDocumentTypes();
            setDocTypes(data);
        } catch (err) {
            console.error('Failed to load doc types', err);
        }
    };

    // Actions
    const handleRunNow = async (id: number) => {
        try {
            await runScheduleNow(id);
            alert('Schedule triggered successfully. It will run shortly.');
            loadSchedules(); // Refresh to see updated next_run_at
        } catch (err) {
            console.error(err);
            alert('Failed to trigger schedule');
        }
    };

    const handleToggleEnabled = async (schedule: OcrSchedule) => {
        try {
            await updateSchedule(schedule.schedule_id, { enabled: !schedule.enabled });
            loadSchedules();
        } catch (err) {
            console.error(err);
            alert('Failed to toggle status');
        }
    };

    const handleViewFiles = async (schedule: OcrSchedule) => {
        setSelectedScheduleForFiles(schedule);
        setFileStatusFilter('');
        loadFiles(schedule.schedule_id);
    };

    const loadFiles = async (scheduleId: number, status?: string) => {
        try {
            setFilesLoading(true);
            const data = await fetchScheduleFiles(scheduleId, status);
            setScheduleFiles(data);
        } catch (err) {
            console.error(err);
            alert('Failed to load files');
        } finally {
            setFilesLoading(false);
        }
    };

    // Form Handling
    const handleCreateClick = () => {
        setEditingSchedule(null);
        setIsCreating(true);
        setSelectedScheduleForFiles(null); // Close file view if open
    };

    const handleEditClick = (schedule: OcrSchedule) => {
        setEditingSchedule(schedule);
        setIsCreating(false);
        setSelectedScheduleForFiles(null); // Close file view if open
    };

    const handleFormSuccess = () => {
        setIsCreating(false);
        setEditingSchedule(null);
        loadSchedules();
    };

    return (
        <div className="container mx-auto px-4 py-8">
            {/* Header */}
            <div className="flex justify-between items-center mb-6">
                <h1 className="text-2xl font-bold">OCR Schedules</h1>
                {!isCreating && !editingSchedule && (
                    <button
                        onClick={handleCreateClick}
                        className="bg-green-600 text-white px-4 py-2 rounded hover:bg-green-700 transition-colors"
                    >
                        Create New Schedule
                    </button>
                )}
            </div>

            {/* Error Message */}
            {error && (
                <div className="bg-red-100 border border-red-400 text-red-700 px-4 py-3 rounded mb-4">
                    {error}
                </div>
            )}

            {/* Create/Edit Form */}
            {(isCreating || editingSchedule) && (
                <ScheduleForm
                    isCreating={isCreating}
                    initialData={editingSchedule}
                    docTypes={docTypes}
                    onCancel={() => {
                        setIsCreating(false);
                        setEditingSchedule(null);
                    }}
                    onSuccess={handleFormSuccess}
                />
            )}

            {/* Schedules List */}
            {!isCreating && !editingSchedule && (
                <div className="bg-white shadow-md rounded-lg overflow-hidden mb-8">
                    {isLoading ? (
                        <div className="text-center py-10">Loading schedules...</div>
                    ) : schedules.length === 0 ? (
                        <div className="text-center py-10 text-gray-500">No schedules found. Create one to get started.</div>
                    ) : (
                        <div className="overflow-x-auto">
                            <table className="min-w-full divide-y divide-gray-200">
                                <thead className="bg-gray-50">
                                    <tr>
                                        <th className="px-6 py-3 text-left text-xs font-medium text-gray-500 uppercase tracking-wider">Name</th>
                                        <th className="px-6 py-3 text-left text-xs font-medium text-gray-500 uppercase tracking-wider">Doc Type</th>
                                        <th className="px-6 py-3 text-left text-xs font-medium text-gray-500 uppercase tracking-wider">Status</th>
                                        <th className="px-6 py-3 text-left text-xs font-medium text-gray-500 uppercase tracking-wider">Schedule</th>
                                        <th className="px-6 py-3 text-left text-xs font-medium text-gray-500 uppercase tracking-wider">Next Run</th>
                                        <th className="px-6 py-3 text-left text-xs font-medium text-gray-500 uppercase tracking-wider">Last Run</th>
                                        <th className="px-6 py-3 text-right text-xs font-medium text-gray-500 uppercase tracking-wider">Actions</th>
                                    </tr>
                                </thead>
                                <tbody className="bg-white divide-y divide-gray-200">
                                    {schedules.map((schedule) => (
                                        <tr key={schedule.schedule_id} className="hover:bg-gray-50">
                                            <td className="px-6 py-4 whitespace-nowrap text-sm font-medium text-gray-900">
                                                {schedule.name}
                                            </td>
                                            <td className="px-6 py-4 whitespace-nowrap text-sm text-gray-500">
                                                {docTypes.find(d => d.doc_type_id === schedule.doc_type_id)?.type_name || schedule.doc_type_id || '-'}
                                            </td>
                                            <td className="px-6 py-4 whitespace-nowrap">
                                                <span className={`px-2 inline-flex text-xs leading-5 font-semibold rounded-full ${schedule.enabled ? 'bg-green-100 text-green-800' : 'bg-gray-100 text-gray-800'
                                                    }`}>
                                                    {schedule.enabled ? 'Enabled' : 'Disabled'}
                                                </span>
                                            </td>
                                            <td className="px-6 py-4 whitespace-nowrap text-sm text-gray-500">
                                                {formatScheduleSummary(schedule)}
                                            </td>
                                            <td className="px-6 py-4 whitespace-nowrap text-sm text-gray-500">
                                                {schedule.next_run_at ? new Date(schedule.next_run_at).toLocaleString() : '-'}
                                            </td>
                                            <td className="px-6 py-4 whitespace-nowrap text-sm text-gray-500">
                                                {schedule.last_run_at ? new Date(schedule.last_run_at).toLocaleString() : '-'}
                                            </td>
                                            <td className="px-6 py-4 whitespace-nowrap text-right text-sm font-medium space-x-3">
                                                <button
                                                    onClick={() => handleEditClick(schedule)}
                                                    className="text-indigo-600 hover:text-indigo-900"
                                                >
                                                    Edit
                                                </button>
                                                <button
                                                    onClick={() => handleToggleEnabled(schedule)}
                                                    className={`${schedule.enabled ? 'text-amber-600 hover:text-amber-900' : 'text-green-600 hover:text-green-900'}`}
                                                >
                                                    {schedule.enabled ? 'Disable' : 'Enable'}
                                                </button>
                                                <button
                                                    onClick={() => handleRunNow(schedule.schedule_id)}
                                                    className="text-blue-600 hover:text-blue-900"
                                                >
                                                    Run Now
                                                </button>
                                                <button
                                                    onClick={() => handleViewFiles(schedule)}
                                                    className="text-gray-600 hover:text-gray-900"
                                                >
                                                    Files
                                                </button>
                                            </td>
                                        </tr>
                                    ))}
                                </tbody>
                            </table>
                        </div>
                    )}
                </div>
            )}

            {/* File History Panel */}
            {selectedScheduleForFiles && (
                <div className="bg-white shadow-md rounded-lg p-6 border-t-4 border-blue-500">
                    <div className="flex justify-between items-center mb-4">
                        <h2 className="text-xl font-bold">
                            Files for: {selectedScheduleForFiles.name}
                        </h2>
                        <div className="flex items-center gap-2">
                            <select
                                value={fileStatusFilter}
                                onChange={(e) => {
                                    setFileStatusFilter(e.target.value);
                                    loadFiles(selectedScheduleForFiles.schedule_id, e.target.value);
                                }}
                                className="border border-gray-300 rounded px-2 py-1 text-sm"
                            >
                                <option value="">All Status</option>
                                <option value="PENDING">Pending</option>
                                <option value="PROCESSING">Processing</option>
                                <option value="WAITING_FOR_EXCEL">Waiting for Excel</option>
                                <option value="COMPLETED">Completed</option>
                                <option value="ERROR">Error</option>
                            </select>
                            <button
                                onClick={() => setSelectedScheduleForFiles(null)}
                                className="text-gray-500 hover:text-gray-700"
                            >
                                ✕ Close
                            </button>
                        </div>
                    </div>

                    {filesLoading ? (
                        <div className="text-center py-8">Loading files...</div>
                    ) : scheduleFiles.length === 0 ? (
                        <div className="text-center py-8 text-gray-500">No files found for this schedule.</div>
                    ) : (
                        <div className="overflow-x-auto">
                            <table className="min-w-full divide-y divide-gray-200">
                                <thead className="bg-gray-50">
                                    <tr>
                                        <th className="px-4 py-2 text-left text-xs font-medium text-gray-500 uppercase">Month</th>
                                        <th className="px-4 py-2 text-left text-xs font-medium text-gray-500 uppercase">Filename</th>
                                        <th className="px-4 py-2 text-left text-xs font-medium text-gray-500 uppercase">Status</th>
                                        <th className="px-4 py-2 text-left text-xs font-medium text-gray-500 uppercase">Attempts</th>
                                        <th className="px-4 py-2 text-left text-xs font-medium text-gray-500 uppercase">Error</th>
                                        <th className="px-4 py-2 text-left text-xs font-medium text-gray-500 uppercase">Updated</th>
                                    </tr>
                                </thead>
                                <tbody className="bg-white divide-y divide-gray-200">
                                    {scheduleFiles.map((file) => (
                                        <tr key={file.id} className="hover:bg-gray-50">
                                            <td className="px-4 py-2 whitespace-nowrap text-sm text-gray-900">{file.month_str}</td>
                                            <td className="px-4 py-2 whitespace-nowrap text-sm text-gray-900" title={file.onedrive_path}>
                                                {file.filename}
                                            </td>
                                            <td className="px-4 py-2 whitespace-nowrap">
                                                <StatusBadge status={file.status} />
                                            </td>
                                            <td className="px-4 py-2 whitespace-nowrap text-sm text-gray-500">{file.attempt_count}</td>
                                            <td className="px-4 py-2 whitespace-nowrap text-sm text-red-600 max-w-xs truncate" title={file.error_message || ''}>
                                                {file.error_message}
                                            </td>
                                            <td className="px-4 py-2 whitespace-nowrap text-sm text-gray-500">
                                                {file.updated_at ? new Date(file.updated_at).toLocaleString() : '-'}
                                            </td>
                                        </tr>
                                    ))}
                                </tbody>
                            </table>
                        </div>
                    )}
                </div>
            )}
        </div>
    );
}

// --- Sub-Components ---

function ScheduleForm({
    isCreating,
    initialData,
    docTypes,
    onCancel,
    onSuccess
}: {
    isCreating: boolean;
    initialData: OcrSchedule | null;
    docTypes: DocumentType[];
    onCancel: () => void;
    onSuccess: () => void;
}) {
    const [formData, setFormData] = useState<any>({
        name: '',
        doc_type_id: '',
        enabled: true,
        material_root_path: '',
        history_root_path: '',
        output_root_path: '',
        failed_subfolder_name: '_Failed',
        schedule_mode: 'INTERVAL',
        start_at: '', // datetime-local string
        interval_seconds: 3600,
        period_unit: 'hour',
        period_value: 1,
        runs_per_period: 1,
        window_start_time: '09:00',
        window_end_time: '18:00',
        allowed_weekdays: '0,1,2,3,4', // Mon-Fri
        max_files_per_cycle: 10,
        ...initialData,
        // Handle nulls for controlled inputs
        doc_type_id: initialData?.doc_type_id || '',
        start_at: initialData?.start_at ? new Date(initialData.start_at).toISOString().slice(0, 16) : '',
    });

    const [error, setError] = useState('');
    const [saving, setSaving] = useState(false);

    const handleChange = (e: React.ChangeEvent<HTMLInputElement | HTMLSelectElement>) => {
        const { name, value, type } = e.target;
        setFormData((prev: any) => ({
            ...prev,
            [name]: type === 'checkbox' ? (e.target as HTMLInputElement).checked : value
        }));
    };

    const handleSubmit = async (e: React.FormEvent) => {
        e.preventDefault();
        setError('');
        setSaving(true);

        try {
            // Prepare payload
            const payload = { ...formData };

            // Convert number fields
            if (payload.doc_type_id) payload.doc_type_id = Number(payload.doc_type_id);
            if (payload.max_files_per_cycle) payload.max_files_per_cycle = Number(payload.max_files_per_cycle);

            // Calculate interval_seconds based on period inputs if needed, 
            // but here we might just rely on user input or simple logic.
            // For simplicity, let's assume the backend handles complex logic or we just send what we have.
            // Actually, let's do a simple conversion for display consistency if the user changed period_unit/value
            if (payload.period_unit && payload.period_value) {
                const multipliers: Record<string, number> = {
                    second: 1,
                    minute: 60,
                    hour: 3600,
                    day: 86400
                };
                payload.interval_seconds = Number(payload.period_value) * (multipliers[payload.period_unit] || 1);
            }

            // Format start_at to ISO if present
            if (payload.start_at) {
                payload.start_at = new Date(payload.start_at).toISOString();
            } else {
                payload.start_at = null;
            }

            if (isCreating) {
                await createSchedule(payload);
            } else if (initialData) {
                await updateSchedule(initialData.schedule_id, payload);
            }
            onSuccess();
        } catch (err) {
            console.error(err);
            setError('Failed to save schedule');
        } finally {
            setSaving(false);
        }
    };

    return (
        <div className="bg-white shadow-md rounded-lg p-6 mb-6">
            <h2 className="text-lg font-medium mb-4">
                {isCreating ? 'Create New Schedule' : 'Edit Schedule'}
            </h2>

            {error && (
                <div className="bg-red-100 text-red-700 px-4 py-2 rounded mb-4 text-sm">
                    {error}
                </div>
            )}

            <form onSubmit={handleSubmit} className="space-y-4">
                {/* Basic Info */}
                <div className="grid grid-cols-1 md:grid-cols-2 gap-4">
                    <div>
                        <label className="block text-sm font-medium text-gray-700">Name</label>
                        <input
                            type="text"
                            name="name"
                            value={formData.name}
                            onChange={handleChange}
                            required
                            className="mt-1 block w-full border border-gray-300 rounded-md shadow-sm py-2 px-3 focus:ring-indigo-500 focus:border-indigo-500 sm:text-sm"
                        />
                    </div>
                    <div>
                        <label className="block text-sm font-medium text-gray-700">Document Type</label>
                        <select
                            name="doc_type_id"
                            value={formData.doc_type_id}
                            onChange={handleChange}
                            className="mt-1 block w-full border border-gray-300 rounded-md shadow-sm py-2 px-3 focus:ring-indigo-500 focus:border-indigo-500 sm:text-sm"
                        >
                            <option value="">Select Doc Type...</option>
                            {docTypes.map(dt => (
                                <option key={dt.doc_type_id} value={dt.doc_type_id}>
                                    {dt.type_name}
                                </option>
                            ))}
                        </select>
                    </div>
                </div>

                {/* Paths */}
                <div className="space-y-2">
                    <h3 className="text-sm font-medium text-gray-900 pt-2">OneDrive Paths</h3>
                    <div>
                        <label className="block text-xs text-gray-500">Material Root Path</label>
                        <input
                            type="text"
                            name="material_root_path"
                            value={formData.material_root_path}
                            onChange={handleChange}
                            required
                            className="mt-1 block w-full border border-gray-300 rounded-md shadow-sm py-2 px-3 sm:text-sm"
                        />
                    </div>
                    <div className="grid grid-cols-1 md:grid-cols-2 gap-4">
                        <div>
                            <label className="block text-xs text-gray-500">History Root Path</label>
                            <input
                                type="text"
                                name="history_root_path"
                                value={formData.history_root_path}
                                onChange={handleChange}
                                required
                                className="mt-1 block w-full border border-gray-300 rounded-md shadow-sm py-2 px-3 sm:text-sm"
                            />
                        </div>
                        <div>
                            <label className="block text-xs text-gray-500">Output Root Path</label>
                            <input
                                type="text"
                                name="output_root_path"
                                value={formData.output_root_path}
                                onChange={handleChange}
                                required
                                className="mt-1 block w-full border border-gray-300 rounded-md shadow-sm py-2 px-3 sm:text-sm"
                            />
                        </div>
                    </div>
                </div>

                {/* Scheduling */}
                <div className="space-y-2 border-t pt-4 mt-4">
                    <h3 className="text-sm font-medium text-gray-900">Scheduling Configuration</h3>

                    <div className="grid grid-cols-1 md:grid-cols-2 gap-4">
                        <div>
                            <label className="block text-sm font-medium text-gray-700">Mode</label>
                            <select
                                name="schedule_mode"
                                value={formData.schedule_mode}
                                onChange={handleChange}
                                className="mt-1 block w-full border border-gray-300 rounded-md shadow-sm py-2 px-3 sm:text-sm"
                            >
                                <option value="INTERVAL">Simple Interval</option>
                                <option value="WINDOWED_INTERVAL">Windowed Interval (e.g. Business Hours)</option>
                            </select>
                        </div>
                        <div>
                            <label className="block text-sm font-medium text-gray-700">Start At (First Run)</label>
                            <input
                                type="datetime-local"
                                name="start_at"
                                value={formData.start_at}
                                onChange={handleChange}
                                className="mt-1 block w-full border border-gray-300 rounded-md shadow-sm py-2 px-3 sm:text-sm"
                            />
                        </div>
                    </div>

                    <div className="grid grid-cols-3 gap-4">
                        <div>
                            <label className="block text-xs text-gray-500">Run Every (Value)</label>
                            <input
                                type="number"
                                name="period_value"
                                value={formData.period_value}
                                onChange={handleChange}
                                min="1"
                                className="mt-1 block w-full border border-gray-300 rounded-md shadow-sm py-2 px-3 sm:text-sm"
                            />
                        </div>
                        <div>
                            <label className="block text-xs text-gray-500">Unit</label>
                            <select
                                name="period_unit"
                                value={formData.period_unit}
                                onChange={handleChange}
                                className="mt-1 block w-full border border-gray-300 rounded-md shadow-sm py-2 px-3 sm:text-sm"
                            >
                                <option value="second">Seconds</option>
                                <option value="minute">Minutes</option>
                                <option value="hour">Hours</option>
                                <option value="day">Days</option>
                            </select>
                        </div>
                        <div>
                            <label className="block text-xs text-gray-500">Max Files / Cycle</label>
                            <input
                                type="number"
                                name="max_files_per_cycle"
                                value={formData.max_files_per_cycle}
                                onChange={handleChange}
                                className="mt-1 block w-full border border-gray-300 rounded-md shadow-sm py-2 px-3 sm:text-sm"
                            />
                        </div>
                    </div>

                    {formData.schedule_mode === 'WINDOWED_INTERVAL' && (
                        <div className="bg-gray-50 p-3 rounded-md space-y-3">
                            <h4 className="text-xs font-bold text-gray-700 uppercase">Window Settings</h4>
                            <div className="grid grid-cols-2 gap-4">
                                <div>
                                    <label className="block text-xs text-gray-500">Window Start (HH:MM)</label>
                                    <input
                                        type="time"
                                        name="window_start_time"
                                        value={formData.window_start_time}
                                        onChange={handleChange}
                                        className="mt-1 block w-full border border-gray-300 rounded-md shadow-sm py-2 px-3 sm:text-sm"
                                    />
                                </div>
                                <div>
                                    <label className="block text-xs text-gray-500">Window End (HH:MM)</label>
                                    <input
                                        type="time"
                                        name="window_end_time"
                                        value={formData.window_end_time}
                                        onChange={handleChange}
                                        className="mt-1 block w-full border border-gray-300 rounded-md shadow-sm py-2 px-3 sm:text-sm"
                                    />
                                </div>
                            </div>
                            <div>
                                <label className="block text-xs text-gray-500">Allowed Weekdays (0=Mon, 6=Sun)</label>
                                <input
                                    type="text"
                                    name="allowed_weekdays"
                                    value={formData.allowed_weekdays}
                                    onChange={handleChange}
                                    placeholder="e.g. 0,1,2,3,4"
                                    className="mt-1 block w-full border border-gray-300 rounded-md shadow-sm py-2 px-3 sm:text-sm"
                                />
                                <p className="text-xs text-gray-400 mt-1">Comma separated: 0=Mon, 1=Tue, ..., 6=Sun</p>
                            </div>
                        </div>
                    )}
                </div>

                <div className="flex items-center pt-4">
                    <input
                        type="checkbox"
                        name="enabled"
                        checked={formData.enabled}
                        onChange={handleChange}
                        className="h-4 w-4 text-indigo-600 focus:ring-indigo-500 border-gray-300 rounded"
                    />
                    <label className="ml-2 block text-sm text-gray-900">Enable Schedule</label>
                </div>

                <div className="flex justify-end space-x-3 pt-4">
                    <button
                        type="button"
                        onClick={onCancel}
                        className="bg-white py-2 px-4 border border-gray-300 rounded-md shadow-sm text-sm font-medium text-gray-700 hover:bg-gray-50 focus:outline-none"
                    >
                        Cancel
                    </button>
                    <button
                        type="submit"
                        disabled={saving}
                        className="inline-flex justify-center py-2 px-4 border border-transparent shadow-sm text-sm font-medium rounded-md text-white bg-indigo-600 hover:bg-indigo-700 focus:outline-none disabled:opacity-50"
                    >
                        {saving ? 'Saving...' : (isCreating ? 'Create Schedule' : 'Update Schedule')}
                    </button>
                </div>
            </form>
        </div>
    );
}

function formatScheduleSummary(s: OcrSchedule) {
    let summary = `Every ${s.period_value} ${s.period_unit}(s)`;
    if (s.schedule_mode === 'WINDOWED_INTERVAL') {
        summary += ` within ${s.window_start_time}-${s.window_end_time}`;
        if (s.allowed_weekdays) {
            summary += ` (Days: ${s.allowed_weekdays})`;
        }
    }
    return summary;
}

function StatusBadge({ status }: { status: string }) {
    let color = 'bg-gray-100 text-gray-800';
    switch (status) {
        case 'COMPLETED': color = 'bg-green-100 text-green-800'; break;
        case 'PROCESSING': color = 'bg-blue-100 text-blue-800'; break;
        case 'WAITING_FOR_EXCEL': color = 'bg-yellow-100 text-yellow-800'; break;
        case 'ERROR': color = 'bg-red-100 text-red-800'; break;
    }
    return (
        <span className={`px-2 inline-flex text-xs leading-5 font-semibold rounded-full ${color}`}>
            {status}
        </span>
    );
}
