import { useEffect, useState } from 'react';

const API_BASE = 'http://127.0.0.1:8000';

function App() {
  const [issues, setIssues] = useState([]);
  const [selectedIssue, setSelectedIssue] = useState(null);

  useEffect(() => {
    fetch(`${API_BASE}/api/qc/review-queue`)
      .then((response) => response.json())
      .then((data) => {
        setIssues(data.issues || []);
        setSelectedIssue(data.issues?.[0] || null);
      })
      .catch(() => {
        setIssues([]);
      });
  }, []);

  return (
    <div style={{ fontFamily: 'Arial, sans-serif', padding: 24, maxWidth: 1200, margin: '0 auto' }}>
      <h1>QC Review Queue</h1>
      <p>Track flagged cases and their severity.</p>
      <div style={{ display: 'grid', gridTemplateColumns: '1fr 1.3fr', gap: 24 }}>
        <div>
          <h2>Pending Issues</h2>
          {issues.length === 0 ? (
            <p>No issues yet.</p>
          ) : (
            <ul style={{ listStyle: 'none', padding: 0, margin: 0 }}>
              {issues.map((issue) => (
                <li key={issue.id} style={{ marginBottom: 12 }}>
                  <button
                    onClick={() => setSelectedIssue(issue)}
                    style={{
                      width: '100%',
                      textAlign: 'left',
                      padding: 12,
                      borderRadius: 8,
                      border: selectedIssue?.id === issue.id ? '2px solid #2563eb' : '1px solid #d1d5db',
                      background: selectedIssue?.id === issue.id ? '#eff6ff' : 'white',
                      cursor: 'pointer',
                    }}
                  >
                    <strong>{issue.rule_name}</strong>
                    <div style={{ color: '#4b5563', fontSize: 12 }}>
                      {issue.submission_id} • {issue.severity}
                    </div>
                  </button>
                </li>
              ))}
            </ul>
          )}
        </div>
        <div style={{ border: '1px solid #e5e7eb', borderRadius: 12, padding: 16, background: '#fafafa' }}>
          {selectedIssue ? (
            <>
              <h2>Issue Details</h2>
              <p><strong>Submission:</strong> {selectedIssue.submission_id}</p>
              <p><strong>Case:</strong> {selectedIssue.case_id || 'N/A'}</p>
              <p><strong>Rule:</strong> {selectedIssue.rule_name}</p>
              <p><strong>Severity:</strong> {selectedIssue.severity}</p>
              <p><strong>Status:</strong> {selectedIssue.status}</p>
              <p><strong>Message:</strong> {selectedIssue.message}</p>
            </>
          ) : (
            <p>Select an issue to inspect it.</p>
          )}
        </div>
      </div>
    </div>
  );
}

export default App;
