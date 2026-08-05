import { useEffect, useMemo, useState } from 'react';
import inicioLogo from '../../assets/Inicio-pics2.png';

const API_BASE = import.meta.env.VITE_API_BASE_URL || '';

const splitQCReport = (evidence = '') => {
  const marker = '\n\nNext check: ';
  const index = evidence.indexOf(marker);
  return index === -1
    ? { evidence, nextCheck: '' }
    : { evidence: evidence.slice(0, index), nextCheck: evidence.slice(index + marker.length) };
};

const workflowSteps = [
  { title: 'Import', description: 'Pull SurveyCTO submissions into the QC Hub raw ingestion layer.' },
  { title: 'Process', description: 'Normalize and stage imported records for downstream review.' },
  { title: 'Transform', description: 'Map incoming payloads into the internal canonical QC model.' },
  { title: 'Review', description: 'Route issues through the queue for action and resolution.' },
];

function App() {
  const [issues, setIssues] = useState([]);
  const [reviewQueueCount, setReviewQueueCount] = useState(0);
  const [reviewQueueLoading, setReviewQueueLoading] = useState(false);
  const [reviewQueueOffset, setReviewQueueOffset] = useState(0);
  const [selectedIssue, setSelectedIssue] = useState(null);
  const [rawImports, setRawImports] = useState([]);
  const [rawSubmissionCount, setRawSubmissionCount] = useState(0);
  const [page, setPage] = useState('home');
  const [adminSidebarOpen, setAdminSidebarOpen] = useState(false);
  const [staffMenuOpen, setStaffMenuOpen] = useState(false);
  const [sessionRole, setSessionRole] = useState('');
  const [sessionUserId, setSessionUserId] = useState('');
  const [loginName, setLoginName] = useState('');
  const [loginPassword, setLoginPassword] = useState('');
  const [loginError, setLoginError] = useState('');
  const [showRawBrowser, setShowRawBrowser] = useState(false);
  const [syncStatus, setSyncStatus] = useState('');
  const [dashboard, setDashboard] = useState(null);
  const [staffDashboard, setStaffDashboard] = useState({ assigned_count: 0, pending_count: 0, in_progress_count: 0, approved_count: 0, rejected_count: 0, completed_count: 0 });
  const [qualityOverview, setQualityOverview] = useState({ entities: {}, scoring_note: '' });
  const [qualityLoading, setQualityLoading] = useState(false);
  const [cleanAnalysisLoading, setCleanAnalysisLoading] = useState(false);
  const [qualityGroup, setQualityGroup] = useState('interviewers');
  const [staffMembers, setStaffMembers] = useState([]);
  const [staffSuccess, setStaffSuccess] = useState('');
  const [staffListPage, setStaffListPage] = useState(0);
  const [insightQuestionPage, setInsightQuestionPage] = useState(0);
  const [cleanQuestionPage, setCleanQuestionPage] = useState(0);
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
  const [newStaff, setNewStaff] = useState({ username: '', email: '', password: '', role: 'reviewer' });
  const [passwordChange, setPasswordChange] = useState({ current_password: '', new_password: '', confirm_password: '' });
  const [accountMessage, setAccountMessage] = useState('');
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
  const [cleanAnalysisTables, setCleanAnalysisTables] = useState({ respondent_count: 0, tables: [], questions: [], question_id: null });
  const [cleanAnalysisQuestionId, setCleanAnalysisQuestionId] = useState('');
  const [selectedAnalysisTableId, setSelectedAnalysisTableId] = useState('');
  const [selectedAnalysisCut, setSelectedAnalysisCut] = useState('Total');
  const [selectedAnalysisGroupLabels, setSelectedAnalysisGroupLabels] = useState([]);
  const [showAnalysisFilterOptions, setShowAnalysisFilterOptions] = useState(false);

  const loadHomeData = () => {
    fetch(`${API_BASE}/api/admin/dashboard`)
      .then((response) => response.json())
      .then((data) => setDashboard(data))
      .catch(() => setDashboard(null));

    loadReviewQueue();

    if (sessionRole && sessionRole !== 'admin' && sessionUserId) {
      fetch(`${API_BASE}/api/staff/${encodeURIComponent(sessionUserId)}/dashboard`)
        .then((response) => response.ok ? response.json() : Promise.reject())
        .then((data) => setStaffDashboard(data))
        .catch(() => setStaffDashboard({ assigned_count: 0, pending_count: 0, in_progress_count: 0, approved_count: 0, rejected_count: 0, completed_count: 0 }));
    }

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

  const loadReviewQueue = (offset = 0) => {
    setReviewQueueLoading(true);
    const params = new URLSearchParams({ limit: '10', offset: String(offset) });
    if (sessionRole !== 'admin') params.set('assignee_id', sessionUserId);
    fetch(`${API_BASE}/api/qc/review-queue?${params.toString()}`)
      .then((response) => response.json())
      .then((data) => {
        const loadedIssues = data.issues || [];
        setIssues(loadedIssues);
        setReviewQueueCount(Number(data.count || 0));
        setReviewQueueOffset(offset);
        setSelectedIssue(null);
      })
      .catch(() => {
        setIssues([]);
        setReviewQueueCount(0);
        setReviewQueueOffset(0);
        setSelectedIssue(null);
      })
      .finally(() => setReviewQueueLoading(false));
  };

  const loadQualityOverview = () => {
    setQualityLoading(true);
    fetch(`${API_BASE}/api/qc/quality-overview`)
      .then((response) => response.ok ? response.json() : Promise.reject())
      .then((data) => setQualityOverview(data))
      .catch(() => setQualityOverview({ entities: {}, scoring_note: '' }))
      .finally(() => setQualityLoading(false));
  };

  const loadCleanAnalysis = (questionId = '') => {
    setCleanAnalysisLoading(true);
    const params = new URLSearchParams({ category: 'noodles' });
    if (questionId) params.set('question_id', questionId);
    fetch(`${API_BASE}/api/analytics/clean-tables?${params.toString()}`)
      .then((response) => response.ok ? response.json() : Promise.reject())
      .then((data) => {
        setCleanAnalysisTables(data);
        setCleanAnalysisQuestionId(data.question_id || data.tables?.[0]?.id || '');
      })
      .catch(() => setCleanAnalysisTables({ respondent_count: 0, tables: [], questions: [], question_id: null }))
      .finally(() => setCleanAnalysisLoading(false));
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

    if (showRawBrowser || page === 'raw') loadRawDataPage(0, false, '', rawDataPageSize);
  };

  useEffect(() => {
    loadHomeData();
  }, [sessionRole, sessionUserId]);

  useEffect(() => {
    if (page === 'admin' || page === 'raw' || page === 'staff' || page === 'staff-registration' || page === 'staff-list') {
      loadAdminData();
    }
  }, [page, showRawBrowser]);

  useEffect(() => {
    if (page !== 'admin') return undefined;

    // Imports can finish in the background, so keep the administrator's
    // survey-quality totals current even when no button has been clicked.
    const refreshDashboard = () => {
      fetch(`${API_BASE}/api/admin/dashboard`)
        .then((response) => response.ok ? response.json() : Promise.reject())
        .then((data) => setDashboard(data))
        .catch(() => {});
    };
    const intervalId = setInterval(refreshDashboard, 15000);
    return () => clearInterval(intervalId);
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
    if (page === 'quality') loadQualityOverview();
  }, [page]);

  useEffect(() => {
    if (page === 'clean-analysis') loadCleanAnalysis();
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
    setStaffSuccess('');
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
        setNewStaff({ username: '', email: '', password: '', role: 'reviewer' });
        setStaffSuccess(`${created.username} has been registered successfully.`);
        setStaffListPage(0);
      })
      .catch(() => {
        setAdminError('Unable to add staff. Check the email and try again.');
      });
  };

  const deleteStaff = (staff) => {
    if (!window.confirm(`Remove ${staff.username}'s staff account? Their assigned issues will become unassigned.`)) return;
    fetch(`${API_BASE}/api/admin/staff/${encodeURIComponent(staff.staff_id)}`, { method: 'DELETE' })
      .then((response) => response.ok ? response : response.json().then((body) => Promise.reject(new Error(body.detail || 'Unable to remove staff'))))
      .then(() => { setStaffMembers((current) => current.filter((member) => member.staff_id !== staff.staff_id)); setStaffSuccess(`${staff.username}'s account was removed.`); })
      .catch((error) => setAdminError(error.message));
  };

  const changePassword = (event) => {
    event.preventDefault();
    setAccountMessage('');
    if (passwordChange.new_password !== passwordChange.confirm_password) { setAccountMessage('New passwords do not match.'); return; }
    fetch(`${API_BASE}/api/staff/${encodeURIComponent(sessionUserId)}/password`, { method: 'PUT', headers: { 'Content-Type': 'application/json' }, body: JSON.stringify(passwordChange) })
      .then(async (response) => { if (response.ok) return; const body = await response.json().catch(() => ({})); throw new Error(body.detail || 'Unable to change password'); })
      .then(() => { setPasswordChange({ current_password: '', new_password: '', confirm_password: '' }); setAccountMessage('Password updated successfully.'); })
      .catch((error) => setAccountMessage(error.message));
  };

  const assignIssue = (issueId, staffId, assignmentRemark = '') => {
    fetch(`${API_BASE}/api/qc/issues/${issueId}/assignment`, {
      method: 'PUT',
      headers: { 'Content-Type': 'application/json' },
      body: JSON.stringify({ staff_id: staffId || null, assignment_remark: assignmentRemark || null }),
    })
      .then(async (response) => {
        if (response.ok) return response.json();
        const body = await response.json().catch(() => ({}));
        throw new Error(body.detail || 'Unable to save assignment');
      })
      .then((updated) => {
        setIssues((current) => current.map((issue) => issue.issue_id === updated.issue_id ? updated : issue));
        setSelectedIssue(updated);
      })
      .catch((error) => setAdminError(error.message));
  };

  const updateIssueStatus = (status) => {
    if (!selectedIssue) return;
    fetch(`${API_BASE}/api/qc/issues/${selectedIssue.issue_id}/action`, {
      method: 'POST', headers: { 'Content-Type': 'application/json' }, body: JSON.stringify({ status, resolution_note: selectedIssue.resolution_note || null }),
    }).then((response) => response.ok ? response.json() : Promise.reject()).then((updated) => {
      setIssues((current) => current.map((issue) => issue.issue_id === updated.issue_id ? { ...issue, ...updated } : issue));
      setSelectedIssue((current) => ({ ...current, ...updated }));
      if (sessionRole === 'admin') loadAdminData(); else loadHomeData();
    });
  };

  const runMainSurveyQC = () => {
    fetch(`${API_BASE}/api/qc/run-main-survey`, { method: 'POST' })
      .then((response) => response.ok ? response.json() : Promise.reject())
      .then(() => { loadHomeData(); loadAdminData(); });
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

  const activePageTitle = {
    admin: 'Admin Dashboard',
    home: 'Review Outliers',
    quality: 'Demographic / Screener Quality',
    insights: 'Unreviewed Insights',
    'clean-analysis': 'Reviewed Insights',
    'staff-dashboard': 'My Dashboard',
    'staff-registration': 'Staff Registration',
    'staff-list': 'QC Staff Directory',
    raw: 'Raw SurveyCTO Data',
    decoded: 'Decoded Questions',
  }[page] || 'QC Hub';

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

  const openRawSubmission = (submissionKey) => {
    setRawDataSearch(submissionKey);
    setPage('raw');
    loadRawDataPage(0, false, submissionKey, rawDataPageSize);
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
  const insightQuestionsPerPage = 8;
  const visibleInsightQuestions = useMemo(() => (analysisTables.questions || []).slice(insightQuestionPage * insightQuestionsPerPage, (insightQuestionPage + 1) * insightQuestionsPerPage), [analysisTables.questions, insightQuestionPage]);
  const insightQuestionPageCount = Math.max(1, Math.ceil((analysisTables.questions || []).length / insightQuestionsPerPage));
  const visibleCleanQuestions = useMemo(() => (cleanAnalysisTables.questions || []).slice(cleanQuestionPage * 8, (cleanQuestionPage + 1) * 8), [cleanAnalysisTables.questions, cleanQuestionPage]);
  const cleanQuestionPageCount = Math.max(1, Math.ceil((cleanAnalysisTables.questions || []).length / 8));
  const staffPerPage = 8;
  const visibleStaffMembers = useMemo(() => staffMembers.slice(staffListPage * staffPerPage, (staffListPage + 1) * staffPerPage), [staffMembers, staffListPage]);
  const staffPageCount = Math.max(1, Math.ceil(staffMembers.length / staffPerPage));

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

  if (!sessionRole) {
    return (
      <main className="login-shell" style={{ minHeight: '100vh', display: 'grid', placeItems: 'center', padding: 24, fontFamily: 'Arial, sans-serif', background: 'radial-gradient(circle at 12% 15%, rgba(45,212,191,.28), transparent 24%), radial-gradient(circle at 88% 78%, rgba(56,189,248,.23), transparent 28%), linear-gradient(135deg, #031126, #0b3b79)' }}>
        <section style={{ width: 'min(960px, 100%)', display: 'grid', gridTemplateColumns: '1.15fr .85fr', overflow: 'hidden', borderRadius: 28, background: '#fff', boxShadow: '0 28px 80px rgba(2, 18, 48, .35)', animation: 'fadeUp .5s ease both' }}>
          <div style={{ padding: '56px 48px', color: '#fff', position: 'relative', overflow: 'hidden', background: 'radial-gradient(circle at 80% 15%, rgba(45,212,191,.35), transparent 28%), linear-gradient(145deg, #071b3e, #1266bb)' }}>
            <div className="data-grid-overlay" />
            <img src={inicioLogo} alt="Inicio" style={{ width: 158, height: 'auto', position: 'relative', marginBottom: 24 }} />
            <div style={{ fontWeight: 800, letterSpacing: '.16em', fontSize: 12, position: 'relative' }}>QC HUB</div>
            <h1 style={{ fontSize: 42, lineHeight: 1.08, margin: '20px 0' }}>Quality control that makes every survey defensible.</h1>
            <p style={{ lineHeight: 1.65, opacity: .9 }}>Review evidence-led outliers, assign work, and keep the complete audit trail in one place.</p>
          </div>
          <form onSubmit={(event) => { event.preventDefault(); setLoginError(''); fetch(`${API_BASE}/api/auth/login`, { method: 'POST', headers: { 'Content-Type': 'application/json' }, body: JSON.stringify({ username: loginName, password: loginPassword }) }).then(async (response) => { if (!response.ok) throw new Error((await response.json()).detail || 'Unable to sign in'); return response.json(); }).then((account) => { setSessionRole(account.role); setSessionUserId(account.staff_id); setLoginName(account.username); setPage(account.role === 'admin' ? 'admin' : 'staff-dashboard'); }).catch((error) => setLoginError(error.message)); }} style={{ padding: 48, display: 'grid', alignContent: 'center', gap: 16 }}>
            <div><div style={{ color: '#2563eb', fontWeight: 800, fontSize: 12, letterSpacing: '.12em' }}>WELCOME BACK</div><h2 style={{ fontSize: 28, margin: '8px 0' }}>Sign in to your workspace</h2></div>
            <label style={{ display: 'grid', gap: 7, color: '#475569', fontWeight: 700, fontSize: 13 }}>Username or email<input required value={loginName} onChange={(event) => setLoginName(event.target.value)} placeholder="Username or email" style={{ padding: '13px 14px', border: '1px solid #cbd5e1', borderRadius: 10 }} /></label>
            <label style={{ display: 'grid', gap: 7, color: '#475569', fontWeight: 700, fontSize: 13 }}>Password<input required type="password" value={loginPassword} onChange={(event) => setLoginPassword(event.target.value)} placeholder="Your password" style={{ padding: '13px 14px', border: '1px solid #cbd5e1', borderRadius: 10 }} /></label>
            <button style={{ padding: 14, border: 0, borderRadius: 10, background: '#1368ce', color: '#fff', fontWeight: 800, cursor: 'pointer' }}>Sign in</button>
            {loginError && <div style={{ color: '#b91c1c', fontWeight: 700, fontSize: 13 }}>{loginError}</div>}
          </form>
        </section>
        <style>{'@keyframes fadeUp { from { opacity:0; transform: translateY(14px) } to { opacity:1; transform: translateY(0) } }'}</style>
      </main>
    );
  }

  return (
    <div className="app-shell" style={{ fontFamily: 'Arial, sans-serif', background: 'radial-gradient(circle at 92% 4%, rgba(56,189,248,.16), transparent 23%), radial-gradient(circle at 8% 30%, rgba(45,212,191,.12), transparent 21%), #f4f8fc', minHeight: '100vh', color: '#111827' }}>
      <div className="data-orb orb-one" /><div className="data-orb orb-two" />
      <div style={{ maxWidth: 1280, margin: '0 auto', padding: 24, position: 'relative', zIndex: 1 }}>
        <div className="workspace-nav" style={{ display: 'flex', justifyContent: 'space-between', alignItems: 'center', marginBottom: 24 }}>
          <div style={{ display: 'flex', gap: 10, flexWrap: 'wrap', alignItems: 'center' }}>
            <div className="brand-lockup" style={{ marginRight: 8 }}><img src={inicioLogo} alt="Inicio" /><span>QC Hub</span></div>
            {sessionRole !== 'admin' && <button
              onClick={() => setPage('quality')}
              style={{
                padding: '12px 18px', marginRight: 10, borderRadius: 12,
                border: page === 'quality' ? '2px solid #2563eb' : '1px solid #d1d5db',
                background: page === 'quality' ? '#eff6ff' : '#ffffff', cursor: 'pointer', fontWeight: 700,
              }}
            >
              Demographic / Screener Quality
            </button>}
            {sessionRole !== 'admin' && <button
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
              Unreviewed Insights
            </button>}
            {sessionRole === 'admin' && <button
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
              Admin Dashboard
            </button>}
          </div>
          {sessionRole === 'admin' && <div style={{ display: 'flex', gap: 10, alignItems: 'center' }}>
            <span style={{ color: '#64748b', fontSize: 13, fontWeight: 700 }}>{sessionRole === 'admin' ? 'Administrator' : 'Staff reviewer'} {loginName ? `· ${loginName}` : ''}</span>
            {page === 'admin' && <button
              onClick={handleSync}
              style={{ padding: '12px 18px', borderRadius: 12, border: 'none', background: 'linear-gradient(135deg, #0ea5e9, #2563eb)', color: '#ffffff', cursor: 'pointer', fontWeight: 800, boxShadow: '0 8px 18px rgba(37,99,235,.22)' }}
            >
              Sync Data
            </button>}
            {page === 'admin' && syncStatus && (
              <div style={{ color: syncStatus.startsWith('Sync failed') ? '#b91c1c' : '#1f2937', fontSize: 14, fontWeight: 700 }}>
                {syncStatus}
              </div>
            )}
            <button onClick={() => { setSessionRole(''); setSessionUserId(''); setLoginName(''); }} style={{ padding: '10px 12px', borderRadius: 10, border: '1px solid #cbd5e1', background: '#fff', color: '#334155', cursor: 'pointer', fontWeight: 700 }}>Sign out</button>
          </div>}
          {sessionRole !== 'admin' && <button onClick={() => { setSessionRole(''); setSessionUserId(''); setLoginName(''); }} style={{ padding: '10px 12px', borderRadius: 10, border: '1px solid #cbd5e1', background: '#fff', color: '#334155', cursor: 'pointer', fontWeight: 700 }}>Sign out</button>}
        </div>

        <div style={{ margin: '-8px 0 20px', color: '#0f3e76', fontWeight: 800, fontSize: 14, letterSpacing: '.04em' }}>
          {activePageTitle}
        </div>

        {(insightsLoading || qualityLoading || cleanAnalysisLoading || rawDataLoading || decodedLoading || reviewQueueLoading) && <div style={{ position: 'fixed', zIndex: 200, right: 24, bottom: 24, display: 'flex', alignItems: 'center', gap: 12, padding: '12px 16px', borderRadius: 16, background: 'rgba(15, 42, 87, .96)', color: '#fff', boxShadow: '0 16px 34px rgba(15,23,42,.28)', animation: 'fadeUp .25s ease both' }}><div style={{ width: 28, height: 34, border: '2px solid #bfdbfe', borderRadius: '5px 5px 9px 9px', overflow: 'hidden', position: 'relative' }}><div style={{ position: 'absolute', left: 0, right: 0, bottom: 0, height: '65%', background: 'linear-gradient(#7dd3fc, #2563eb)', animation: 'waterFill 1.1s ease-in-out infinite alternate' }} /></div><div><div style={{ fontSize: 13, fontWeight: 800 }}>Refreshing data…</div><div style={{ marginTop: 2, color: '#bfdbfe', fontSize: 12 }}>Please wait while your workspace updates.</div></div></div>}
        <style>{`@keyframes waterFill { from { height: 22%; } to { height: 82%; } }
          @keyframes dataDrift { 0%,100% { transform:translate3d(0,0,0) } 50% { transform:translate3d(18px,-14px,0) } }
          .app-shell { position:relative; overflow:hidden; }
          .app-shell::before { content:''; position:absolute; inset:0; opacity:.34; pointer-events:none; background-image:linear-gradient(rgba(30,64,175,.07) 1px, transparent 1px),linear-gradient(90deg, rgba(30,64,175,.07) 1px, transparent 1px); background-size:32px 32px; mask-image:linear-gradient(to bottom, #000, transparent 72%); }
          .data-orb { position:fixed; z-index:0; width:280px; height:280px; border-radius:50%; pointer-events:none; filter:blur(2px); opacity:.5; animation:dataDrift 11s ease-in-out infinite; }
          .orb-one { top:110px; right:-130px; background:radial-gradient(circle, rgba(14,165,233,.22), transparent 68%); }
          .orb-two { bottom:20px; left:-130px; background:radial-gradient(circle, rgba(20,184,166,.16), transparent 68%); animation-delay:-5s; }
          .workspace-nav { padding:10px 12px; border:1px solid rgba(191,219,254,.8); border-radius:18px; background:rgba(255,255,255,.8); box-shadow:0 12px 34px rgba(15,23,42,.08); backdrop-filter:blur(14px); }
          .brand-lockup { display:flex; align-items:center; gap:10px; min-height:46px; padding:5px 12px 5px 8px; border-radius:12px; color:#fff; background:linear-gradient(135deg, #071b3e, #1457a6); box-shadow:0 8px 18px rgba(15,42,87,.18); font-size:14px; font-weight:800; letter-spacing:.03em; }
          .brand-lockup img { width:64px; height:34px; object-fit:contain; object-position:left center; }
          .app-shell button { transition:transform .16s ease, box-shadow .16s ease, filter .16s ease; }
          .app-shell button:hover:not(:disabled) { transform:translateY(-1px); filter:saturate(1.08); }
          @media (max-width: 760px) { .workspace-nav { align-items:flex-start !important; gap:12px; flex-direction:column; } .workspace-nav > div:last-child { flex-wrap:wrap; } .app-shell [style*="repeat(4, minmax(0, 1fr))"] { grid-template-columns:repeat(2, minmax(0, 1fr)) !important; } }
        `}</style>

        {sessionRole === 'admin' && <aside
          onMouseEnter={() => setAdminSidebarOpen(true)}
          onMouseLeave={() => setAdminSidebarOpen(false)}
          style={{ position: 'fixed', zIndex: 20, top: 92, left: 0, width: adminSidebarOpen ? 230 : 54, overflow: 'hidden', borderRadius: '0 14px 14px 0', background: '#0f2a57', boxShadow: '0 8px 24px rgba(15, 42, 87, .25)', transition: 'width .18s ease' }}
        >
          <div style={{ padding: '14px 17px', color: '#bfdbfe', fontSize: 20, fontWeight: 800 }}>☰</div>
          {[['admin', 'Admin dashboard'], ['home', 'Review outliers'], ['raw', 'Raw SurveyCTO pulls'], ['quality', 'Demographic / screener quality'], ['insights', 'Unreviewed insights'], ['clean-analysis', 'Reviewed insights']].map(([target, label]) => <button key={target} onClick={() => setPage(target)} title={label} style={{ display: 'block', width: '100%', padding: '12px 17px', border: 0, borderLeft: page === target ? '3px solid #93c5fd' : '3px solid transparent', background: page === target ? '#1d4ed8' : 'transparent', color: '#fff', cursor: 'pointer', fontWeight: 700, whiteSpace: 'nowrap', textAlign: 'left' }}>{adminSidebarOpen ? label : label.charAt(0)}</button>)}
          <div onMouseEnter={() => setStaffMenuOpen(true)} onMouseLeave={() => setStaffMenuOpen(false)}><button onClick={() => { setPage('staff-registration'); setStaffMenuOpen(true); }} title="Staff accounts" style={{ display: 'block', width: '100%', padding: '12px 17px', border: 0, borderLeft: ['staff-registration', 'staff-list'].includes(page) ? '3px solid #93c5fd' : '3px solid transparent', background: ['staff-registration', 'staff-list'].includes(page) ? '#1d4ed8' : 'transparent', color: '#fff', cursor: 'pointer', fontWeight: 700, whiteSpace: 'nowrap', textAlign: 'left' }}>{adminSidebarOpen ? 'Staff accounts ›' : 'S'}</button>{adminSidebarOpen && staffMenuOpen && <div style={{ padding: '0 10px 10px 25px', display: 'grid', gap: 5, background: 'rgba(2, 23, 55, .25)' }}><button onClick={() => setPage('staff-registration')} style={{ padding: '8px 9px', border: 0, borderRadius: 8, background: 'rgba(255,255,255,.12)', color: '#dbeafe', textAlign: 'left', cursor: 'pointer', fontSize: 12, fontWeight: 700 }}>Staff registration</button><button onClick={() => setPage('staff-list')} style={{ padding: '8px 9px', border: 0, borderRadius: 8, background: 'rgba(255,255,255,.12)', color: '#dbeafe', textAlign: 'left', cursor: 'pointer', fontSize: 12, fontWeight: 700 }}>List of QC staff</button></div>}</div>
        </aside>}

        {sessionRole !== 'admin' && <aside
          onMouseEnter={() => setAdminSidebarOpen(true)}
          onMouseLeave={() => setAdminSidebarOpen(false)}
          style={{ position: 'fixed', zIndex: 20, top: 92, left: 0, width: adminSidebarOpen ? 238 : 58, overflow: 'hidden', borderRadius: '0 16px 16px 0', background: 'linear-gradient(180deg, #102a56, #173f7a)', boxShadow: '0 12px 30px rgba(15, 42, 87, .28)', transition: 'width .18s ease' }}
        >
          <div style={{ padding: '15px 19px', color: '#bfdbfe', fontSize: 18, fontWeight: 800 }}>✦</div>
          {[['staff-dashboard', 'My dashboard'], ['quality', 'Demographic / screener quality'], ['insights', 'Unreviewed insights']].map(([target, label]) => <button key={target} onClick={() => setPage(target)} title={label} style={{ display: 'block', width: '100%', padding: '13px 18px', border: 0, borderLeft: page === target ? '3px solid #7dd3fc' : '3px solid transparent', background: page === target ? 'rgba(59,130,246,.35)' : 'transparent', color: '#fff', cursor: 'pointer', fontWeight: 700, whiteSpace: 'nowrap', textAlign: 'left' }}>{adminSidebarOpen ? label : label.charAt(0)}</button>)}
        </aside>}

        {page === 'staff-dashboard' && sessionRole !== 'admin' ? (
          <section style={{ display: 'grid', gap: 20, marginBottom: 24 }}>
            <div style={{ padding: '28px 30px', borderRadius: 22, color: '#fff', background: 'radial-gradient(circle at 85% 5%, rgba(125,211,252,.38), transparent 26%), linear-gradient(120deg, #102a56, #2563b8)' }}>
              <div style={{ fontSize: 12, fontWeight: 800, letterSpacing: '.16em', color: '#bae6fd' }}>REVIEWER WORKSPACE</div>
              <h1 style={{ margin: '8px 0', fontSize: 32 }}>Good to see you, {loginName}.</h1>
              <p style={{ margin: 0, opacity: .88 }}>Your queue is focused on the outliers assigned to you—clear decisions, useful notes, and no duplicate ownership.</p>
            </div>
            <div style={{ display: 'grid', gridTemplateColumns: 'repeat(4, minmax(0, 1fr))', gap: 14 }}>
              {[
                ['Assigned to me', staffDashboard.assigned_count, '#e0f2fe', '#075985'],
                ['Ready to review', staffDashboard.pending_count, '#fef3c7', '#92400e'],
                ['In investigation', staffDashboard.in_progress_count, '#ede9fe', '#5b21b6'],
                ['Decisions made', staffDashboard.completed_count, '#dcfce7', '#166534'],
              ].map(([label, value, bg, color]) => <div key={label} style={{ padding: 18, borderRadius: 18, background: bg, color }}><div style={{ fontSize: 12, fontWeight: 800, letterSpacing: '.08em', textTransform: 'uppercase' }}>{label}</div><div style={{ fontSize: 34, fontWeight: 800, marginTop: 8 }}>{Number(value).toLocaleString()}</div></div>)}
            </div>
            <div style={{ display: 'flex', justifyContent: 'space-between', alignItems: 'center', borderRadius: 16, padding: 18, background: '#fff', boxShadow: '0 8px 28px rgba(15,23,42,.06)' }}><div><strong>Next up: your assigned outliers</strong><div style={{ marginTop: 4, color: '#64748b', fontSize: 14 }}>Review evidence and leave a decision note for the admin.</div></div><button onClick={() => setPage('home')} style={{ padding: '10px 14px', border: 0, borderRadius: 10, background: '#2563eb', color: '#fff', cursor: 'pointer', fontWeight: 800 }}>Open my queue</button></div>
            <form onSubmit={changePassword} style={{ maxWidth: 560, display: 'grid', gap: 12, padding: 20, borderRadius: 16, background: '#fff', boxShadow: '0 8px 28px rgba(15,23,42,.06)' }}><div><div style={{ color: '#2563eb', fontSize: 12, fontWeight: 800, letterSpacing: '.1em' }}>ACCOUNT SECURITY</div><h2 style={{ margin: '5px 0 0', fontSize: 20 }}>Change password</h2></div><input required type="password" value={passwordChange.current_password} onChange={(event) => setPasswordChange((current) => ({ ...current, current_password: event.target.value }))} placeholder="Current password" style={{ padding: '11px 12px', borderRadius: 10, border: '1px solid #cbd5e1' }} /><input required minLength={6} type="password" value={passwordChange.new_password} onChange={(event) => setPasswordChange((current) => ({ ...current, new_password: event.target.value }))} placeholder="New password (minimum 6 characters)" style={{ padding: '11px 12px', borderRadius: 10, border: '1px solid #cbd5e1' }} /><input required minLength={6} type="password" value={passwordChange.confirm_password} onChange={(event) => setPasswordChange((current) => ({ ...current, confirm_password: event.target.value }))} placeholder="Confirm new password" style={{ padding: '11px 12px', borderRadius: 10, border: '1px solid #cbd5e1' }} /><button style={{ justifySelf: 'start', padding: '10px 14px', border: 0, borderRadius: 10, background: '#0f766e', color: '#fff', cursor: 'pointer', fontWeight: 800 }}>Update password</button>{accountMessage && <div style={{ color: accountMessage.includes('success') ? '#166534' : '#b91c1c', fontWeight: 700, fontSize: 13 }}>{accountMessage}</div>}</form>
          </section>
        ) : page === 'clean-analysis' ? (
          <section style={{ display: 'grid', gridTemplateColumns: '260px minmax(0, 1fr)', gap: 18, marginBottom: 24 }}>
            <div style={{ background: '#fff', borderRadius: 16, padding: 16, boxShadow: '0 8px 28px rgba(15, 23, 42, .06)' }}><div style={{ color: '#15803d', fontSize: 12, fontWeight: 800, letterSpacing: '.1em' }}>QC-CLEARED DATA</div><h2 style={{ margin: '8px 0' }}>{cleanAnalysisTables.respondent_count.toLocaleString()} eligible submissions</h2><p style={{ color: '#64748b', fontSize: 13 }}>Only records with no active red flags are included. Resolved or cleared outliers enter automatically.</p><div style={{ display: 'grid', gap: 8 }}>{visibleCleanQuestions.map((question, index) => <button key={question.id} onClick={() => loadCleanAnalysis(question.id)} style={{ padding: '11px 10px', border: question.id === cleanAnalysisQuestionId ? '1px solid #86efac' : '1px solid transparent', borderRadius: 10, textAlign: 'left', cursor: 'pointer', background: question.id === cleanAnalysisQuestionId ? 'linear-gradient(135deg, #dcfce7, #f0fdf4)' : '#f8fafc', color: '#14532d', fontWeight: question.id === cleanAnalysisQuestionId ? 800 : 600, boxShadow: question.id === cleanAnalysisQuestionId ? '0 5px 14px rgba(22,163,74,.12)' : 'none' }}><span style={{ display: 'inline-flex', width: 20, height: 20, alignItems: 'center', justifyContent: 'center', borderRadius: 999, marginRight: 7, background: '#bbf7d0', fontSize: 11 }}>{cleanQuestionPage * 8 + index + 1}</span>{question.label}</button>)}</div><div style={{ display: 'flex', justifyContent: 'space-between', alignItems: 'center', marginTop: 13 }}><button onClick={() => setCleanQuestionPage((page) => Math.max(0, page - 1))} disabled={cleanQuestionPage === 0} style={{ padding: '7px 10px', borderRadius: 8, border: '1px solid #bbf7d0', background: '#fff', opacity: cleanQuestionPage ? 1 : .45 }}>Previous</button><span style={{ fontSize: 12, color: '#64748b' }}>{cleanQuestionPage + 1}/{cleanQuestionPageCount}</span><button onClick={() => setCleanQuestionPage((page) => Math.min(cleanQuestionPageCount - 1, page + 1))} disabled={cleanQuestionPage >= cleanQuestionPageCount - 1} style={{ padding: '7px 10px', borderRadius: 8, border: 0, background: '#16a34a', color: '#fff', opacity: cleanQuestionPage < cleanQuestionPageCount - 1 ? 1 : .45 }}>Next</button></div></div>
            <div style={{ background: '#fff', borderRadius: 16, padding: 20, boxShadow: '0 8px 28px rgba(15, 23, 42, .06)' }}>{(() => { const table = cleanAnalysisTables.tables?.find((item) => item.id === cleanAnalysisQuestionId) || cleanAnalysisTables.tables?.[0]; return <><div style={{ color: '#15803d', fontSize: 12, fontWeight: 800, letterSpacing: '.1em' }}>CLEAN DATA ANALYSIS</div><h1 style={{ margin: '6px 0 16px' }}>{table?.title || 'Select a question'}</h1>{table ? <table style={{ width: '100%', borderCollapse: 'collapse' }}><thead><tr style={{ textAlign: 'left', background: '#f0fdf4' }}><th style={{ padding: 11 }}>Response</th><th style={{ padding: 11, textAlign: 'right' }}>N</th><th style={{ padding: 11, textAlign: 'right' }}>%</th></tr></thead><tbody>{table.rows.map((row) => <tr key={row.label} style={{ borderTop: '1px solid #e5e7eb' }}><td style={{ padding: 11 }}>{row.label}</td><td style={{ padding: 11, textAlign: 'right' }}>{row.count.toLocaleString()}</td><td style={{ padding: 11, textAlign: 'right' }}>{row.pct}%</td></tr>)}</tbody></table> : <p style={{ color: '#64748b' }}>No QC-cleared data is available yet.</p>}</>; })()}</div>
          </section>
        ) : (page === 'staff-registration' || page === 'staff-list') ? (<><style>{page === 'staff-registration' ? 'section[style*="max-width: 780px"] { display: none !important; }' : 'section[style*="max-width: 680px"] { display: none !important; }'}</style>
          <section style={{ maxWidth: 680, background: '#fff', borderRadius: 18, padding: 24, boxShadow: '0 8px 28px rgba(15, 23, 42, .06)', marginBottom: 24 }}><div style={{ color: '#2563eb', fontSize: 12, letterSpacing: '.12em', fontWeight: 800 }}>ADMIN ONLY</div><h1 style={{ margin: '6px 0 18px' }}>Staff registration</h1><form onSubmit={handleCreateStaff} style={{ display: 'grid', gap: 14 }}><input required value={newStaff.username} onChange={(e) => setNewStaff({ ...newStaff, username: e.target.value })} placeholder="Name" style={{ padding: '12px 14px', borderRadius: 12, border: '1px solid #d1d5db' }} /><input required value={newStaff.email} onChange={(e) => setNewStaff({ ...newStaff, email: e.target.value })} placeholder="Email" type="email" style={{ padding: '12px 14px', borderRadius: 12, border: '1px solid #d1d5db' }} /><input required value={newStaff.password} onChange={(e) => setNewStaff({ ...newStaff, password: e.target.value })} placeholder="Temporary password (minimum 6 characters)" type="password" minLength={6} style={{ padding: '12px 14px', borderRadius: 12, border: '1px solid #d1d5db' }} /><select value={newStaff.role} onChange={(e) => setNewStaff({ ...newStaff, role: e.target.value })} style={{ padding: '12px 14px', borderRadius: 12, border: '1px solid #d1d5db' }}><option value="reviewer">Reviewer</option><option value="admin">Admin</option></select><button style={{ padding: '12px 16px', borderRadius: 12, border: 0, background: '#10b981', color: '#fff', fontWeight: 700, cursor: 'pointer' }}>Register staff</button>{adminError && <div style={{ color: '#b91c1c' }}>{adminError}</div>}</form></section>
          <section style={{ maxWidth: 780, background: '#fff', borderRadius: 18, padding: 24, boxShadow: '0 8px 28px rgba(15, 23, 42, .06)', marginBottom: 24 }}><div style={{ display: 'flex', justifyContent: 'space-between', alignItems: 'center', marginBottom: 14 }}><div><div style={{ color: '#2563eb', fontSize: 12, letterSpacing: '.12em', fontWeight: 800 }}>STAFF DIRECTORY</div><h2 style={{ margin: '5px 0 0' }}>Registered staff</h2></div><span style={{ color: '#64748b' }}>{staffMembers.length} total</span></div>{staffSuccess && <div role="status" style={{ marginBottom: 14, padding: '11px 13px', borderRadius: 10, background: '#dcfce7', color: '#166534', fontWeight: 700 }}>{staffSuccess}</div>}<div style={{ display: 'grid', gap: 9 }}>{visibleStaffMembers.map((member) => <div key={member.staff_id} style={{ display: 'flex', justifyContent: 'space-between', gap: 12, padding: 13, borderRadius: 12, background: '#f8fafc', border: '1px solid #e2e8f0' }}><div><strong>{member.username}</strong><div style={{ marginTop: 3, color: '#64748b', fontSize: 13 }}>{member.email}</div></div><div style={{ display: 'flex', alignItems: 'center', gap: 8 }}><span style={{ padding: '5px 9px', borderRadius: 999, background: '#dbeafe', color: '#1d4ed8', fontSize: 12, fontWeight: 800 }}>{member.role}</span><button onClick={() => deleteStaff(member)} style={{ padding: '7px 9px', borderRadius: 8, border: '1px solid #fecaca', background: '#fff', color: '#b91c1c', cursor: 'pointer', fontWeight: 800 }}>Remove</button></div></div>)}</div><div style={{ display: 'flex', justifyContent: 'space-between', alignItems: 'center', marginTop: 16 }}><span style={{ fontSize: 13, color: '#64748b' }}>Page {staffListPage + 1} of {staffPageCount}</span><div style={{ display: 'flex', gap: 8 }}><button onClick={() => setStaffListPage((page) => Math.max(0, page - 1))} disabled={staffListPage === 0} style={{ padding: '8px 12px', borderRadius: 9, border: '1px solid #cbd5e1', background: '#fff', cursor: staffListPage ? 'pointer' : 'default', opacity: staffListPage ? 1 : .45 }}>Previous</button><button onClick={() => setStaffListPage((page) => Math.min(staffPageCount - 1, page + 1))} disabled={staffListPage >= staffPageCount - 1} style={{ padding: '8px 12px', borderRadius: 9, border: 0, background: '#2563eb', color: '#fff', cursor: staffListPage < staffPageCount - 1 ? 'pointer' : 'default', opacity: staffListPage < staffPageCount - 1 ? 1 : .45 }}>Next</button></div></div></section></>
        ) : page === 'quality' ? (
          <section style={{ display: 'grid', gap: 16, marginBottom: 24 }}>
            <div style={{ display: 'flex', justifyContent: 'space-between', alignItems: 'end', gap: 12, flexWrap: 'wrap' }}>
              <div><div style={{ color: '#0875b8', fontWeight: 800, fontSize: 13, letterSpacing: '0.14em', textTransform: 'uppercase' }}>Saved QC signals</div><h1 style={{ margin: '6px 0 0', fontSize: 30 }}>Data quality by fieldwork segment</h1><p style={{ margin: '6px 0 0', color: '#64748b', maxWidth: 760 }}>{qualityOverview.scoring_note}</p></div>
              <button onClick={loadQualityOverview} style={{ padding: '10px 14px', border: 'none', borderRadius: 10, background: '#216ee8', color: '#fff', cursor: 'pointer', fontWeight: 700 }}>Refresh scores</button>
            </div>
            <div style={{ display: 'flex', gap: 8, flexWrap: 'wrap' }}>{[['interviewers', 'Interviewers'], ['respondents', 'Respondents'], ['cities', 'Cities'], ['regions', 'Regions']].map(([key, label]) => <button key={key} onClick={() => setQualityGroup(key)} style={{ padding: '9px 12px', borderRadius: 9, border: qualityGroup === key ? '2px solid #2563eb' : '1px solid #cbd5e1', background: qualityGroup === key ? '#eff6ff' : '#fff', cursor: 'pointer', fontWeight: 700 }}>{label}</button>)}</div>
            <div style={{ overflowX: 'auto', background: '#fff', borderRadius: 16, boxShadow: '0 8px 28px rgba(15, 23, 42, 0.06)' }}>
              <table style={{ width: '100%', borderCollapse: 'collapse', minWidth: 760 }}><thead><tr style={{ background: '#f8fafc', textAlign: 'left', color: '#475569' }}><th style={{ padding: 13 }}>Name / segment</th><th style={{ padding: 13, textAlign: 'right' }}>Interviews</th><th style={{ padding: 13, textAlign: 'right' }}>Flagged</th><th style={{ padding: 13, textAlign: 'right' }}>High flags</th><th style={{ padding: 13, textAlign: 'right' }}>Flag rate</th><th style={{ padding: 13, textAlign: 'right' }}>Quality</th><th style={{ padding: 13 }}>Priority</th></tr></thead><tbody>{(qualityOverview.entities?.[qualityGroup] || []).map((item) => <tr key={item.name} style={{ borderTop: '1px solid #e2e8f0' }}><td style={{ padding: 13, fontWeight: 700 }}>{item.name}</td><td style={{ padding: 13, textAlign: 'right' }}>{item.interviews}</td><td style={{ padding: 13, textAlign: 'right' }}>{item.flagged_interviews}</td><td style={{ padding: 13, textAlign: 'right' }}>{item.high_flags}</td><td style={{ padding: 13, textAlign: 'right' }}>{item.flag_rate}%</td><td style={{ padding: 13, textAlign: 'right', fontWeight: 800, color: item.quality_score < 75 ? '#b91c1c' : item.quality_score < 90 ? '#b45309' : '#15803d' }}>{item.quality_score}/100</td><td style={{ padding: 13 }}>{item.quality_band}</td></tr>)}{!(qualityOverview.entities?.[qualityGroup] || []).length && <tr><td colSpan="7" style={{ padding: 22, color: '#64748b', textAlign: 'center' }}>No saved submissions are available yet.</td></tr>}</tbody></table>
            </div>
          </section>
        ) : page === 'insights' ? (
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
                {false && <select value={selectedAnalysisTableId} onChange={(event) => loadInsights(selectedAnalysisCut === 'Total' ? '' : selectedAnalysisCut, event.target.value)} style={{ width: '100%', padding: 10, borderRadius: 8, border: '1px solid #cbd5e1', background: '#fff', marginBottom: 12 }}>
                  {(analysisTables.questions || []).map((question) => <option key={question.id} value={question.id}>{question.label}</option>)}
                </select>}
                <div style={{ display: 'grid', gap: 8 }}>{visibleInsightQuestions.map((question, index) => <button key={question.id} onClick={() => loadInsights(selectedAnalysisCut === 'Total' ? '' : selectedAnalysisCut, question.id)} style={{ padding: '12px 12px', border: question.id === selectedAnalysisTableId ? '1px solid #93c5fd' : '1px solid transparent', borderRadius: 12, textAlign: 'left', cursor: 'pointer', background: question.id === selectedAnalysisTableId ? 'linear-gradient(135deg, #dbeafe, #eff6ff)' : '#fff', color: '#163c70', fontSize: 13, lineHeight: 1.35, fontWeight: question.id === selectedAnalysisTableId ? 800 : 600, boxShadow: question.id === selectedAnalysisTableId ? '0 6px 16px rgba(37,99,235,.12)' : '0 1px 2px rgba(15,23,42,.04)', transition: 'transform .15s ease, box-shadow .15s ease' }}><span style={{ display: 'inline-flex', width: 22, height: 22, alignItems: 'center', justifyContent: 'center', marginRight: 8, borderRadius: 999, background: question.id === selectedAnalysisTableId ? '#2563eb' : '#e2e8f0', color: question.id === selectedAnalysisTableId ? '#fff' : '#475569', fontSize: 11 }}>{insightQuestionPage * insightQuestionsPerPage + index + 1}</span>{question.label}</button>)}</div>
                <div style={{ display: 'flex', justifyContent: 'space-between', alignItems: 'center', gap: 8, marginTop: 14 }}><button onClick={() => setInsightQuestionPage((page) => Math.max(0, page - 1))} disabled={insightQuestionPage === 0} style={{ padding: '8px 11px', borderRadius: 9, border: '1px solid #cbd5e1', background: '#fff', cursor: insightQuestionPage ? 'pointer' : 'default', opacity: insightQuestionPage ? 1 : .45 }}>Previous</button><span style={{ color: '#64748b', fontSize: 12, fontWeight: 700 }}>{insightQuestionPage + 1} / {insightQuestionPageCount}</span><button onClick={() => setInsightQuestionPage((page) => Math.min(insightQuestionPageCount - 1, page + 1))} disabled={insightQuestionPage >= insightQuestionPageCount - 1} style={{ padding: '8px 11px', borderRadius: 9, border: 0, background: '#2563eb', color: '#fff', cursor: insightQuestionPage < insightQuestionPageCount - 1 ? 'pointer' : 'default', opacity: insightQuestionPage < insightQuestionPageCount - 1 ? 1 : .45 }}>Next</button></div>
                {false && <div style={{ marginTop: 14 }}>
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
              }</aside>
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
        ) : page === 'raw' ? (
          <section style={{ background: '#ffffff', borderRadius: 18, padding: 24, boxShadow: '0 8px 28px rgba(15, 23, 42, 0.06)' }}>
            <div style={{ display: 'flex', justifyContent: 'space-between', gap: 16, alignItems: 'end', marginBottom: 18, flexWrap: 'wrap' }}><div><div style={{ color: '#2563eb', fontSize: 12, letterSpacing: '.12em', fontWeight: 800 }}>ADMIN ONLY</div><h1 style={{ margin: '6px 0' }}>Raw SurveyCTO data</h1><p style={{ margin: 0, color: '#64748b' }}>Select a submission to inspect its collected fields and responses.</p></div><div style={{ color: '#64748b' }}>{rawDataTotalCount.toLocaleString()} submissions</div></div>
            <input value={rawDataSearch} onChange={(event) => { const next = event.target.value; setRawDataSearch(next); loadRawDataPage(0, false, next, rawDataPageSize); }} placeholder="Search a submission or field" style={{ width: 'min(520px, 100%)', boxSizing: 'border-box', padding: '12px 14px', borderRadius: 10, border: '1px solid #cbd5e1', marginBottom: 16 }} />
            <div style={{ display: 'grid', gap: 10 }}>{rawDataTable.rows.map((row, index) => <button key={`${row.submission_key || index}`} onClick={() => setSelectedRawRecord(row)} style={{ textAlign: 'left', display: 'flex', justifyContent: 'space-between', gap: 16, padding: 16, borderRadius: 12, border: '1px solid #e2e8f0', background: '#fff', cursor: 'pointer' }}><div><strong>{row.submission_key || `Submission ${index + 1}`}</strong><div style={{ color: '#64748b', fontSize: 13, marginTop: 5 }}>{row.username || row.Interviewer || 'Interviewer not recorded'} · {row.SubmissionDate || row.fetched_at || 'Date not recorded'}</div></div><span style={{ color: '#155dc4', fontWeight: 800 }}>Inspect →</span></button>)}</div>
            <div style={{ marginTop: 16, display: 'flex', justifyContent: 'space-between' }}><button onClick={() => loadRawDataPage(Math.max(0, rawDataOffset - rawDataPageSize), false, rawDataSearch, rawDataPageSize)} disabled={rawDataOffset === 0}>Previous</button><button onClick={() => loadRawDataPage(rawDataOffset + rawDataPageSize, false, rawDataSearch, rawDataPageSize)} disabled={!rawDataHasMore}>Next</button></div>
          </section>
        ) : page === 'admin' ? (
          <section style={{ display: 'grid', gridTemplateColumns: '1.2fr 0.8fr', gap: 24, marginBottom: 24 }}>
            <div style={{ background: '#ffffff', borderRadius: 18, padding: 24, boxShadow: '0 8px 28px rgba(15, 23, 42, 0.06)' }}>
              <div style={{ marginBottom: 18, paddingBottom: 16, borderBottom: '1px solid #dbeafe' }}><div style={{ fontSize: 12, fontWeight: 800, letterSpacing: '.14em', color: '#2563eb' }}>OPERATIONS OVERVIEW</div><h1 style={{ margin: '7px 0 0', fontSize: 30, color: '#0f2a57' }}>Admin Dashboard</h1><div style={{ marginTop: 6, color: '#64748b' }}>A focused view of data health and review activity.</div></div>
              <div style={{ marginBottom: 14, color: '#64748b', fontSize: 13 }}>Live SurveyCTO quality status — a survey is marked good when it has no active outlier. Resolved and cleared findings are removed from the outlier total.</div>
              <div style={{ display: 'grid', gridTemplateColumns: 'repeat(2, minmax(0, 1fr))', gap: 14, marginBottom: 24 }}>
                {[
                  { label: 'Total data pulled', value: dashboard?.raw_submission_count ?? 0, accent: '#1d4ed8' },
                  { label: 'High severity', value: dashboard?.high_severity_count ?? 0, accent: '#d97706' },
                  { label: 'Medium severity', value: dashboard?.medium_severity_count ?? 0, accent: '#f59e0b' },
                  { label: 'Total reviewed', value: dashboard?.total_reviewed_count ?? 0, accent: '#059669' },
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
              <div style={{ marginTop: 18, paddingTop: 18, borderTop: '1px solid #e2e8f0' }}>
                <div style={{ display: 'flex', justifyContent: 'space-between', gap: 12, alignItems: 'center' }}>
                  <div><strong>Outlier review</strong><div style={{ color: '#64748b', fontSize: 13, marginTop: 4 }}>Select any flagged submission to inspect its reason, evidence, and assignment.</div></div>
                  <button onClick={() => setPage('home')} style={{ padding: '9px 12px', border: 0, borderRadius: 9, background: '#dbeafe', color: '#155dc4', cursor: 'pointer', fontWeight: 800 }}>Open review queue</button>
                </div>
                <div style={{ display: 'grid', gap: 8, marginTop: 12, maxHeight: 190, overflow: 'auto' }}>
                  {issues.slice(0, 5).map((issue) => <button key={issue.issue_id} onClick={() => { setSelectedIssue(issue); setPage('home'); }} style={{ textAlign: 'left', padding: '10px 12px', border: '1px solid #e2e8f0', background: '#fff', borderRadius: 10, cursor: 'pointer' }}><strong style={{ color: '#b91c1c' }}>{issue.flag_name || 'QC flag'}</strong><span style={{ color: '#475569', fontSize: 13 }}> · {issue.evidence || issue.issue_summary}</span></button>)}
                </div>
              </div>
            </div>

            <div style={{ background: '#ffffff', borderRadius: 18, padding: 24, boxShadow: '0 8px 28px rgba(15, 23, 42, 0.06)' }}>
              <div style={{ display: 'flex', justifyContent: 'space-between', gap: 12, alignItems: 'center' }}><div><h2 style={{ margin: 0 }}>SurveyCTO raw data browser</h2><div style={{ color: '#64748b', fontSize: 13, marginTop: 5 }}>Kept out of the way until it is needed.</div></div><button onClick={() => setShowRawBrowser((open) => !open)} style={{ padding: '9px 12px', border: 0, borderRadius: 9, background: '#eff6ff', color: '#155dc4', cursor: 'pointer', fontWeight: 800 }}>{showRawBrowser ? 'Hide raw data' : 'View raw data'}</button></div>
              {showRawBrowser && <div style={{ animation: 'fadeUp .25s ease both' }}>
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
              </div>}
            </div>

            {false && <><div style={{ background: '#ffffff', borderRadius: 18, padding: 24, boxShadow: '0 8px 28px rgba(15, 23, 42, 0.06)' }}>
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
                <input
                  value={newStaff.password}
                  onChange={(e) => setNewStaff({ ...newStaff, password: e.target.value })}
                  placeholder="Temporary password (minimum 6 characters)"
                  type="password"
                  minLength={6}
                  required
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
            </div></>}
          </section>
        ) : (
          <>
            <section
          style={{
            display: 'block',
            gap: 24,
            alignItems: 'stretch',
            marginBottom: 24,
          }}
        >
          {false && <div
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
            {false && <div style={{ display: 'flex', gap: 12, flexWrap: 'wrap' }}>
              <button style={{ padding: '12px 16px', borderRadius: 12, border: 'none', background: '#ffffff', color: '#1d4ed8', fontWeight: 700, cursor: 'pointer' }}>
                Open Queue
              </button>
              <button style={{ padding: '12px 16px', borderRadius: 12, border: '1px solid rgba(255,255,255,0.45)', background: 'transparent', color: '#ffffff', fontWeight: 700, cursor: 'pointer' }}>
                View Workflow
              </button>
            </div>}
          </div>}

          <div style={{ display: 'grid', gridTemplateColumns: 'repeat(4, minmax(0, 1fr))', gap: 14 }}>
            {[
              { label: 'Total issues', value: dashboard?.issue_count ?? stats.total, accent: '#1d4ed8' },
              { label: 'High severity', value: dashboard?.high_severity_count ?? stats.highSeverity, accent: '#dc2626' },
              { label: 'Medium severity', value: dashboard?.medium_severity_count ?? stats.mediumSeverity, accent: '#f59e0b' },
              { label: 'Pending review', value: dashboard?.pending_review_count ?? stats.pending, accent: '#059669' },
            ].map((card) => (
              <div key={card.label} style={{ background: '#ffffff', borderRadius: 16, padding: 18, boxShadow: '0 8px 30px rgba(15, 23, 42, 0.08)' }}>
                <div style={{ fontSize: 13, fontWeight: 700, color: '#6b7280', marginBottom: 10 }}>{card.label}</div>
                <div style={{ fontSize: 34, fontWeight: 800, color: card.accent }}>{card.value}</div>
              </div>
            ))}
          </div>
        </section>

        {false && <section style={{ marginBottom: 24, display: 'grid', gridTemplateColumns: 'repeat(4, minmax(0, 1fr))', gap: 14 }}>
          {workflowSteps.map((step) => (
            <div key={step.title} style={{ background: '#ffffff', borderRadius: 16, padding: 18, boxShadow: '0 8px 28px rgba(15, 23, 42, 0.06)' }}>
              <div style={{ display: 'inline-flex', alignItems: 'center', gap: 8, padding: '6px 10px', borderRadius: 999, background: '#dbeafe', color: '#1d4ed8', fontSize: 12, fontWeight: 700 }}>
                {step.title}
              </div>
              <p style={{ marginTop: 12, color: '#4b5563', lineHeight: 1.5 }}>{step.description}</p>
            </div>
          ))}
        </section>}

        <section style={{ display: 'grid', gridTemplateColumns: '1fr 1.25fr', gap: 24 }}>
          {false && sessionRole === 'admin' && !selectedIssue && <div style={{ background: '#ffffff', borderRadius: 18, padding: 18, boxShadow: '0 8px 28px rgba(15, 23, 42, 0.06)' }}>
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
          </div>}

          <div style={{ background: '#ffffff', borderRadius: 18, padding: 18, boxShadow: '0 8px 28px rgba(15, 23, 42, 0.06)' }}>
            <div style={{ display: 'flex', justifyContent: 'space-between', alignItems: 'center', marginBottom: 16 }}>
              <h2 style={{ margin: 0 }}>Review Queue</h2>
              <div style={{ display: 'flex', alignItems: 'center', gap: 10 }}>{sessionRole === 'admin' && <button onClick={runMainSurveyQC} style={{ padding: '7px 10px', borderRadius: 8, border: 'none', background: '#2563eb', color: '#fff', cursor: 'pointer', fontWeight: 700 }}>Run QC checks</button>}<span style={{ color: '#6b7280', fontSize: 14 }}>Showing {issues.length.toLocaleString()} of {reviewQueueCount.toLocaleString()} items</span></div>
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
                <div style={{ display: 'flex', justifyContent: 'space-between', alignItems: 'center', gap: 10, paddingTop: 4 }}><span style={{ color: '#64748b', fontSize: 12 }}>Showing {reviewQueueCount ? reviewQueueOffset + 1 : 0}–{Math.min(reviewQueueOffset + issues.length, reviewQueueCount)} of {reviewQueueCount}</span><div style={{ display: 'flex', gap: 8 }}><button onClick={() => loadReviewQueue(Math.max(0, reviewQueueOffset - 10))} disabled={reviewQueueOffset === 0 || reviewQueueLoading} style={{ padding: '8px 10px', borderRadius: 8, border: '1px solid #cbd5e1', background: '#fff', cursor: reviewQueueOffset === 0 || reviewQueueLoading ? 'default' : 'pointer' }}>Previous</button><button onClick={() => loadReviewQueue(reviewQueueOffset + 10)} disabled={reviewQueueOffset + issues.length >= reviewQueueCount || reviewQueueLoading} style={{ padding: '8px 10px', borderRadius: 8, border: '1px solid #cbd5e1', background: '#fff', cursor: reviewQueueOffset + issues.length >= reviewQueueCount || reviewQueueLoading ? 'default' : 'pointer' }}>Next</button></div></div>
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
                <div style={{ background: '#f8fafc', borderRadius: 12, padding: 14 }}>
                  <div style={{ fontSize: 12, color: '#6b7280', marginBottom: 6 }}>What we found</div>
                  <div>{splitQCReport(selectedIssue.evidence).evidence || 'No evidence was recorded.'}</div>
                </div>
                {Object.keys(selectedIssue.context || {}).length > 0 && <div style={{ background: '#f8fafc', borderRadius: 12, padding: 14 }}>
                  <div style={{ fontSize: 12, color: '#6b7280', marginBottom: 10 }}>Interview context</div>
                  <div style={{ display: 'grid', gridTemplateColumns: 'repeat(2, minmax(0, 1fr))', gap: 10 }}>{Object.entries(selectedIssue.context).map(([label, value]) => <div key={label}><div style={{ fontSize: 11, color: '#64748b' }}>{label}</div>{label === 'Related submission keys' ? <div style={{ display: 'flex', flexWrap: 'wrap', gap: 6, marginTop: 5 }}>{String(value).split(',').map((key) => key.trim()).filter(Boolean).map((key) => <button key={key} onClick={() => openRawSubmission(key)} style={{ padding: '5px 7px', border: '1px solid #bfdbfe', borderRadius: 7, background: '#eff6ff', color: '#1d4ed8', cursor: 'pointer', fontSize: 11, fontWeight: 700 }}>{key}</button>)}</div> : <div style={{ fontWeight: 700, overflowWrap: 'anywhere' }}>{value}</div>}</div>)}</div>
                </div>}
                <div style={{ padding: 14, borderRadius: 12, background: '#eff6ff', color: '#1e3a5f', lineHeight: 1.55 }}>
                  <strong>Recommended next check</strong><div style={{ marginTop: 5 }}>{splitQCReport(selectedIssue.evidence).nextCheck || <>Confirm the evidence against the submission, check whether this is a legitimate field condition, then assign a reviewer or record the resolution. This flag is <strong>{selectedIssue.severity}</strong> severity.</>}</div>
                </div>
                <div style={{ display: 'grid', gap: 6 }}>
                  {sessionRole === 'admin' && <label htmlFor="issue-assignee" style={{ fontSize: 12, color: '#6b7280', fontWeight: 700 }}>Assign reviewer</label>}
                  {sessionRole === 'admin' && <select id="issue-assignee" value={selectedIssue.assigned_to_user_id || ''} onChange={(event) => assignIssue(selectedIssue.issue_id, event.target.value)} style={{ padding: '10px 12px', borderRadius: 10, border: '1px solid #cbd5e1', background: '#fff' }}>
                    <option value="">Unassigned</option>
                    {staffMembers.map((member) => <option key={member.staff_id} value={member.staff_id}>{member.username} — {member.role}</option>)}
                  </select>}
                  {sessionRole === 'admin' ? <>
                    <label htmlFor="assignment-remark" style={{ fontSize: 12, color: '#6b7280', fontWeight: 700, marginTop: 4 }}>Assignment remark (visible to assigned staff)</label>
                    <textarea id="assignment-remark" maxLength={1000} value={selectedIssue.assignment_remark || ''} onChange={(event) => setSelectedIssue((current) => ({ ...current, assignment_remark: event.target.value }))} placeholder="Add short context or instructions for this task" rows={3} style={{ padding: '10px 12px', borderRadius: 10, border: '1px solid #cbd5e1', resize: 'vertical', fontFamily: 'inherit' }} />
                    <button onClick={() => assignIssue(selectedIssue.issue_id, selectedIssue.assigned_to_user_id, selectedIssue.assignment_remark)} style={{ padding: '10px 12px', border: 0, borderRadius: 9, background: '#2563eb', color: '#fff', cursor: 'pointer', fontWeight: 800 }}>Save assignment and remark</button>
                  </> : <><div style={{ background: '#fff7ed', borderRadius: 10, padding: 12, color: '#9a3412' }}><strong>Admin instruction</strong><div style={{ marginTop: 4 }}>{selectedIssue.assignment_remark || 'No additional instructions were added.'}</div></div><label htmlFor="review-remark" style={{ fontSize: 12, color: '#6b7280', fontWeight: 700, marginTop: 4 }}>Your remark for the admin</label><textarea id="review-remark" maxLength={1000} value={selectedIssue.resolution_note || ''} onChange={(event) => setSelectedIssue((current) => ({ ...current, resolution_note: event.target.value }))} placeholder="Explain your decision or what needs further investigation" rows={4} style={{ padding: '10px 12px', borderRadius: 10, border: '1px solid #cbd5e1', resize: 'vertical', fontFamily: 'inherit' }} /></>}
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
                <div style={{ display: 'flex', gap: 10, flexWrap: 'wrap' }}>
                  <button onClick={() => updateIssueStatus('in_progress')} style={{ padding: '10px 12px', border: 0, borderRadius: 9, background: '#dbeafe', color: '#155dc4', cursor: 'pointer', fontWeight: 800 }}>Start review</button>
                  <button onClick={() => updateIssueStatus('approved')} style={{ padding: '10px 12px', border: 0, borderRadius: 9, background: '#dcfce7', color: '#166534', cursor: 'pointer', fontWeight: 800 }}>Approve</button>
                  <button onClick={() => updateIssueStatus('needs_investigation')} style={{ padding: '10px 12px', border: 0, borderRadius: 9, background: '#fef3c7', color: '#92400e', cursor: 'pointer', fontWeight: 800 }}>Needs investigation</button>
                  <button onClick={() => updateIssueStatus('rejected')} style={{ padding: '10px 12px', border: 0, borderRadius: 9, background: '#f1f5f9', color: '#475569', cursor: 'pointer', fontWeight: 800 }}>Reject flag</button>
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
