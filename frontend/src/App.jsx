import { useEffect, useMemo, useState } from 'react';

const API_BASE = import.meta.env.VITE_API_BASE_URL || '';

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
  const [page, setPage] = useState('insights');
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
  const [interpretEnabled, setInterpretEnabled] = useState(true);
  const [xlsformMetadata, setXlsformMetadata] = useState(null);
  const [newStaff, setNewStaff] = useState({ username: '', email: '', role: 'reviewer' });
  const [adminError, setAdminError] = useState('');
  const [queuedImports, setQueuedImports] = useState([]);
  const [queuedCount, setQueuedCount] = useState(0);
  const [workerStatus, setWorkerStatus] = useState('stopped');
  const [syncDetails, setSyncDetails] = useState(null);
  const [decodedQuestions, setDecodedQuestions] = useState([]);
  const [decodedSubmissionKey, setDecodedSubmissionKey] = useState('');
  const [decodedLoading, setDecodedLoading] = useState(false);
  const [decodedAutoLoadAttempted, setDecodedAutoLoadAttempted] = useState(false);
  const [expandedDecodedCategories, setExpandedDecodedCategories] = useState({});
  const [insights, setInsights] = useState({ respondent_count: 0, categories: [], sectors: [] });
  const [insightsLoading, setInsightsLoading] = useState(false);
  const [analysisTables, setAnalysisTables] = useState({ respondent_count: 0, tables: [], filters: [], filter_field: null, questions: [], question_id: null });
  const [selectedAnalysisTableId, setSelectedAnalysisTableId] = useState('');
  const [selectedAnalysisCut, setSelectedAnalysisCut] = useState('Total');
  const [selectedAnalysisGroupLabels, setSelectedAnalysisGroupLabels] = useState([]);
  const [showAnalysisFilterOptions, setShowAnalysisFilterOptions] = useState(false);

  const loadHomeData = () => {
    fetch(`${API_BASE}/api/qc/review-queue?limit=20`)
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

    fetch(`${API_BASE}/api/import/survey-platform/raw?limit=20&include_payload=false`)
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
    const query = new URLSearchParams({ limit: String(pageSize), offset: String(offset), interpret: String(interpretEnabled) });
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

    // fetch XLSForm metadata for question labels
    fetch(`${API_BASE}/api/admin/xlsform-metadata`)
      .then((r) => r.json())
      .then((m) => setXlsformMetadata(m))
      .catch(() => setXlsformMetadata(null));

    // fetch queued imports and sync status
    fetch(`${API_BASE}/api/import/queued?limit=20&include_payload=false`)
      .then((r) => r.json())
      .then((d) => {
        setQueuedImports(d.items || []);
        setQueuedCount(d.count || 0);
      })
      .catch(() => {
        setQueuedImports([]);
        setQueuedCount(0);
      });

    fetch(`${API_BASE}/api/import/sync-status`)
      .then((r) => r.json())
      .then((d) => setSyncDetails(d))
      .catch(() => setSyncDetails(null));

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
    if (page === 'decoded') {
      loadLatestDecodedQuestions();
    }
  }, [page]);

  useEffect(() => {
    if (page === 'insights') loadInsights();
  }, [page]);

  useEffect(() => {
    // initialize visible columns whenever the table metadata changes
    const cols = rawDataTable.columns || [];
    if (cols.length > 0 && visibleColumns.length === 0) {
      setVisibleColumns(cols.slice(0, 10).map((c) => c.name));
    }
  }, [rawDataTable.columns]);

  const handleSync = () => {
    // Trigger background sync and poll dashboard for updates so the UI doesn't block.
    setSyncStatus('Sync started...');
    fetch(`${API_BASE}/api/import/survey-platform/sync-async`, { method: 'POST' })
      .then(async (response) => {
        const body = await response.text();
        if (!response.ok) {
          let result = {};
          try {
            result = body ? JSON.parse(body) : {};
          } catch { /* Use the HTTP status when the error body is not JSON. */ }
          throw new Error(result.detail || result.message || `Sync request failed (HTTP ${response.status})`);
        }

        return body;
      })
      .then(() => {
        // Immediately refresh available DB data and start polling for the sync completion
        loadHomeData();
        if (page === 'admin') loadAdminData();

        const start = Date.now();
        const pollInterval = 3000;
        const timeoutMs = 60000; // poll up to 60s

        const id = setInterval(() => {
          loadAdminData();
          loadHomeData();
          // stop after timeout
          if (Date.now() - start > timeoutMs) {
            clearInterval(id);
            setSyncStatus('Sync running in background; check dashboard for updates.');
          }
        }, pollInterval);

        // stop polling when admin page updates last_sync_at or after timeout
        setTimeout(() => clearInterval(id), timeoutMs + 2000);
      })
      .catch((error) => {
        setSyncStatus(`Sync trigger failed: ${error.message}`);
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

  const fetchQueued = () => {
    fetch(`${API_BASE}/api/import/queued?limit=20&include_payload=false`)
      .then((r) => r.json())
      .then((d) => {
        setQueuedImports(d.items || []);
        setQueuedCount(d.count || 0);
      })
      .catch(() => {
        setQueuedImports([]);
        setQueuedCount(0);
      });
  };

  const fetchSyncDetails = () => {
    fetch(`${API_BASE}/api/import/sync-status`)
      .then((r) => r.json())
      .then((d) => setSyncDetails(d))
      .catch(() => setSyncDetails(null));
  };

  const loadDecodedQuestions = (submissionKey) => {
    if (!submissionKey) return;
    setDecodedLoading(true);
    fetch(`${API_BASE}/api/admin/decoded-questions/${encodeURIComponent(submissionKey)}`)
      .then((r) => r.json())
      .then((data) => {
        const rows = data.rows || [];
        setDecodedQuestions(rows);
        setDecodedSubmissionKey(submissionKey);
        setExpandedDecodedCategories((current) => {
          if (Object.keys(current).length > 0) return current;
          const firstCategory = rows[0]?.category || 'General';
          return { [firstCategory]: true };
        });
      })
      .catch(() => {
        setDecodedQuestions([]);
        setDecodedSubmissionKey(submissionKey);
      })
      .finally(() => setDecodedLoading(false));
  };

  const loadLatestDecodedQuestions = () => {
    if (decodedAutoLoadAttempted) return;
    setDecodedAutoLoadAttempted(true);
    fetch(`${API_BASE}/api/admin/raw-data-table?limit=1&offset=0&interpret=false`)
      .then((response) => response.json())
      .then((data) => {
        const firstRow = (data.rows || [])[0];
        const fallbackKey = firstRow?.submission_key || '';
        if (fallbackKey) {
          loadDecodedQuestions(fallbackKey);
        }
      })
      .catch(() => {
        setDecodedQuestions([]);
      });
  };

  const [appliedResponseFilter, setAppliedResponseFilter] = useState(null);

  const loadInsights = (filterField = '', questionId = '', responseQuestion = '', responseValue = '') => {
    setInsightsLoading(true);
    const analysisQuery = new URLSearchParams({ category: 'noodles' });
    if (filterField) analysisQuery.set('filter_field', filterField);
    if (questionId) analysisQuery.set('question_id', questionId);
    if (responseQuestion) analysisQuery.set('filter_question', responseQuestion);
    if (responseValue) analysisQuery.set('filter_value', responseValue);
    Promise.all([
      fetch(`${API_BASE}/api/insights/overview`).then((response) => response.ok ? response.json() : Promise.reject()),
      fetch(`${API_BASE}/api/analytics/tables?${analysisQuery.toString()}`).then((response) => response.ok ? response.json() : Promise.reject()),
    ])
      .then(([overview, tableData]) => {
        setInsights(overview);
        setAnalysisTables(tableData);
        setSelectedAnalysisTableId(tableData.question_id || tableData.tables?.[0]?.id || '');
        setSelectedAnalysisCut(tableData.filter_field || 'Total');
        const loadedTable = tableData.tables?.find((table) => table.id === (tableData.question_id || tableData.tables?.[0]?.id)) || tableData.tables?.[0];
        const loadedGroups = loadedTable?.cuts?.find((cut) => cut.field === tableData.filter_field)?.groups || [];
        setSelectedAnalysisGroupLabels((current) => {
          const available = loadedGroups.map((group) => group.label);
          const retained = current.filter((label) => available.includes(label));
          return retained.length ? retained : available;
        });
        if (responseQuestion && responseValue) setAppliedResponseFilter({ question: responseQuestion, value: responseValue });
        else setAppliedResponseFilter(null);
      })
      .catch(() => {
        setInsights({ respondent_count: 0, categories: [], sectors: [] });
        setAnalysisTables({ respondent_count: 0, tables: [], filters: [], filter_field: null, questions: [], question_id: null });
        setSelectedAnalysisGroupLabels([]);
        setAppliedResponseFilter(null);
      })
      .finally(() => setInsightsLoading(false));
  };

  const decodedQuestionGroups = useMemo(() => {
    return decodedQuestions.reduce((groups, row) => {
      const category = row.category || 'General';
      if (!groups[category]) groups[category] = [];
      groups[category].push(row);
      return groups;
    }, {});
  }, [decodedQuestions]);

  const toggleDecodedCategory = (category) => {
    setExpandedDecodedCategories((current) => ({
      ...current,
      [category]: !current[category],
    }));
  };

  const DistributionCard = ({ title, items, emptyMessage }) => {
    const topCount = Math.max(...items.map((item) => item.count), 1);
    const colors = ['#1aa7e0', '#10b981', '#f59e0b', '#8b5cf6', '#f43f5e', '#3b82f6', '#14b8a6'];
    return (
      <section style={{ background: '#ffffff', borderRadius: 22, padding: 22, boxShadow: '0 12px 30px rgba(15, 23, 42, 0.06)', minWidth: 0 }}>
        <div style={{ display: 'flex', alignItems: 'center', justifyContent: 'space-between', marginBottom: 16 }}>
          <h2 style={{ margin: 0, color: '#0875b8', fontSize: 13, letterSpacing: '0.14em', textTransform: 'uppercase' }}>{title}</h2>
          <span style={{ width: 32, height: 32, borderRadius: '50%', display: 'grid', placeItems: 'center', background: '#216ee8', color: '#ffffff', fontSize: 16 }}>▥</span>
        </div>
        {items.length === 0 ? (
          <div style={{ padding: '28px 10px', color: '#64748b', textAlign: 'center', border: '1px dashed #cbd5e1', borderRadius: 12 }}>{emptyMessage}</div>
        ) : (
          <div style={{ display: 'grid', gap: 10, maxHeight: 520, overflowY: 'auto', paddingRight: 3 }}>
            {items.map((item, index) => (
              <div key={item.label} style={{ padding: '11px 10px', borderRadius: 12, background: '#fbfdff' }}>
                <div style={{ display: 'flex', justifyContent: 'space-between', gap: 14, fontSize: 13, color: '#17233a', marginBottom: 8 }}>
                  <span style={{ overflow: 'hidden', textOverflow: 'ellipsis', whiteSpace: 'nowrap' }}>{item.label}</span>
                  <strong style={{ whiteSpace: 'nowrap' }}>{item.count.toLocaleString()} ({item.pct}%)</strong>
                </div>
                <div style={{ height: 7, borderRadius: 999, background: '#e8edf3', overflow: 'hidden' }}>
                  <div style={{ width: `${(item.count / topCount) * 100}%`, height: '100%', background: colors[index % colors.length], borderRadius: 999 }} />
                </div>
              </div>
            ))}
          </div>
        )}
      </section>
    );
  };

  const selectedAnalysisTable = useMemo(
    () => analysisTables.tables.find((table) => table.id === selectedAnalysisTableId) || analysisTables.tables[0],
    [analysisTables.tables, selectedAnalysisTableId],
  );

  const selectedAnalysisGroups = useMemo(() => {
    if (!selectedAnalysisTable || selectedAnalysisCut === 'Total') return [];
    const groups = selectedAnalysisTable.cuts?.find((cut) => cut.field === selectedAnalysisCut)?.groups || [];
    return groups.filter((group) => selectedAnalysisGroupLabels.includes(group.label));
  }, [selectedAnalysisTable, selectedAnalysisCut, selectedAnalysisGroupLabels]);

  const selectedAnalysisCutDetails = useMemo(
    () => selectedAnalysisTable?.cuts?.find((cut) => cut.field === selectedAnalysisCut),
    [selectedAnalysisTable, selectedAnalysisCut],
  );

  const processNext = () => {
    fetch(`${API_BASE}/api/import/process-next`, { method: 'POST' })
      .then((r) => r.json())
      .then(() => {
        fetchQueued();
        loadHomeData();
        if (page === 'admin') loadAdminData();
      })
      .catch(() => {
        // ignore
      });
  };

  const startWorker = () => {
    fetch(`${API_BASE}/api/import/start-worker`, { method: 'POST' })
      .then((r) => r.json())
      .then(() => {
        setWorkerStatus('running');
        fetchQueued();
      })
      .catch(() => {
        // ignore
      });
  };

  const stopWorker = () => {
    fetch(`${API_BASE}/api/import/stop-worker`, { method: 'POST' })
      .then((r) => r.json())
      .then(() => {
        setWorkerStatus('stopped');
        fetchQueued();
      })
      .catch(() => {
        // ignore
      });
  };

  return (
    <div style={{ fontFamily: 'Arial, sans-serif', background: '#f3f4f6', minHeight: '100vh', color: '#111827' }}>
      <div style={{ maxWidth: 1280, margin: '0 auto', padding: 24 }}>
        <div style={{ display: 'flex', justifyContent: 'space-between', alignItems: 'center', marginBottom: 24 }}>
          <div>
            <button
              onClick={() => setPage('insights')}
              style={{
                padding: '12px 18px',
                marginRight: 10,
                borderRadius: 12,
                border: page === 'insights' ? '2px solid #2563eb' : '1px solid #d1d5db',
                background: page === 'insights' ? '#eff6ff' : '#ffffff',
                cursor: 'pointer',
                fontWeight: 700,
              }}
            >
              Insights
            </button>
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
            <button
              onClick={() => setPage('decoded')}
              style={{
                padding: '12px 18px',
                borderRadius: 12,
                border: page === 'decoded' ? '2px solid #2563eb' : '1px solid #d1d5db',
                background: page === 'decoded' ? '#eff6ff' : '#ffffff',
                cursor: 'pointer',
                fontWeight: 700,
              }}
            >
              Decoded Questions
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

        {page === 'insights' ? (
          <section style={{ display: 'grid', gap: 16, marginBottom: 24 }}>
            <div style={{ display: 'flex', justifyContent: 'space-between', alignItems: 'end', gap: 16, flexWrap: 'wrap' }}>
              <div>
                <div style={{ color: '#0875b8', fontWeight: 800, fontSize: 13, letterSpacing: '0.14em', textTransform: 'uppercase' }}>Survey interpretation</div>
                <h1 style={{ margin: '6px 0 0', fontSize: 30 }}>Question analysis</h1>
                <p style={{ margin: '6px 0 0', color: '#64748b' }}>Choose a question, then compare its answer options by any available filter.</p>
              </div>
              <button onClick={() => loadInsights(selectedAnalysisCut === 'Total' ? '' : selectedAnalysisCut, selectedAnalysisTableId)} style={{ padding: '10px 14px', borderRadius: 10, border: 'none', background: '#216ee8', color: '#ffffff', cursor: 'pointer', fontWeight: 700 }}>
                {insightsLoading ? 'Refreshing…' : 'Refresh insights'}
              </button>
            </div>
            <section style={{ display: 'grid', gridTemplateColumns: '280px minmax(0, 1fr)', background: '#ffffff', borderRadius: 22, overflow: 'hidden', boxShadow: '0 12px 30px rgba(15, 23, 42, 0.06)' }}>
              <aside style={{ padding: 18, background: '#f8fafc', borderRight: '1px solid #e2e8f0', maxHeight: '72vh', overflowY: 'auto' }}>
                <div style={{ color: '#0875b8', fontSize: 12, fontWeight: 800, letterSpacing: '0.1em', textTransform: 'uppercase', marginBottom: 10 }}>Questions</div>
                <select value={selectedAnalysisTableId} onChange={(event) => loadInsights(selectedAnalysisCut === 'Total' ? '' : selectedAnalysisCut, event.target.value)} style={{ width: '100%', padding: 10, borderRadius: 8, border: '1px solid #cbd5e1', background: '#fff', marginBottom: 12 }}>
                  {(analysisTables.questions || []).map((question) => <option key={question.id} value={question.id}>{question.label}</option>)}
                </select>
                <div style={{ display: 'grid', gap: 6 }}>{(analysisTables.questions || []).map((question) => <button key={question.id} onClick={() => loadInsights(selectedAnalysisCut === 'Total' ? '' : selectedAnalysisCut, question.id)} style={{ padding: '9px 10px', border: 'none', borderRadius: 8, textAlign: 'left', cursor: 'pointer', background: question.id === selectedAnalysisTableId ? '#dbeafe' : 'transparent', color: '#1e3a5f', fontSize: 12, fontWeight: question.id === selectedAnalysisTableId ? 700 : 500 }}>{question.label}</button>)}</div>
                <div style={{ marginTop: 14 }}>
                  <div style={{ color: '#64748b', fontSize: 12, fontWeight: 800, marginBottom: 8 }}>Options</div>
                  {selectedAnalysisTable ? (
                    <div style={{ display: 'grid', gap: 8 }}>
                      {selectedAnalysisTable.rows.map((row) => (
                        <button
                          key={row.label}
                          onClick={() => loadInsights(selectedAnalysisCut === 'Total' ? '' : selectedAnalysisCut, selectedAnalysisTableId, selectedAnalysisTableId, row.label)}
                          style={{ display: 'flex', justifyContent: 'space-between', padding: '8px 10px', borderRadius: 8, border: '1px solid #e6eef8', background: appliedResponseFilter && appliedResponseFilter.question === selectedAnalysisTableId && appliedResponseFilter.value === row.label ? '#dbeafe' : '#fff', cursor: 'pointer', fontSize: 13 }}
                        >
                          <span style={{ textAlign: 'left', overflow: 'hidden', textOverflow: 'ellipsis', whiteSpace: 'nowrap' }}>{row.label}</span>
                          <span style={{ color: '#64748b', marginLeft: 8 }}>{row.count}</span>
                        </button>
                      ))}
                      {appliedResponseFilter && appliedResponseFilter.question === selectedAnalysisTableId && (
                        <div style={{ display: 'flex', gap: 8, alignItems: 'center', marginTop: 8 }}>
                          <div style={{ fontSize: 12, color: '#374151' }}>Filter applied: <strong>{appliedResponseFilter.value}</strong></div>
                          <button onClick={() => loadInsights(selectedAnalysisCut === 'Total' ? '' : selectedAnalysisCut, selectedAnalysisTableId)} style={{ padding: '6px 8px', borderRadius: 8, border: '1px solid #d1d5db', background: '#fff', cursor: 'pointer' }}>Clear</button>
                        </div>
                      )}
                    </div>
                  ) : (
                    <div style={{ color: '#9ca3af', fontSize: 13 }}>Select a question to view its options.</div>
                  )}
                </div>
              </aside>
              <div style={{ padding: 22, minWidth: 0 }}>
              <div style={{ display: 'flex', justifyContent: 'space-between', alignItems: 'end', gap: 12, flexWrap: 'wrap', marginBottom: 16 }}>
                <div>
                  <div style={{ color: '#0875b8', fontWeight: 800, fontSize: 13, letterSpacing: '0.14em', textTransform: 'uppercase' }}>Noodles tracker</div>
                  <h2 style={{ margin: '5px 0 0', fontSize: 20 }}>{selectedAnalysisTable?.title || 'Generated analysis tables'}</h2>
                  {selectedAnalysisTable && <div style={{ marginTop: 4, color: '#64748b', fontSize: 13 }}>Base: {selectedAnalysisTable.base.toLocaleString()} · {selectedAnalysisTable.question}</div>}
                </div>
                <a href={`${API_BASE}/api/analytics/tables/export?category=noodles`} style={{ padding: '10px 14px', borderRadius: 10, background: '#eff6ff', color: '#155dc4', fontWeight: 700, textDecoration: 'none' }}>Download Excel</a>
              </div>
              {analysisTables.tables.length > 0 ? (
                <>
                  <div style={{ display: 'flex', alignItems: 'end', gap: 10, margin: '4px 0 14px', flexWrap: 'wrap' }}>
                    <div style={{ display: 'grid', gap: 5, minWidth: 0 }}>
                      <label htmlFor="analysis-cut" style={{ color: '#52667d', fontSize: 13, fontWeight: 700 }}>Filter question</label>
                      <select id="analysis-cut" value={selectedAnalysisCut} onChange={(event) => { const field = event.target.value; setSelectedAnalysisGroupLabels([]); if (field === 'Total') setSelectedAnalysisCut('Total'); else loadInsights(field, selectedAnalysisTableId); }} style={{ width: 260, maxWidth: '100%', padding: '8px 10px', border: '1px solid #cbd5e1', borderRadius: 8, background: '#fff', color: '#1e3a5f' }}>
                        <option value="Total">Total sample</option>
                        {(analysisTables.filters || []).map((filter) => <option key={filter.field} value={filter.field}>{filter.label}</option>)}
                      </select>
                    </div>
                    {selectedAnalysisCut !== 'Total' && <div style={{ display: 'grid', gap: 5, minWidth: 0, position: 'relative' }}>
                      <label htmlFor="analysis-cut-options" style={{ color: '#52667d', fontSize: 13, fontWeight: 700 }}>Filter options</label>
                      <button id="analysis-cut-options" type="button" onClick={() => setShowAnalysisFilterOptions((open) => !open)} style={{ width: 260, maxWidth: '100%', padding: '8px 10px', border: '1px solid #cbd5e1', borderRadius: 8, background: '#fff', color: '#1e3a5f', cursor: 'pointer', textAlign: 'left', display: 'flex', justifyContent: 'space-between', gap: 8 }}>
                        <span>{selectedAnalysisGroupLabels.length === (selectedAnalysisCutDetails?.groups || []).length ? `All ${selectedAnalysisGroupLabels.length} options` : `${selectedAnalysisGroupLabels.length} option${selectedAnalysisGroupLabels.length === 1 ? '' : 's'} selected`}</span><span>⌄</span>
                      </button>
                      {showAnalysisFilterOptions && <div style={{ position: 'absolute', zIndex: 10, top: 67, left: 0, width: 300, maxWidth: 'calc(100vw - 48px)', maxHeight: 280, overflowY: 'auto', padding: 10, border: '1px solid #cbd5e1', borderRadius: 10, background: '#fff', boxShadow: '0 12px 24px rgba(15, 23, 42, 0.16)' }}>
                        <div style={{ display: 'flex', justifyContent: 'space-between', gap: 8, marginBottom: 8 }}>
                          <button type="button" onClick={() => setSelectedAnalysisGroupLabels((selectedAnalysisCutDetails?.groups || []).map((group) => group.label))} style={{ border: 'none', background: 'transparent', color: '#155dc4', cursor: 'pointer', fontSize: 12, fontWeight: 700 }}>Select all</button>
                          <button type="button" onClick={() => setSelectedAnalysisGroupLabels([])} style={{ border: 'none', background: 'transparent', color: '#155dc4', cursor: 'pointer', fontSize: 12, fontWeight: 700 }}>Clear</button>
                        </div>
                        <div style={{ display: 'grid', gap: 4 }}>{(selectedAnalysisCutDetails?.groups || []).map((group) => <label key={group.label} style={{ display: 'flex', alignItems: 'center', gap: 8, padding: '6px 4px', cursor: 'pointer', fontSize: 13 }}><input type="checkbox" checked={selectedAnalysisGroupLabels.includes(group.label)} onChange={() => setSelectedAnalysisGroupLabels((current) => current.includes(group.label) ? current.filter((label) => label !== group.label) : [...current, group.label])} /><span style={{ overflow: 'hidden', textOverflow: 'ellipsis', whiteSpace: 'nowrap' }}>{group.label} ({group.base.toLocaleString()})</span></label>)}</div>
                      </div>}
                    </div>}
                    {selectedAnalysisCut !== 'Total' && <span style={{ color: '#64748b', fontSize: 12, maxWidth: 260 }}>Choose one or more options. Each column shows N and % within that group.</span>}
                    {selectedAnalysisCutDetails?.truncated && <span style={{ color: '#9a3412', fontSize: 12 }}>Showing the {selectedAnalysisGroups.length} largest values out of {selectedAnalysisCutDetails.total_groups}.</span>}
                  </div>
                  <div style={{ overflowX: 'auto' }}>
                    <table style={{ width: '100%', borderCollapse: 'collapse', fontSize: 13 }}>
                      <thead><tr style={{ background: '#f8fafc', color: '#475569', textAlign: 'left' }}><th style={{ padding: 10 }}>Response</th>{selectedAnalysisCut === 'Total' ? <><th style={{ padding: 10, textAlign: 'right' }}>N</th><th style={{ padding: 10, textAlign: 'right' }}>%</th></> : selectedAnalysisGroups.map((group) => <th key={group.label} style={{ padding: 10, textAlign: 'right', whiteSpace: 'nowrap' }}>{group.label}<small style={{ display: 'block', color: '#64748b' }}>Base {group.base}</small></th>)}</tr></thead>
                      <tbody>{selectedAnalysisTable?.rows.map((row) => <tr key={row.label} style={{ borderTop: '1px solid #edf2f7' }}><td style={{ padding: 10 }}>{row.label}</td>{selectedAnalysisCut === 'Total' ? <><td style={{ padding: 10, textAlign: 'right', fontWeight: 700 }}>{row.count.toLocaleString()}</td><td style={{ padding: 10, textAlign: 'right' }}>{row.pct}%</td></> : selectedAnalysisGroups.map((group) => { const count = group.counts?.[row.label] || 0; const pct = group.base ? Math.round((count / group.base) * 1000) / 10 : 0; return <td key={group.label} style={{ padding: 10, textAlign: 'right' }}>{count.toLocaleString()} <span style={{ color: '#64748b' }}>({pct}%)</span></td>; })}</tr>)}</tbody>
                    </table>
                  </div>
                </>
              ) : <div style={{ color: '#64748b', padding: 18, border: '1px dashed #cbd5e1', borderRadius: 12 }}>Noodles analysis tables will appear after SurveyCTO submissions are synced.</div>}
              </div>
            </section>
          </section>
        ) : page === 'decoded' ? (
          <section style={{ display: 'grid', gap: 24, marginBottom: 24 }}>
            <div style={{ background: '#ffffff', borderRadius: 18, padding: 24, boxShadow: '0 8px 28px rgba(15, 23, 42, 0.06)' }}>
              <h1 style={{ marginTop: 0 }}>Decoded question view</h1>
              <p style={{ color: '#4b5563', marginTop: -6 }}>This page shows the questionnaire questions and the human-readable responses for a selected submission, without the raw payload.</p>
              <div style={{ display: 'flex', gap: 10, marginBottom: 12, flexWrap: 'wrap' }}>
                <div style={{ flex: 1, minWidth: 220, padding: '10px 12px', borderRadius: 10, border: '1px solid #d1d5db', background: '#f8fafc', color: '#374151' }}>
                  {decodedSubmissionKey || 'Loading the latest available submission…'}
                </div>
                <button
                  onClick={() => loadLatestDecodedQuestions()}
                  style={{ padding: '10px 12px', borderRadius: 10, border: 'none', background: '#2563eb', color: '#ffffff', cursor: 'pointer', fontWeight: 700 }}
                >
                  Reload latest view
                </button>
              </div>
              {decodedLoading ? (
                <div style={{ color: '#6b7280' }}>Loading decoded questions…</div>
              ) : decodedQuestions.length > 0 ? (
                <div style={{ display: 'grid', gap: 8 }}>
                  {Object.entries(decodedQuestionGroups).map(([category, rows]) => {
                    const isExpanded = Boolean(expandedDecodedCategories[category]);
                    return (
                      <section key={category} style={{ border: '1px solid #e2e8f0', borderRadius: 12, overflow: 'hidden', background: '#ffffff' }}>
                        <button
                          type="button"
                          onClick={() => toggleDecodedCategory(category)}
                          aria-expanded={isExpanded}
                          style={{ width: '100%', display: 'flex', alignItems: 'center', justifyContent: 'space-between', gap: 12, padding: '10px 12px', border: 'none', background: '#f8fafc', color: '#1e293b', cursor: 'pointer', textAlign: 'left', fontWeight: 700 }}
                        >
                          <span>{category}</span>
                          <span style={{ display: 'inline-flex', alignItems: 'center', gap: 8, color: '#64748b', fontSize: 12, fontWeight: 600 }}>
                            {rows.length} {rows.length === 1 ? 'response' : 'responses'}
                            <span aria-hidden="true">{isExpanded ? '−' : '+'}</span>
                          </span>
                        </button>
                        {isExpanded && (
                          <div style={{ display: 'grid' }}>
                            {rows.map((row, index) => (
                              <div key={`${category}-${row.question}-${index}`} style={{ display: 'grid', gridTemplateColumns: 'minmax(180px, 0.9fr) minmax(0, 1.4fr)', gap: 16, padding: '9px 12px', borderTop: '1px solid #eef2f7' }}>
                                <div style={{ fontSize: 13, fontWeight: 700, color: '#334155' }}>{row.question}</div>
                                <div style={{ color: '#0f766e', fontSize: 13, overflowWrap: 'anywhere' }}>{row.response || '—'}</div>
                              </div>
                            ))}
                          </div>
                        )}
                      </section>
                    );
                  })}
                </div>
              ) : (
                <div style={{ color: '#6b7280', padding: 12, borderRadius: 12, border: '1px dashed #cbd5e1' }}>No decoded questions are available yet for the latest submission.</div>
              )}
            </div>
          </section>
        ) : page === 'admin' ? (
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
                    <span style={{ marginLeft: 10, display: 'inline-flex', alignItems: 'center', gap: 8 }}>
                      <label style={{ fontSize: 13, display: 'flex', alignItems: 'center', gap: 8 }}>
                        <input type="checkbox" checked={interpretEnabled} onChange={(e) => { setInterpretEnabled(e.target.checked); loadRawDataPage(0, false, rawDataSearch, rawDataPageSize); }} />
                        Interpret
                      </label>
                      {interpretEnabled && (
                        <div style={{ padding: '6px 8px', background: '#ecfeff', color: '#0f766e', borderRadius: 999, fontSize: 12, fontWeight: 700 }}>
                          Interpreted
                        </div>
                      )}
                    </span>
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
                                {interpretEnabled && (
                                  <div style={{ marginTop: 6, fontSize: 11, color: '#065f46' }}>
                                    <small>interpreted</small>
                                  </div>
                                )}
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

            <div style={{ background: '#ffffff', borderRadius: 18, padding: 24, boxShadow: '0 8px 28px rgba(15, 23, 42, 0.06)' }}>
              <h2 style={{ marginTop: 0 }}>New Submissions Queue</h2>
              <div style={{ marginBottom: 12, display: 'flex', gap: 8, alignItems: 'center' }}>
                <button onClick={processNext} style={{ padding: '8px 12px', borderRadius: 8, border: 'none', background: '#2563eb', color: '#fff', cursor: 'pointer', fontWeight: 700 }}>Process Next</button>
                {workerStatus === 'running' ? (
                  <button onClick={stopWorker} style={{ padding: '8px 12px', borderRadius: 8, border: '1px solid #d1d5db', background: '#fff', cursor: 'pointer' }}>Stop Worker</button>
                ) : (
                  <button onClick={startWorker} style={{ padding: '8px 12px', borderRadius: 8, border: '1px solid #d1d5db', background: '#fff', cursor: 'pointer' }}>Start Auto-Worker</button>
                )}
                <div style={{ marginLeft: 'auto', color: '#6b7280' }}>Queued: {queuedCount}</div>
              </div>
              <div style={{ maxHeight: 260, overflow: 'auto' }}>
                {queuedImports.length === 0 ? (
                  <div style={{ color: '#6b7280', padding: 12, borderRadius: 8, border: '1px dashed #cbd5e1' }}>No queued submissions.</div>
                ) : (
                  queuedImports.map((q) => (
                    <div key={q.job_id} style={{ borderRadius: 10, padding: 10, marginBottom: 8, background: '#f8fafc' }}>
                      <div style={{ display: 'flex', justifyContent: 'space-between', gap: 8 }}>
                        <div>
                          <div style={{ fontWeight: 700 }}>{q.submission_key}</div>
                          <div style={{ fontSize: 12, color: '#4b5563' }}>{q.instrument_code} • {q.fetched_at ? new Date(q.fetched_at).toLocaleString() : '—'}</div>
                        </div>
                        <div style={{ fontSize: 12, color: '#6b7280' }}>{q.queued_at ? new Date(q.queued_at).toLocaleString() : '—'}</div>
                      </div>
                      {q.raw_payload && (
                        <pre style={{ marginTop: 8, whiteSpace: 'pre-wrap', fontSize: 12 }}>{JSON.stringify(q.raw_payload, null, 2)}</pre>
                      )}
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
                {(() => {
                  const qmap = xlsformMetadata && xlsformMetadata.questions ? xlsformMetadata.questions : null;
                  if (qmap) {
                    const keys = Object.keys(qmap).filter((k) => Object.prototype.hasOwnProperty.call(selectedRawRecord, k));
                    if (keys.length > 0) {
                      return keys.map((key) => {
                        const meta = qmap[key] || {};
                        const label = meta.label || meta.prompt || meta.question || meta.text || key;
                        const value = selectedRawRecord[key];
                        return (
                          <div key={key} style={{ border: '1px solid #e5e7eb', borderRadius: 12, padding: 12, background: '#f8fafc' }}>
                            <div style={{ fontWeight: 700, marginBottom: 6 }}>{label}</div>
                            <pre style={{ margin: 0, whiteSpace: 'pre-wrap', wordBreak: 'break-word', fontSize: 12, color: '#111827' }}>
                              {value == null ? '—' : typeof value === 'object' ? JSON.stringify(value, null, 2) : String(value)}
                            </pre>
                          </div>
                        );
                      });
                    }
                  }
                  // fallback: show raw key/values
                  return Object.entries(selectedRawRecord).map(([key, value]) => (
                    <div key={key} style={{ border: '1px solid #e5e7eb', borderRadius: 12, padding: 12, background: '#f8fafc' }}>
                      <div style={{ fontWeight: 700, marginBottom: 6 }}>{key}</div>
                      <pre style={{ margin: 0, whiteSpace: 'pre-wrap', wordBreak: 'break-word', fontSize: 12, color: '#111827' }}>
                        {value == null ? '—' : typeof value === 'object' ? JSON.stringify(value, null, 2) : String(value)}
                      </pre>
                    </div>
                  ));
                })()}
              </div>
            </div>
          </div>
        )}
      </div>
    </div>
  );
}

export default App;
