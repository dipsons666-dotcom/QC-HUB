import { useEffect, useMemo, useState } from 'react';

const API_BASE = import.meta.env.VITE_API_BASE_URL || 'http://127.0.0.1:8000';

const workflowSteps = [
  { title: 'Import', description: 'Pull SurveyCTO submissions into the QC Hub raw ingestion layer.' },
  { title: 'Process', description: 'Normalize and stage imported records for downstream review.' },
  { title: 'Transform', description: 'Map incoming payloads into the internal canonical QC model.' },
  { title: 'Review', description: 'Route issues through the queue for action and resolution.' },
];

function App() {
  const [issues, setIssues] = useState([]);
  const [selectedIssue, setSelectedIssue] = useState(null);
  const [rawImports, setRawImports] = useState([]);
  const [rawSubmissionCount, setRawSubmissionCount] = useState(0);
  const [page, setPage] = useState('home');
  const [syncStatus, setSyncStatus] = useState('');
  const [dashboard, setDashboard] = useState(null);
  const [staffMembers, setStaffMembers] = useState([]);
  const [rawDataTable, setRawDataTable] = useState({ columns: [], rows: [] });
  const [rawDataOffset, setRawDataOffset] = useState(0);
  const [rawDataPageSize, setRawDataPageSize] = useState(50);
  const [rawDataSearch, setRawDataSearch] = useState('');
  const [rawDataHasMore, setRawDataHasMore] = useState(false);
  const [rawDataLoading, setRawDataLoading] = useState(false);
  const [rawDataTotalCount, setRawDataTotalCount] = useState(0);
  const [selectedRawRecord, setSelectedRawRecord] = useState(null);
  const [visibleColumns, setVisibleColumns] = useState([]);
  const [showColumnPicker, setShowColumnPicker] = useState(false);
  const [columnFilters, setColumnFilters] = useState({});
  const [newStaff, setNewStaff] = useState({ username: '', email: '', role: 'reviewer' });
  const [adminError, setAdminError] = useState('');

  const loadHomeData = () => {
    fetch(`${API_BASE}/api/qc/review-queue`)
      .then((response) => response.json())
      .then((data) => {
        const loadedIssues = data.issues || [];
        setIssues(loadedIssues);
        setSelectedIssue(loadedIssues[0] || null);
      })
      .catch(() => {
        setIssues([]);
        setSelectedIssue(null);
      });

    fetch(`${API_BASE}/api/import/survey-platform/raw`)
      .then((response) => response.json())
      .then((data) => {
        const items = data.items || [];
        setRawImports(items);
        setRawSubmissionCount(items.length);
      })
      .catch(() => {
        setRawImports([]);
        setRawSubmissionCount(0);
      });
  };

  const loadRawDataPage = (offset = 0, append = false, search = rawDataSearch, pageSize = rawDataPageSize) => {
    setRawDataLoading(true);
    const query = new URLSearchParams({ limit: String(pageSize), offset: String(offset) });
    if (search) query.set('search', search);

    fetch(`${API_BASE}/api/admin/raw-data-table?${query.toString()}`)
      .then((response) => response.json())
      .then((data) => {
        const nextRows = data.rows || [];
        setRawDataTable((current) => ({
          columns: data.columns || current.columns,
          rows: append ? [...current.rows, ...nextRows] : nextRows,
        }));
        setRawDataOffset(offset);
        setRawDataHasMore(Boolean(data.has_more));
        setRawDataTotalCount(Number(data.count || 0));
      })
      .catch(() => {
        setRawDataTable({ columns: [], rows: [] });
        setRawDataOffset(0);
        setRawDataHasMore(false);
        setRawDataTotalCount(0);
      })
      .finally(() => setRawDataLoading(false));
  };

  const loadAdminData = () => {
    setAdminError('');
    fetch(`${API_BASE}/api/admin/dashboard`)
      .then((response) => response.json())
      .then((data) => setDashboard(data))
      .catch(() => setDashboard(null));

    fetch(`${API_BASE}/api/admin/staff`)
      .then((response) => response.json())
      .then((data) => setStaffMembers(data || []))
      .catch(() => setStaffMembers([]));

    loadRawDataPage(0, false, '', rawDataPageSize);
  };

  useEffect(() => {
    loadHomeData();
  }, []);

  useEffect(() => {
    if (page === 'admin') {
      loadAdminData();
    }
  }, [page]);

  useEffect(() => {
    // initialize visible columns whenever the table metadata changes
    const cols = rawDataTable.columns || [];
    if (cols.length > 0 && visibleColumns.length === 0) {
      setVisibleColumns(cols.slice(0, 10).map((c) => c.name));
    }
  }, [rawDataTable.columns]);

  const handleSync = () => {
    setSyncStatus('Syncing...');
    fetch(`${API_BASE}/api/import/survey-platform/sync`, { method: 'POST' })
      .then((response) => {
        if (!response.ok) {
          return response.text().then((body) => {
            throw new Error(`Sync failed (${response.status}): ${body || response.statusText}`);
          });
        }
        return response.json();
      })
      .then((data) => {
        const fetched = data.fetched ?? 0;
        const stored = data.stored ?? 0;
        const updated = data.updated ?? 0;
        setSyncStatus(`Sync complete: fetched ${fetched}, stored ${stored}, updated ${updated}. Raw submissions: ${rawSubmissionCount}`);
        loadHomeData();
        if (page === 'admin') {
          loadAdminData();
        }
      })
      .catch((error) => {
        setSyncStatus(`Sync failed: ${error.message}`);
      });
  };

  const handleCreateStaff = (event) => {
    event.preventDefault();
    setAdminError('');
    fetch(`${API_BASE}/api/admin/staff`, {
      method: 'POST',
      headers: { 'Content-Type': 'application/json' },
      body: JSON.stringify(newStaff),
    })
      .then((response) => {
        if (!response.ok) throw new Error('Create staff failed');
        return response.json();
      })
      .then((created) => {
        setStaffMembers((current) => [...current, created]);
        setNewStaff({ username: '', email: '', role: 'reviewer' });
      })
      .catch(() => {
        setAdminError('Unable to add staff. Check the email and try again.');
      });
  };

  const stats = useMemo(() => {
    const highSeverity = issues.filter((issue) => issue.severity?.toLowerCase() === 'high').length;
    const mediumSeverity = issues.filter((issue) => issue.severity?.toLowerCase() === 'medium').length;
    const pending = issues.filter((issue) => issue.issue_status === 'pending_review').length;

    return {
      total: issues.length,
      highSeverity,
      mediumSeverity,
      pending,
    };
  }, [issues]);

  const formatDate = (value) => {
    if (!value) return '—';
    const parsed = new Date(value);
    return Number.isNaN(parsed.getTime()) ? value : parsed.toLocaleString();
  };

  const filteredRawRows = useMemo(() => {
    const filters = Object.entries(columnFilters).filter(([, v]) => String(v || '').trim() !== '');
    if (filters.length === 0) return rawDataTable.rows || [];
    return (rawDataTable.rows || []).filter((row) => {
      return filters.every(([col, term]) => {
        const cell = row[col];
        if (cell == null) return false;
        return String(cell).toLowerCase().includes(String(term).toLowerCase());
      });
    });
  }, [rawDataTable.rows, columnFilters]);

  const exportCsv = () => {
    const cols = visibleColumns.length > 0 ? visibleColumns : (rawDataTable.columns || []).map((c) => c.name);
    const rows = filteredRawRows;
    const escape = (v) => {
      if (v == null) return '';
      const s = typeof v === 'object' ? JSON.stringify(v) : String(v);
      return '"' + s.replace(/"/g, '""') + '"';
    };
    const lines = [cols.map((c) => escape(c)).join(',')];
    for (const r of rows) {
      lines.push(cols.map((c) => escape(r[c])).join(','));
    }
    const blob = new Blob([lines.join('\n')], { type: 'text/csv;charset=utf-8;' });
    const url = URL.createObjectURL(blob);
    const a = document.createElement('a');
    a.href = url;
    a.download = `raw-data-export-${new Date().toISOString().slice(0,19).replace(/[:T]/g, '-')}.csv`;
    document.body.appendChild(a);
    a.click();
    a.remove();
    URL.revokeObjectURL(url);
  };

  return (
    <div style={{ fontFamily: 'Arial, sans-serif', background: '#f3f4f6', minHeight: '100vh', color: '#111827' }}>
      <div style={{ maxWidth: 1280, margin: '0 auto', padding: 24 }}>
        <div style={{ display: 'flex', justifyContent: 'space-between', alignItems: 'center', marginBottom: 24 }}>
          <div>
            <button
              onClick={() => setPage('home')}
              style={{
                padding: '12px 18px',
                marginRight: 10,
                borderRadius: 12,
                border: page === 'home' ? '2px solid #2563eb' : '1px solid #d1d5db',
                background: page === 'home' ? '#eff6ff' : '#ffffff',
                cursor: 'pointer',
                fontWeight: 700,
              }}
            >
              Home
            </button>
            <button
              onClick={() => setPage('admin')}
              style={{
                padding: '12px 18px',
                borderRadius: 12,
                border: page === 'admin' ? '2px solid #2563eb' : '1px solid #d1d5db',
                background: page === 'admin' ? '#eff6ff' : '#ffffff',
                cursor: 'pointer',
                fontWeight: 700,
              }}
            >
              Admin Portal
            </button>
          </div>
          <div style={{ display: 'grid', gap: 8 }}>
            <button
              onClick={handleSync}
              style={{ padding: '12px 18px', borderRadius: 12, border: 'none', background: '#2563eb', color: '#ffffff', cursor: 'pointer', fontWeight: 700 }}
            >
              Sync SurveyCTO
            </button>
            {syncStatus && (
              <div style={{ color: syncStatus.startsWith('Sync failed') ? '#b91c1c' : '#1f2937', fontSize: 14, fontWeight: 700 }}>
                {syncStatus}
              </div>
            )}
          </div>
        </div>

        {page === 'admin' ? (
          <section style={{ display: 'grid', gridTemplateColumns: '1.2fr 0.8fr', gap: 24, marginBottom: 24 }}>
            <div style={{ background: '#ffffff', borderRadius: 18, padding: 24, boxShadow: '0 8px 28px rgba(15, 23, 42, 0.06)' }}>
              <h1 style={{ marginTop: 0 }}>Admin Dashboard</h1>
              <div style={{ display: 'grid', gridTemplateColumns: 'repeat(3, minmax(0, 1fr))', gap: 14, marginBottom: 24 }}>
                {[
                  { label: 'Raw submissions', value: dashboard?.raw_submission_count ?? 0, accent: '#1d4ed8' },
                  { label: 'Open issues', value: dashboard?.issue_count ?? 0, accent: '#dc2626' },
                  { label: 'Pending review', value: dashboard?.pending_review_count ?? 0, accent: '#059669' },
                  { label: 'High severity', value: dashboard?.high_severity_count ?? 0, accent: '#d97706' },
                  { label: 'Medium severity', value: dashboard?.medium_severity_count ?? 0, accent: '#f59e0b' },
                  { label: 'Staff members', value: dashboard?.staff_count ?? 0, accent: '#2563eb' },
                ].map((metric) => (
                  <div key={metric.label} style={{ background: '#f8fafc', borderRadius: 16, padding: 18 }}>
                    <div style={{ fontSize: 13, fontWeight: 700, color: '#6b7280', marginBottom: 10 }}>{metric.label}</div>
                    <div style={{ fontSize: 32, fontWeight: 800, color: metric.accent }}>{metric.value}</div>
                  </div>
                ))}
              </div>
              <div style={{ color: '#6b7280' }}>
                Last sync: {dashboard?.last_sync_at ? formatDate(dashboard.last_sync_at) : 'Not available'}
              </div>
            </div>

            <div style={{ background: '#ffffff', borderRadius: 18, padding: 24, boxShadow: '0 8px 28px rgba(15, 23, 42, 0.06)' }}>
              <h2 style={{ marginTop: 0 }}>SurveyCTO raw data browser</h2>
              <div style={{ overflowX: 'auto', marginTop: 16 }}>
                <div style={{ display: 'flex', gap: 10, marginBottom: 12, flexWrap: 'wrap' }}>
                  <input
                    value={rawDataSearch}
                    onChange={(event) => {
                      const nextValue = event.target.value;
                      setRawDataSearch(nextValue);
                      loadRawDataPage(0, false, nextValue, rawDataPageSize);
                    }}
                    placeholder="Search rows or fields"
                    style={{ flex: 1, minWidth: 220, padding: '10px 12px', borderRadius: 10, border: '1px solid #d1d5db' }}
                  />
                  <div style={{ position: 'relative' }}>
                    <button
                      onClick={() => setShowColumnPicker((s) => !s)}
                      style={{ padding: '10px 12px', borderRadius: 10, border: '1px solid #d1d5db', background: '#ffffff', cursor: 'pointer', fontWeight: 700 }}
                    >
                      Columns
                    </button>
                    {showColumnPicker && (
                      <div style={{ position: 'absolute', right: 0, top: '42px', background: '#ffffff', border: '1px solid #e5e7eb', padding: 12, borderRadius: 8, boxShadow: '0 10px 30px rgba(2,6,23,0.08)', maxHeight: 320, overflow: 'auto', zIndex: 50 }}>
                        {(rawDataTable.columns || []).map((c) => (
                          <label key={c.name} style={{ display: 'flex', alignItems: 'center', gap: 8, marginBottom: 8 }}>
                            <input
                              type="checkbox"
                              checked={visibleColumns.includes(c.name)}
                              onChange={(e) => {
                                const next = new Set(visibleColumns);
                                if (e.target.checked) next.add(c.name);
                                else next.delete(c.name);
                                setVisibleColumns(Array.from(next));
                              }}
                            />
                            <span style={{ fontSize: 13 }}>{c.name}</span>
                          </label>
                        ))}
                      </div>
                    )}
                  </div>
                  <select
                    value={rawDataPageSize}
                    onChange={(event) => {
                      const nextSize = Number(event.target.value);
                      setRawDataPageSize(nextSize);
                      loadRawDataPage(0, false, rawDataSearch, nextSize);
                    }}
                    style={{ padding: '10px 12px', borderRadius: 10, border: '1px solid #d1d5db' }}
                  >
                    <option value={25}>25 rows</option>
                    <option value={50}>50 rows</option>
                    <option value={100}>100 rows</option>
                  </select>
                </div>

                {rawDataTable.rows.length === 0 ? (
                  <div style={{ color: '#6b7280', padding: 14, borderRadius: 12, border: '1px dashed #cbd5e1' }}>
                    No SurveyCTO rows match the current search.
                  </div>
                ) : (
                  <>
                    <div style={{ color: '#6b7280', fontSize: 13, marginBottom: 8 }}>
                      Showing {Math.min(rawDataTable.rows.length, rawDataPageSize)} of {rawDataTotalCount} rows{rawDataSearch ? ` matching “${rawDataSearch}”` : ''}
                    </div>
                    <table style={{ width: '100%', borderCollapse: 'collapse', fontSize: 13 }}>
                      <thead>
                        <tr>
                          {visibleColumns.map((colName) => (
                            <th key={colName} style={{ textAlign: 'left', padding: '10px 8px', borderBottom: '1px solid #e5e7eb', background: '#f8fafc', whiteSpace: 'nowrap' }}>
                              <div style={{ display: 'flex', flexDirection: 'column', gap: 6 }}>
                                <div style={{ fontWeight: 700 }}>{colName}</div>
                                <input
                                  value={columnFilters[colName] || ''}
                                  onChange={(e) => setColumnFilters((cur) => ({ ...cur, [colName]: e.target.value }))}
                                  placeholder="Filter"
                                  style={{ padding: '6px 8px', borderRadius: 8, border: '1px solid #e5e7eb', fontSize: 12 }}
                                />
                              </div>
                            </th>
                          ))}
                        </tr>
                      </thead>
                      <tbody>
                        {filteredRawRows.map((row, index) => (
                          <tr key={`${row.submission_key || index}-${index}`}>
                            {visibleColumns.map((colName) => (
                              <td key={`${colName}-${index}`} style={{ padding: '10px 8px', borderBottom: '1px solid #f3f4f6', verticalAlign: 'top', maxWidth: 220 }}>
                                {row[colName] == null ? '—' : typeof row[colName] === 'object' ? JSON.stringify(row[colName]) : String(row[colName]).length > 120 ? `${String(row[colName]).slice(0, 117)}…` : String(row[colName])}
                              </td>
                            ))}
                            <td style={{ padding: '10px 8px', borderBottom: '1px solid #f3f4f6' }}>
                              <button
                                onClick={() => setSelectedRawRecord(row)}
                                style={{ padding: '8px 10px', borderRadius: 8, border: '1px solid #d1d5db', background: '#ffffff', cursor: 'pointer', fontWeight: 700 }}
                              >
                                View
                              </button>
                            </td>
                          </tr>
                        ))}
                      </tbody>
                    </table>
                    <div style={{ marginTop: 12, display: 'flex', justifyContent: 'space-between', alignItems: 'center', gap: 10, flexWrap: 'wrap' }}>
                      <div style={{ color: '#6b7280', fontSize: 13 }}>
                        Page {Math.floor(rawDataOffset / rawDataPageSize) + 1}
                      </div>
                      <div style={{ display: 'flex', gap: 8, flexWrap: 'wrap' }}>
                        <button
                          onClick={exportCsv}
                          style={{ padding: '10px 14px', borderRadius: 10, border: '1px solid #d1d5db', background: '#ffffff', cursor: 'pointer', fontWeight: 700 }}
                        >
                          Export CSV
                        </button>
                        <button
                          onClick={() => loadRawDataPage(Math.max(0, rawDataOffset - rawDataPageSize), false, rawDataSearch, rawDataPageSize)}
                          disabled={rawDataOffset === 0 || rawDataLoading}
                          style={{ padding: '10px 14px', borderRadius: 10, border: '1px solid #d1d5db', background: '#ffffff', cursor: rawDataOffset === 0 || rawDataLoading ? 'default' : 'pointer', opacity: rawDataOffset === 0 ? 0.6 : 1 }}
                        >
                          Previous
                        </button>
                        <button
                          onClick={() => loadRawDataPage(rawDataOffset + rawDataPageSize, false, rawDataSearch, rawDataPageSize)}
                          disabled={!rawDataHasMore || rawDataLoading}
                          style={{ padding: '10px 14px', borderRadius: 10, border: '1px solid #d1d5db', background: '#ffffff', cursor: !rawDataHasMore || rawDataLoading ? 'default' : 'pointer', opacity: !rawDataHasMore || rawDataLoading ? 0.6 : 1 }}
                        >
                          Next
                        </button>
                      </div>
                    </div>
                  </>
                )}
              </div>
            </div>

            <div style={{ background: '#ffffff', borderRadius: 18, padding: 24, boxShadow: '0 8px 28px rgba(15, 23, 42, 0.06)' }}>
              <h2 style={{ marginTop: 0 }}>Staff management</h2>
              <form onSubmit={handleCreateStaff} style={{ display: 'grid', gap: 14, marginBottom: 18 }}>
                <input
                  value={newStaff.username}
                  onChange={(e) => setNewStaff({ ...newStaff, username: e.target.value })}
                  placeholder="Name"
                  style={{ padding: '12px 14px', borderRadius: 12, border: '1px solid #d1d5db' }}
                />
                <input
                  value={newStaff.email}
                  onChange={(e) => setNewStaff({ ...newStaff, email: e.target.value })}
                  placeholder="Email"
                  type="email"
                  style={{ padding: '12px 14px', borderRadius: 12, border: '1px solid #d1d5db' }}
                />
                <select
                  value={newStaff.role}
                  onChange={(e) => setNewStaff({ ...newStaff, role: e.target.value })}
                  style={{ padding: '12px 14px', borderRadius: 12, border: '1px solid #d1d5db' }}
                >
                  <option value="reviewer">Reviewer</option>
                  <option value="admin">Admin</option>
                </select>
                <button style={{ padding: '12px 16px', borderRadius: 12, border: 'none', background: '#10b981', color: '#ffffff', fontWeight: 700, cursor: 'pointer' }}>
                  Add staff
                </button>
                {adminError && <div style={{ color: '#b91c1c' }}>{adminError}</div>}
              </form>
              <div style={{ maxHeight: 420, overflow: 'auto' }}>
                {staffMembers.length === 0 ? (
                  <div style={{ color: '#6b7280', padding: 14, borderRadius: 12, border: '1px dashed #cbd5e1' }}>
                    No staff configured yet.
                  </div>
                ) : (
                  staffMembers.map((member) => (
                    <div key={member.staff_id} style={{ display: 'flex', justifyContent: 'space-between', padding: 14, borderRadius: 12, background: '#f9fafb', marginBottom: 10 }}>
                      <div>
                        <strong>{member.username}</strong>
                        <div style={{ color: '#4b5563', fontSize: 13 }}>{member.email}</div>
                      </div>
                      <div style={{ color: '#2563eb', fontWeight: 700 }}>{member.role}</div>
                    </div>
                  ))
                )}
              </div>
            </div>
          </section>
        ) : (
          <>
            <section
          style={{
            display: 'grid',
            gridTemplateColumns: '1.15fr 0.85fr',
            gap: 24,
            alignItems: 'stretch',
            marginBottom: 24,
          }}
        >
          <div
            style={{
              background: 'linear-gradient(135deg, #0f172a, #2563eb)',
              color: 'white',
              borderRadius: 20,
              padding: 28,
              boxShadow: '0 14px 40px rgba(37, 99, 235, 0.22)',
            }}
          >
            <div style={{ fontSize: 14, fontWeight: 700, letterSpacing: 1.2, textTransform: 'uppercase', opacity: 0.8 }}>
              QC Hub
            </div>
            <h1 style={{ fontSize: 40, margin: '10px 0 12px' }}>Survey data quality, reviewed with confidence.</h1>
            <p style={{ fontSize: 16, lineHeight: 1.6, maxWidth: 640, marginBottom: 20, opacity: 0.95 }}>
              Monitor imported SurveyCTO records, track QC rule outcomes, and resolve review issues from one central workspace.
            </p>
            <div style={{ display: 'flex', gap: 12, flexWrap: 'wrap' }}>
              <button style={{ padding: '12px 16px', borderRadius: 12, border: 'none', background: '#ffffff', color: '#1d4ed8', fontWeight: 700, cursor: 'pointer' }}>
                Open Queue
              </button>
              <button style={{ padding: '12px 16px', borderRadius: 12, border: '1px solid rgba(255,255,255,0.45)', background: 'transparent', color: '#ffffff', fontWeight: 700, cursor: 'pointer' }}>
                View Workflow
              </button>
            </div>
          </div>

          <div style={{ display: 'grid', gridTemplateColumns: 'repeat(2, minmax(0, 1fr))', gap: 14 }}>
            {[
              { label: 'Total issues', value: stats.total, accent: '#1d4ed8' },
              { label: 'High severity', value: stats.highSeverity, accent: '#dc2626' },
              { label: 'Medium severity', value: stats.mediumSeverity, accent: '#f59e0b' },
              { label: 'Pending review', value: stats.pending, accent: '#059669' },
            ].map((card) => (
              <div key={card.label} style={{ background: '#ffffff', borderRadius: 16, padding: 18, boxShadow: '0 8px 30px rgba(15, 23, 42, 0.08)' }}>
                <div style={{ fontSize: 13, fontWeight: 700, color: '#6b7280', marginBottom: 10 }}>{card.label}</div>
                <div style={{ fontSize: 34, fontWeight: 800, color: card.accent }}>{card.value}</div>
              </div>
            ))}
          </div>
        </section>

        <section style={{ marginBottom: 24, display: 'grid', gridTemplateColumns: 'repeat(4, minmax(0, 1fr))', gap: 14 }}>
          {workflowSteps.map((step) => (
            <div key={step.title} style={{ background: '#ffffff', borderRadius: 16, padding: 18, boxShadow: '0 8px 28px rgba(15, 23, 42, 0.06)' }}>
              <div style={{ display: 'inline-flex', alignItems: 'center', gap: 8, padding: '6px 10px', borderRadius: 999, background: '#dbeafe', color: '#1d4ed8', fontSize: 12, fontWeight: 700 }}>
                {step.title}
              </div>
              <p style={{ marginTop: 12, color: '#4b5563', lineHeight: 1.5 }}>{step.description}</p>
            </div>
          ))}
        </section>

        <section style={{ display: 'grid', gridTemplateColumns: '1fr 1.25fr', gap: 24 }}>
          <div style={{ background: '#ffffff', borderRadius: 18, padding: 18, boxShadow: '0 8px 28px rgba(15, 23, 42, 0.06)' }}>
            <div style={{ display: 'flex', justifyContent: 'space-between', alignItems: 'center', marginBottom: 16 }}>
              <h2 style={{ margin: 0 }}>Raw SurveyCTO Pulls</h2>
              <span style={{ color: '#6b7280', fontSize: 14 }}>{rawImports.length} items</span>
            </div>

            {rawImports.length === 0 ? (
              <div style={{ border: '1px dashed #cbd5e1', borderRadius: 12, padding: 24, textAlign: 'center', color: '#6b7280' }}>
                No raw SurveyCTO submissions have been pulled into the database yet.
              </div>
            ) : (
              <div style={{ display: 'grid', gap: 10, maxHeight: 420, overflow: 'auto' }}>
                {rawImports.map((record) => (
                  <div key={record.raw_submission_id} style={{ border: '1px solid #d1d5db', borderRadius: 12, padding: 12, background: '#f9fafb' }}>
                    <div style={{ display: 'flex', justifyContent: 'space-between', gap: 8, marginBottom: 6 }}>
                      <strong>{record.submission_key}</strong>
                      <span style={{ fontSize: 12, color: '#6b7280' }}>{record.instrument_code}</span>
                    </div>
                    <pre style={{ margin: 0, whiteSpace: 'pre-wrap', color: '#1f2937', fontSize: 12, lineHeight: 1.45 }}>
                      {JSON.stringify(record.raw_payload, null, 2)}
                    </pre>
                  </div>
                ))}
              </div>
            )}
          </div>

          <div style={{ background: '#ffffff', borderRadius: 18, padding: 18, boxShadow: '0 8px 28px rgba(15, 23, 42, 0.06)' }}>
            <div style={{ display: 'flex', justifyContent: 'space-between', alignItems: 'center', marginBottom: 16 }}>
              <h2 style={{ margin: 0 }}>Review Queue</h2>
              <span style={{ color: '#6b7280', fontSize: 14 }}>{issues.length} items</span>
            </div>

            {issues.length === 0 ? (
              <div style={{ border: '1px dashed #cbd5e1', borderRadius: 12, padding: 24, textAlign: 'center', color: '#6b7280' }}>
                No queued issues yet. Pull submissions and start the workflow to populate the review board.
              </div>
            ) : (
              <div style={{ display: 'grid', gap: 10 }}>
                {issues.map((issue) => (
                  <button
                    key={issue.issue_id}
                    onClick={() => setSelectedIssue(issue)}
                    style={{
                      width: '100%',
                      textAlign: 'left',
                      borderRadius: 14,
                      border: selectedIssue?.issue_id === issue.issue_id ? '2px solid #2563eb' : '1px solid #d1d5db',
                      padding: 14,
                      background: selectedIssue?.issue_id === issue.issue_id ? '#eff6ff' : '#f9fafb',
                      cursor: 'pointer',
                    }}
                  >
                    <div style={{ display: 'flex', justifyContent: 'space-between', gap: 8 }}>
                      <strong>{issue.issue_summary || 'QC issue'}</strong>
                      <span style={{ fontSize: 12, fontWeight: 700, color: issue.severity === 'high' ? '#dc2626' : '#2563eb' }}>
                        {issue.severity}
                      </span>
                    </div>
                    <div style={{ marginTop: 6, color: '#4b5563', fontSize: 12 }}>
                      {issue.submission_key || 'N/A'} • {issue.case_id || 'N/A'} • {issue.issue_status}
                    </div>
                  </button>
                ))}
              </div>
            )}
          </div>

          <div style={{ background: '#ffffff', borderRadius: 18, padding: 18, boxShadow: '0 8px 28px rgba(15, 23, 42, 0.06)' }}>
            <h2 style={{ marginTop: 0 }}>Issue Details</h2>
            {selectedIssue ? (
              <div style={{ display: 'grid', gap: 14 }}>
                <div style={{ background: '#f8fafc', borderRadius: 12, padding: 14 }}>
                  <div style={{ fontSize: 12, color: '#6b7280', marginBottom: 6 }}>Summary</div>
                  <div style={{ fontWeight: 700 }}>{selectedIssue.issue_summary}</div>
                </div>
                <div style={{ display: 'grid', gridTemplateColumns: 'repeat(2, minmax(0, 1fr))', gap: 12 }}>
                  <div style={{ background: '#f8fafc', borderRadius: 12, padding: 12 }}>
                    <div style={{ fontSize: 12, color: '#6b7280' }}>Submission key</div>
                    <div style={{ fontWeight: 700 }}>{selectedIssue.submission_key || 'N/A'}</div>
                  </div>
                  <div style={{ background: '#f8fafc', borderRadius: 12, padding: 12 }}>
                    <div style={{ fontSize: 12, color: '#6b7280' }}>Case ID</div>
                    <div style={{ fontWeight: 700 }}>{selectedIssue.case_id || 'N/A'}</div>
                  </div>
                  <div style={{ background: '#f8fafc', borderRadius: 12, padding: 12 }}>
                    <div style={{ fontSize: 12, color: '#6b7280' }}>Severity</div>
                    <div style={{ fontWeight: 700 }}>{selectedIssue.severity}</div>
                  </div>
                  <div style={{ background: '#f8fafc', borderRadius: 12, padding: 12 }}>
                    <div style={{ fontSize: 12, color: '#6b7280' }}>Status</div>
                    <div style={{ fontWeight: 700 }}>{selectedIssue.issue_status}</div>
                  </div>
                </div>
                <div style={{ background: '#f8fafc', borderRadius: 12, padding: 12 }}>
                  <div style={{ fontSize: 12, color: '#6b7280' }}>Created</div>
                  <div>{formatDate(selectedIssue.created_at)}</div>
                </div>
                <div style={{ background: '#f8fafc', borderRadius: 12, padding: 12 }}>
                  <div style={{ fontSize: 12, color: '#6b7280' }}>Resolution note</div>
                  <div>{selectedIssue.resolution_note || 'No resolution note has been added yet.'}</div>
                </div>
              </div>
            ) : (
              <div style={{ color: '#6b7280', border: '1px dashed #cbd5e1', padding: 24, borderRadius: 12, textAlign: 'center' }}>
                Select an issue from the queue to inspect its details.
              </div>
            )}
          </div>
        </section>
        </>
      )}
        {selectedRawRecord && (
          <div style={{ position: 'fixed', inset: 0, background: 'rgba(15, 23, 42, 0.45)', display: 'flex', alignItems: 'center', justifyContent: 'center', padding: 20, zIndex: 1000 }}>
            <div style={{ background: '#ffffff', borderRadius: 18, width: 'min(900px, 100%)', maxHeight: '85vh', overflow: 'auto', padding: 24, boxShadow: '0 20px 60px rgba(15, 23, 42, 0.25)' }}>
              <div style={{ display: 'flex', justifyContent: 'space-between', alignItems: 'center', marginBottom: 16 }}>
                <h3 style={{ margin: 0 }}>Raw submission details</h3>
                <button onClick={() => setSelectedRawRecord(null)} style={{ padding: '8px 12px', borderRadius: 8, border: '1px solid #d1d5db', background: '#ffffff', cursor: 'pointer', fontWeight: 700 }}>
                  Close
                </button>
              </div>
              <div style={{ display: 'grid', gap: 12 }}>
                {Object.entries(selectedRawRecord).map(([key, value]) => (
                  <div key={key} style={{ border: '1px solid #e5e7eb', borderRadius: 12, padding: 12, background: '#f8fafc' }}>
                    <div style={{ fontWeight: 700, marginBottom: 6 }}>{key}</div>
                    <pre style={{ margin: 0, whiteSpace: 'pre-wrap', wordBreak: 'break-word', fontSize: 12, color: '#111827' }}>
                      {value == null ? '—' : typeof value === 'object' ? JSON.stringify(value, null, 2) : String(value)}
                    </pre>
                  </div>
                ))}
              </div>
            </div>
          </div>
        )}
      </div>
    </div>
  );
}

export default App;
