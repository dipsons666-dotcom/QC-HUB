export async function fetchQueuedImports(apiBase) {
  const resp = await fetch(`${apiBase}/api/import/queued`);
  if (!resp.ok) throw new Error('Failed to fetch queued imports');
  return resp.json();
}

export async function fetchSyncStatus(apiBase) {
  const resp = await fetch(`${apiBase}/api/import/sync-status`);
  if (!resp.ok) throw new Error('Failed to fetch sync status');
  return resp.json();
}
