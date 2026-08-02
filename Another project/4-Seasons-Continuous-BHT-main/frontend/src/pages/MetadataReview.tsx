import { useEffect, useMemo, useState } from "react";
import { Navigate, useNavigate } from "react-router-dom";
import { ArrowLeft, Check, FileSpreadsheet, Link2, RefreshCw, Search, ShieldCheck, X } from "lucide-react";
import { useAuth } from "@/contexts/AuthContext";

const DEFAULT_API_BASE =
  typeof window !== "undefined" && ["localhost", "127.0.0.1"].includes(window.location.hostname)
    ? "http://localhost:4000"
    : "";
const API_BASE = (import.meta.env.VITE_API_BASE_URL || DEFAULT_API_BASE).replace(/\/+$/, "");
const ADMIN_REVIEW_TOKEN = import.meta.env.VITE_ADMIN_REVIEW_TOKEN || "";

type ReviewItem = {
  id: string;
  entity_type: string;
  change_type: string;
  category?: string;
  status: string;
  recommendation?: string;
  first_observed_period?: string;
  detected_at?: string;
  detected_definition?: Record<string, unknown> | null;
  current_definition?: Record<string, unknown> | null;
  potential_matches?: Array<Record<string, unknown>>;
  warnings?: string[];
};

type Diagnostics = {
  schemaVersion?: string;
  tables?: Record<string, number>;
  pendingReview?: Array<{ entity_type: string; change_type: string; count: number }>;
};

type RegistryBrand = {
  id: string;
  label: string;
  code?: string;
  category?: string;
};

type RegistryQuestion = {
  id: string;
  variable: string;
  label: string;
  category?: string;
};

type GeneratedSpec = {
  diagnostics?: {
    brandUniverseCount?: number;
    questionUniverseCount?: number;
    tableCount?: number;
  };
  generatedTables?: Array<{ id: string; variable: string; title: string; questionType: string; tableType: string }>;
};

async function apiRequest<T>(path: string, options: RequestInit = {}): Promise<T> {
  const response = await fetch(`${API_BASE}${path}`, {
    ...options,
    headers: {
      "Content-Type": "application/json",
      ...(ADMIN_REVIEW_TOKEN ? { "x-admin-review-token": ADMIN_REVIEW_TOKEN } : {}),
      ...(options.headers || {}),
    },
  });
  const payload = await response.json().catch(() => null);
  if (!response.ok || payload?.ok === false) {
    throw new Error(payload?.error || `Request failed (${response.status})`);
  }
  return payload as T;
}

function jsonPreview(value: unknown) {
  if (!value) return "None";
  return JSON.stringify(value, null, 2);
}

function displayValue(value: unknown) {
  if (value === null || value === undefined || value === "") return "-";
  if (Array.isArray(value)) return value.join(", ");
  if (typeof value === "object") return JSON.stringify(value);
  return String(value);
}

export default function MetadataReview() {
  const { user, selectedAdminCategory } = useAuth();
  const navigate = useNavigate();
  const [diagnostics, setDiagnostics] = useState<Diagnostics | null>(null);
  const [items, setItems] = useState<ReviewItem[]>([]);
  const [selectedId, setSelectedId] = useState<string>("");
  const [loading, setLoading] = useState(true);
  const [working, setWorking] = useState("");
  const [message, setMessage] = useState("");
  const [error, setError] = useState("");
  const [brands, setBrands] = useState<RegistryBrand[]>([]);
  const [questions, setQuestions] = useState<RegistryQuestion[]>([]);
  const [generatedSpec, setGeneratedSpec] = useState<GeneratedSpec | null>(null);
  const [action, setAction] = useState("approve");
  const [targetId, setTargetId] = useState("");
  const [alias, setAlias] = useState("");
  const [category, setCategory] = useState("");
  const [effectiveFrom, setEffectiveFrom] = useState("");

  const selected = useMemo(() => items.find((item) => item.id === selectedId) || items[0] || null, [items, selectedId]);

  const load = async () => {
    setLoading(true);
    setError("");
    try {
      const [diag, review] = await Promise.all([
        apiRequest<Diagnostics>("/api/admin/metadata/diagnostics"),
        apiRequest<{ items: ReviewItem[] }>("/api/admin/metadata/review"),
      ]);
      setDiagnostics(diag);
      setItems(review.items || []);
      if (!selectedId && review.items?.[0]) setSelectedId(review.items[0].id);
      const [brandPayload, questionPayload, specPayload] = await Promise.all([
        apiRequest<{ brands: RegistryBrand[] }>("/api/admin/metadata/registry/brands"),
        apiRequest<{ questions: RegistryQuestion[] }>("/api/admin/metadata/registry/questions"),
        apiRequest<GeneratedSpec>("/api/admin/metadata/export-spec"),
      ]);
      setBrands(brandPayload.brands || []);
      setQuestions(questionPayload.questions || []);
      setGeneratedSpec(specPayload);
    } catch (err) {
      setError(err instanceof Error ? err.message : "Failed to load metadata review.");
    } finally {
      setLoading(false);
    }
  };

  useEffect(() => {
    void load();
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, []);

  if (!user || user.role !== "admin") {
    return <Navigate to="/" replace />;
  }

  const runAction = async (name: string, fn: () => Promise<unknown>) => {
    setWorking(name);
    setError("");
    setMessage("");
    try {
      await fn();
      setMessage(`${name} completed.`);
      await load();
    } catch (err) {
      setError(err instanceof Error ? err.message : `${name} failed.`);
    } finally {
      setWorking("");
    }
  };

  const approveSelected = () => {
    if (!selected) return;
    const detected = selected.detected_definition || {};
    const body = {
      approvedBy: user.username,
      category: category || selected.category || selectedAdminCategory?.category || detected.category || "Unassigned",
      effectiveFrom: effectiveFrom || selected.first_observed_period || undefined,
      note: "Approved from Metadata Review.",
    };
    const payload = {
      ...body,
      targetBrandId: targetId || undefined,
      targetQuestionId: targetId || undefined,
      alias: alias || displayValue(detected.label || detected.code || detected.variable),
    };
    const endpoint =
      action === "alias" ? "add-alias"
        : action === "merge" ? "merge-brand"
          : action === "link" ? "link-question"
            : action === "replace" ? "replace-question"
              : action === "non-reportable" ? "non-reportable"
              : "approve";
    return runAction(action === "approve" ? "Approve" : action, () =>
      apiRequest(`/api/admin/metadata/review/${selected.id}/${endpoint}`, {
        method: "POST",
        body: JSON.stringify(payload),
      }),
    );
  };

  const rejectSelected = () => {
    if (!selected) return;
    return runAction("Reject", () =>
      apiRequest(`/api/admin/metadata/review/${selected.id}/reject`, {
        method: "POST",
        body: JSON.stringify({ approvedBy: user.username, note: "Rejected from Metadata Review." }),
      }),
    );
  };

  return (
    <main className="min-h-screen bg-slate-100 px-4 py-6 text-slate-950">
      <div className="mx-auto max-w-7xl space-y-5">
        <div className="flex flex-wrap items-center justify-between gap-3">
          <div>
            <button
              type="button"
              onClick={() => navigate(`/admin/dashboard/${selectedAdminCategory?.slug || "noodles"}/export-report`)}
              className="inline-flex items-center gap-2 rounded-lg border border-slate-300 bg-white px-3 py-2 text-sm font-semibold text-slate-700 hover:bg-slate-50"
            >
              <ArrowLeft className="h-4 w-4" />
              Dashboard
            </button>
            <h1 className="mt-4 text-2xl font-bold">Metadata Review</h1>
          </div>
          <div className="flex flex-wrap gap-2">
            <button
              type="button"
              onClick={() => runAction("Dry-run migration", () => apiRequest("/api/admin/metadata/migrate/dry-run", { method: "POST" }))}
              className="inline-flex items-center gap-2 rounded-lg border border-slate-300 bg-white px-3 py-2 text-sm font-semibold hover:bg-slate-50"
              disabled={Boolean(working)}
            >
              <ShieldCheck className="h-4 w-4" />
              Dry Run
            </button>
            <button
              type="button"
              onClick={() => runAction("Detection", () => apiRequest("/api/admin/metadata/detect", { method: "POST", body: JSON.stringify({}) }))}
              className="inline-flex items-center gap-2 rounded-lg border border-slate-300 bg-white px-3 py-2 text-sm font-semibold hover:bg-slate-50"
              disabled={Boolean(working)}
            >
              <Search className="h-4 w-4" />
              Detect
            </button>
            <button
              type="button"
              onClick={load}
              className="inline-flex items-center gap-2 rounded-lg border border-slate-300 bg-white px-3 py-2 text-sm font-semibold hover:bg-slate-50"
              disabled={loading || Boolean(working)}
            >
              <RefreshCw className="h-4 w-4" />
              Refresh
            </button>
          </div>
        </div>

        {message ? <div className="rounded-lg border border-emerald-200 bg-emerald-50 px-4 py-3 text-sm font-semibold text-emerald-800">{message}</div> : null}
        {error ? <div className="rounded-lg border border-red-200 bg-red-50 px-4 py-3 text-sm font-semibold text-red-700">{error}</div> : null}

        <section className="grid gap-3 md:grid-cols-4">
          {Object.entries(diagnostics?.tables || {}).map(([name, count]) => (
            <div key={name} className="rounded-lg border border-slate-200 bg-white p-4">
              <div className="text-xs font-semibold uppercase tracking-wide text-slate-500">{name.replace(/_/g, " ")}</div>
              <div className="mt-2 text-2xl font-bold">{count}</div>
            </div>
          ))}
        </section>

        <section className="rounded-lg border border-slate-200 bg-white p-4">
          <div className="flex flex-wrap items-center justify-between gap-3">
            <div>
              <h2 className="font-semibold">Generated Export Spec Preview</h2>
              <p className="text-sm text-slate-500">Registry-driven table universe preview for approved active records.</p>
            </div>
            <div className="inline-flex items-center gap-2 rounded-lg bg-slate-100 px-3 py-2 text-sm font-semibold text-slate-700">
              <FileSpreadsheet className="h-4 w-4" />
              {generatedSpec?.diagnostics?.tableCount || 0} tables
            </div>
          </div>
          <div className="mt-3 grid gap-3 md:grid-cols-3">
            <div className="rounded-lg border border-slate-200 p-3 text-sm">
              <div className="font-semibold">Brands</div>
              <div className="text-2xl font-bold">{generatedSpec?.diagnostics?.brandUniverseCount || 0}</div>
            </div>
            <div className="rounded-lg border border-slate-200 p-3 text-sm">
              <div className="font-semibold">Questions</div>
              <div className="text-2xl font-bold">{generatedSpec?.diagnostics?.questionUniverseCount || 0}</div>
            </div>
            <div className="rounded-lg border border-slate-200 p-3 text-sm">
              <div className="font-semibold">Generated tables</div>
              <div className="text-2xl font-bold">{generatedSpec?.diagnostics?.tableCount || 0}</div>
            </div>
          </div>
        </section>

        <section className="grid gap-4 lg:grid-cols-[minmax(280px,420px)_1fr]">
          <div className="rounded-lg border border-slate-200 bg-white">
            <div className="border-b border-slate-200 px-4 py-3">
              <h2 className="font-semibold">Pending Queue</h2>
              <p className="text-sm text-slate-500">{loading ? "Loading..." : `${items.length} pending changes`}</p>
            </div>
            <div className="max-h-[650px] overflow-auto">
              {items.map((item) => (
                <button
                  key={item.id}
                  type="button"
                  onClick={() => setSelectedId(item.id)}
                  className={`block w-full border-b border-slate-100 px-4 py-3 text-left hover:bg-slate-50 ${selected?.id === item.id ? "bg-slate-100" : ""}`}
                >
                  <div className="flex items-center justify-between gap-2">
                    <span className="text-sm font-bold capitalize">{item.entity_type}</span>
                    <span className="rounded-full bg-slate-200 px-2 py-1 text-xs font-semibold">{item.change_type}</span>
                  </div>
                  <div className="mt-1 text-sm text-slate-700">{displayValue(item.detected_definition?.label || item.detected_definition?.variable || item.detected_definition?.code)}</div>
                  <div className="mt-1 text-xs text-slate-500">{item.recommendation || "Review required"}</div>
                </button>
              ))}
              {!items.length && !loading ? <div className="px-4 py-8 text-center text-sm text-slate-500">No pending metadata changes.</div> : null}
            </div>
          </div>

          <div className="rounded-lg border border-slate-200 bg-white">
            <div className="flex flex-wrap items-center justify-between gap-2 border-b border-slate-200 px-4 py-3">
              <div>
                <h2 className="font-semibold">Change Detail</h2>
                <p className="text-sm text-slate-500">{selected ? `${selected.entity_type} / ${selected.change_type}` : "Select a change"}</p>
              </div>
              <div className="flex gap-2">
                <button
                  type="button"
                  onClick={approveSelected}
                  disabled={!selected || Boolean(working)}
                  className="inline-flex items-center gap-2 rounded-lg border border-emerald-600 bg-emerald-600 px-3 py-2 text-sm font-semibold text-white disabled:opacity-50"
                >
                  <Check className="h-4 w-4" />
                  Approve
                </button>
                <button
                  type="button"
                  onClick={rejectSelected}
                  disabled={!selected || Boolean(working)}
                  className="inline-flex items-center gap-2 rounded-lg border border-red-600 bg-white px-3 py-2 text-sm font-semibold text-red-700 disabled:opacity-50"
                >
                  <X className="h-4 w-4" />
                  Reject
                </button>
              </div>
            </div>
            {selected ? (
              <div className="grid gap-4 p-4 xl:grid-cols-2">
                <div className="xl:col-span-2 rounded-lg border border-slate-200 bg-slate-50 p-4">
                  <h3 className="text-sm font-bold uppercase tracking-wide text-slate-500">Review Action</h3>
                  <div className="mt-3 grid gap-3 md:grid-cols-4">
                    <label className="text-sm font-semibold">
                      Action
                      <select
                        value={action}
                        onChange={(event) => setAction(event.target.value)}
                        className="mt-1 w-full rounded-lg border border-slate-300 bg-white px-3 py-2"
                      >
                        <option value="approve">Approve as new</option>
                        {selected.entity_type === "brand" ? <option value="alias">Add as alias</option> : null}
                        {selected.entity_type === "brand" ? <option value="merge">Merge brand</option> : null}
                        {selected.entity_type === "question" ? <option value="link">Link question</option> : null}
                        {selected.entity_type === "question" ? <option value="replace">Replace question</option> : null}
                        {selected.entity_type === "question" ? <option value="non-reportable">Mark non-reportable</option> : null}
                      </select>
                    </label>
                    <label className="text-sm font-semibold">
                      Category
                      <input
                        value={category}
                        onChange={(event) => setCategory(event.target.value)}
                        placeholder={selected.category || selectedAdminCategory?.category || "Unassigned"}
                        className="mt-1 w-full rounded-lg border border-slate-300 bg-white px-3 py-2"
                      />
                    </label>
                    <label className="text-sm font-semibold">
                      Effective period
                      <input
                        value={effectiveFrom}
                        onChange={(event) => setEffectiveFrom(event.target.value)}
                        placeholder={selected.first_observed_period || "YYYY-MM"}
                        className="mt-1 w-full rounded-lg border border-slate-300 bg-white px-3 py-2"
                      />
                    </label>
                    <label className="text-sm font-semibold">
                      Target
                      <select
                        value={targetId}
                        onChange={(event) => setTargetId(event.target.value)}
                        disabled={action === "approve" || action === "non-reportable"}
                        className="mt-1 w-full rounded-lg border border-slate-300 bg-white px-3 py-2 disabled:bg-slate-100"
                      >
                        <option value="">Select target</option>
                        {(selected.entity_type === "brand" ? brands : questions).slice(0, 1000).map((item) => (
                          <option key={item.id} value={item.id}>
                            {selected.entity_type === "brand"
                              ? `${(item as RegistryBrand).category || ""} / ${(item as RegistryBrand).label}`
                              : `${(item as RegistryQuestion).category || ""} / ${(item as RegistryQuestion).variable} ${(item as RegistryQuestion).label}`}
                          </option>
                        ))}
                      </select>
                    </label>
                  </div>
                  {selected.entity_type === "brand" && action === "alias" ? (
                    <label className="mt-3 block text-sm font-semibold">
                      Alias label
                      <input
                        value={alias}
                        onChange={(event) => setAlias(event.target.value)}
                        placeholder={displayValue(selected.detected_definition?.label || selected.detected_definition?.code)}
                        className="mt-1 w-full rounded-lg border border-slate-300 bg-white px-3 py-2"
                      />
                    </label>
                  ) : null}
                  {action !== "approve" && action !== "non-reportable" ? (
                    <p className="mt-3 inline-flex items-center gap-2 text-xs font-semibold text-slate-500">
                      <Link2 className="h-3.5 w-3.5" />
                      Alias, merge, link, and replacement actions require a selected target.
                    </p>
                  ) : null}
                  {action === "non-reportable" ? (
                    <p className="mt-3 text-xs font-semibold text-slate-500">
                      This marks the detection as reviewed without creating a reportable export question.
                    </p>
                  ) : null}
                </div>
                <div>
                  <h3 className="text-sm font-bold uppercase tracking-wide text-slate-500">Detected Definition</h3>
                  <pre className="mt-2 max-h-[520px] overflow-auto rounded-lg bg-slate-950 p-4 text-xs text-slate-50">{jsonPreview(selected.detected_definition)}</pre>
                </div>
                <div>
                  <h3 className="text-sm font-bold uppercase tracking-wide text-slate-500">Current Definition</h3>
                  <pre className="mt-2 max-h-[520px] overflow-auto rounded-lg bg-slate-950 p-4 text-xs text-slate-50">{jsonPreview(selected.current_definition)}</pre>
                </div>
                <div className="xl:col-span-2">
                  <h3 className="text-sm font-bold uppercase tracking-wide text-slate-500">Impact</h3>
                  <div className="mt-2 grid gap-3 md:grid-cols-3">
                    <div className="rounded-lg border border-slate-200 p-3 text-sm">
                      <div className="font-semibold">First observed</div>
                      <div>{displayValue(selected.first_observed_period)}</div>
                    </div>
                    <div className="rounded-lg border border-slate-200 p-3 text-sm">
                      <div className="font-semibold">Potential matches</div>
                      <div>{selected.potential_matches?.length || 0}</div>
                    </div>
                    <div className="rounded-lg border border-slate-200 p-3 text-sm">
                      <div className="font-semibold">Warnings</div>
                      <div>{selected.warnings?.join(", ") || "-"}</div>
                    </div>
                  </div>
                </div>
              </div>
            ) : (
              <div className="p-8 text-center text-sm text-slate-500">No change selected.</div>
            )}
          </div>
        </section>
      </div>
    </main>
  );
}
