import {
  Activity,
  AlertTriangle,
  CheckCircle2,
  ChevronRight,
  ClipboardCheck,
  Database,
  Download,
  ExternalLink,
  FileSearch,
  LayoutDashboard,
  ListChecks,
  PanelRightOpen,
  Play,
  RefreshCw,
  Search,
  ServerCog,
  Settings,
  ShieldCheck,
  Upload,
  X,
  XCircle,
} from "lucide-react";
import { ChangeEvent, ReactNode, useEffect, useMemo, useRef, useState } from "react";

type Page = "dashboard" | "database" | "research" | "review" | "sources" | "activity";
type Bidder = Record<string, string | number | boolean> & { _internal_id: number };
type Source = { key: string; name: string; url: string; category: string; status: string };
type Diagnostic = {
  id: number;
  timestamp: string;
  severity: string;
  source_key?: string | null;
  bidder_name?: string | null;
  stage?: string | null;
  message: string;
  details?: Record<string, unknown>;
};
type Run = {
  id: number;
  status: string;
  created_at: string;
  bidder_count: number;
  source_count: number;
  source_keys: string[];
  message?: string;
};
type DashboardData = {
  bidder_count: number;
  source_count: number;
  implemented_source_count: number;
  pending_review_count: number;
  pending_change_count?: number;
  pending_identity_count?: number;
  active_import: null | { filename: string; imported_at: string; row_count: number; columns: string[] };
  last_run: Run | null;
};
type ImportPreview = {
  filename: string;
  row_count: number;
  columns: string[];
  missing_expected_columns: string[];
  extra_columns: string[];
  validation_errors: string[];
  validation_warnings: string[];
  valid_for_import: boolean;
  preview: Record<string, string>[];
};
type ReviewItem = {
  id: number;
  contractor_name: string;
  field_name: string;
  current_value?: string | null;
  proposed_value?: string | null;
  source_key: string;
  source_url?: string | null;
  identity_status?: string;
  completeness_status?: string;
  result_status?: string;
  status: string;
};
type IdentityCandidate = {
  source_record_id: string;
  score: number;
  name_score: number;
  address_score: number;
  city_score: number;
  state_score: number;
  matched_search_name?: string;
  matched_record_name?: string;
  record: Record<string, string>;
};
type IdentityReviewItem = {
  snapshot_id: number;
  research_run_id: number;
  research_task_id: number;
  bidder_id: number;
  contractor_name: string;
  source_key: string;
  retrieved_at: string;
  result_status: string;
  completeness_status: string;
  warnings: string[];
  candidates: IdentityCandidate[];
};
type SamStatus = {
  source_key: string;
  implemented: boolean;
  acquisition_mode?: string;
  cached_extract: string | null;
  cached_extract_date: string | null;
  cached_record_count: number;
};
type SamUploadResponse = {
  item: {
    filename: string;
    csv_name?: string;
    extract_date: string | null;
    record_count: number;
    sha256?: string;
    acquisition_mode?: string;
  };
  comparison: null | {
    run: Run;
    executed: number;
    skipped: number;
    already_processed: number;
    proposal_count: number;
    status_counts: Record<string, number>;
  };
  message: string;
};

const API = import.meta.env.VITE_API_URL || "";

const BIDDER_FIELD_GROUPS = [
  {
    title: "Identity & addresses",
    fields: [
      "id", "contractor_name", "related_companies", "address_1", "city", "state", "zip",
      "additional_address", "additional_address_city", "additional_address_state", "additional_address_zip",
    ],
  },
  {
    title: "Business & coverage",
    fields: ["dfi", "wc", "wc_date"],
  },
  {
    title: "Safety, eligibility & public works",
    fields: [
      "osha_severe_violations", "years", "osha", "state_federal_debarment", "mndol_ineligibility",
      "public_works_projects_budget_time_quality_complaint",
    ],
  },
  {
    title: "Courts, regulatory & complaints",
    fields: [
      "federal_court", "circuit_court", "ccap_show150", "environmental_violations",
      "prevailing_wage_violations", "dwd", "dwd_substance_abuse_plan",
      "better_business_bureau_complaints", "misc_violations", "tax_liability",
    ],
  },
] as const;

const EXPECTED_BIDDER_FIELDS = BIDDER_FIELD_GROUPS.flatMap((group) => [...group.fields]);

async function request<T>(path: string, options?: RequestInit): Promise<T> {
  const response = await fetch(`${API}${path}`, options);
  if (!response.ok) {
    let message = `Request failed (${response.status})`;
    try {
      const body = await response.json();
      const detail = body.detail;
      if (typeof detail === "string") message = detail;
      else if (detail?.message) message = String(detail.message);
      else if (detail) message = JSON.stringify(detail);
    } catch {
      // Keep fallback message.
    }
    throw new Error(message);
  }
  return response.json() as Promise<T>;
}

function prettyField(value: string) {
  return value
    .replace(/^_/, "")
    .split("_")
    .map((part) => part.charAt(0).toUpperCase() + part.slice(1))
    .join(" ");
}

function statusLabel(status: string) {
  return status.replaceAll("_", " ").replace(/\b\w/g, (c) => c.toUpperCase());
}

function formatDate(value?: string | null) {
  if (!value) return "—";
  const date = new Date(value);
  return Number.isNaN(date.getTime()) ? value : date.toLocaleString();
}

function formatPercent(value?: number | null) {
  if (value === undefined || value === null || Number.isNaN(value)) return "—";
  return `${Math.round(value * 100)}%`;
}

function Card({ children, className = "" }: { children: ReactNode; className?: string }) {
  return <section className={`card ${className}`}>{children}</section>;
}

export default function App() {
  const [page, setPage] = useState<Page>("dashboard");
  const [dashboard, setDashboard] = useState<DashboardData | null>(null);
  const [sources, setSources] = useState<Source[]>([]);
  const [bidders, setBidders] = useState<Bidder[]>([]);
  const [bidderTotal, setBidderTotal] = useState(0);
  const [runs, setRuns] = useState<Run[]>([]);
  const [reviewItems, setReviewItems] = useState<ReviewItem[]>([]);
  const [identityReviewItems, setIdentityReviewItems] = useState<IdentityReviewItem[]>([]);
  const [diagnostics, setDiagnostics] = useState<Diagnostic[]>([]);
  const [diagnosticsOpen, setDiagnosticsOpen] = useState(false);
  const [search, setSearch] = useState("");
  const [selectedBidderIds, setSelectedBidderIds] = useState<number[]>([]);
  const [selectedSources, setSelectedSources] = useState<string[]>([]);
  const [researchScope, setResearchScope] = useState<"all" | "selected">("all");
  const [selectedBidder, setSelectedBidder] = useState<Bidder | null>(null);
  const [busy, setBusy] = useState(false);
  const [error, setError] = useState("");
  const [uploadFile, setUploadFile] = useState<File | null>(null);
  const [preview, setPreview] = useState<ImportPreview | null>(null);
  const [samStatus, setSamStatus] = useState<SamStatus | null>(null);
  const [samMessage, setSamMessage] = useState("");
  const fileInput = useRef<HTMLInputElement>(null);
  const samSourceInput = useRef<HTMLInputElement>(null);
  const samResearchInput = useRef<HTMLInputElement>(null);

  const refreshSamStatus = async () => {
    try {
      const data = await request<{ item: SamStatus }>("/api/sources/sam/status");
      setSamStatus(data.item);
    } catch {
      setSamStatus(null);
    }
  };

  const refresh = async () => {
    setError("");
    try {
      const [dashboardData, sourceData, bidderData, runData, reviewData, identityData, diagnosticData] = await Promise.all([
        request<DashboardData>("/api/dashboard"),
        request<{ items: Source[] }>("/api/sources"),
        request<{ items: Bidder[]; total: number }>(`/api/bidders?search=${encodeURIComponent(search)}&limit=250`),
        request<{ items: Run[] }>("/api/runs"),
        request<{ items: ReviewItem[] }>("/api/review"),
        request<{ items: IdentityReviewItem[] }>("/api/identity-review"),
        request<{ items: Diagnostic[] }>("/api/diagnostics"),
      ]);
      setDashboard(dashboardData);
      setSources(sourceData.items);
      setBidders(bidderData.items);
      setBidderTotal(bidderData.total);
      setRuns(runData.items);
      setReviewItems(reviewData.items);
      setIdentityReviewItems(identityData.items);
      setDiagnostics(diagnosticData.items);
      setSelectedSources((current) => {
        const ready = sourceData.items.filter((source) => source.status === "ready").map((source) => source.key);
        const filtered = current.filter((key) => ready.includes(key));
        return filtered.length ? filtered : ready;
      });
    } catch (err) {
      setError(err instanceof Error ? err.message : "Unable to load application data.");
    }
  };

  useEffect(() => {
    void refresh();
  }, []);

  useEffect(() => {
    if (page === "sources" || page === "research") void refreshSamStatus();
  }, [page]);

  useEffect(() => {
    const timer = window.setTimeout(async () => {
      try {
        const data = await request<{ items: Bidder[]; total: number }>(`/api/bidders?search=${encodeURIComponent(search)}&limit=250`);
        setBidders(data.items);
        setBidderTotal(data.total);
      } catch (err) {
        setError(err instanceof Error ? err.message : "Unable to search bidders.");
      }
    }, 250);
    return () => window.clearTimeout(timer);
  }, [search]);

  const handleFile = async (event: ChangeEvent<HTMLInputElement>) => {
    const file = event.target.files?.[0];
    if (!file) return;
    setBusy(true);
    setError("");
    try {
      const body = new FormData();
      body.append("file", file);
      const result = await request<ImportPreview>("/api/import/preview", { method: "POST", body });
      setUploadFile(file);
      setPreview(result);
    } catch (err) {
      setError(err instanceof Error ? err.message : "Unable to preview CSV.");
    } finally {
      setBusy(false);
      event.target.value = "";
    }
  };

  const confirmImport = async () => {
    if (!uploadFile || !preview?.valid_for_import) return;
    setBusy(true);
    setError("");
    try {
      const body = new FormData();
      body.append("file", uploadFile);
      await request("/api/import", { method: "POST", body });
      setPreview(null);
      setUploadFile(null);
      setSelectedBidderIds([]);
      setPage("database");
      await refresh();
    } catch (err) {
      setError(err instanceof Error ? err.message : "Unable to import CSV.");
    } finally {
      setBusy(false);
    }
  };

  const handleSamExtract = async (event: ChangeEvent<HTMLInputElement>, useResearchScope: boolean) => {
    const file = event.target.files?.[0];
    if (!file) return;
    setBusy(true);
    setError("");
    setSamMessage("");
    try {
      const body = new FormData();
      body.append("file", file);
      let path = "/api/sources/sam/extract";
      if (useResearchScope && researchScope === "selected" && selectedBidderIds.length) {
        path += `?bidder_ids=${encodeURIComponent(selectedBidderIds.join(","))}`;
      }
      const result = await request<SamUploadResponse>(path, { method: "POST", body });
      if (result.comparison) {
        const ambiguous =
          (result.comparison.status_counts.AMBIGUOUS_MATCH || 0) +
          (result.comparison.status_counts.MANUAL_REVIEW_REQUIRED || 0);
        const proposals = result.comparison.proposal_count;
        const compared = result.comparison.run.bidder_count;
        const issues = proposals + ambiguous;
        if (issues > 0) {
          setSamMessage(
            `Compared ${compared} bidder${compared === 1 ? "" : "s"}. ${proposals} data difference${proposals === 1 ? "" : "s"} and ${ambiguous} identity match${ambiguous === 1 ? "" : "es"} need review.`,
          );
        } else {
          setSamMessage(`Compared ${compared} bidder${compared === 1 ? "" : "s"}. No confirmed SAM differences were found.`);
        }
      } else {
        setSamMessage(result.message);
      }
      await refreshSamStatus();
      await refresh();
      if (result.comparison) {
        const ambiguous =
          (result.comparison.status_counts.AMBIGUOUS_MATCH || 0) +
          (result.comparison.status_counts.MANUAL_REVIEW_REQUIRED || 0);
        if (result.comparison.proposal_count > 0 || ambiguous > 0) setPage("review");
      }
    } catch (err) {
      setError(err instanceof Error ? err.message : "Unable to load and compare the SAM exclusions extract.");
    } finally {
      setBusy(false);
      event.target.value = "";
    }
  };

  const createRun = async () => {
    if (!selectedSources.length) return;
    setBusy(true);
    setError("");
    try {
      await request("/api/runs", {
        method: "POST",
        headers: { "Content-Type": "application/json" },
        body: JSON.stringify({
          source_keys: selectedSources,
          bidder_ids: researchScope === "selected" ? selectedBidderIds : null,
        }),
      });
      await refresh();
    } catch (err) {
      setError(err instanceof Error ? err.message : "Unable to run research.");
    } finally {
      setBusy(false);
    }
  };

  const reviewChange = async (id: number, decision: "approved" | "dismissed") => {
    setBusy(true);
    setError("");
    try {
      await request(`/api/review/${id}`, {
        method: "POST",
        headers: { "Content-Type": "application/json" },
        body: JSON.stringify({ decision, actor: "local-user" }),
      });
      await refresh();
    } catch (err) {
      setError(err instanceof Error ? err.message : "Unable to review proposed change.");
    } finally {
      setBusy(false);
    }
  };

  const resolveIdentity = async (
    snapshotId: number,
    sourceRecordId: string,
    judgment: "SAME_ENTITY" | "DIFFERENT_ENTITY",
  ) => {
    setBusy(true);
    setError("");
    try {
      await request(`/api/identity-review/${snapshotId}`, {
        method: "POST",
        headers: { "Content-Type": "application/json" },
        body: JSON.stringify({ source_record_id: sourceRecordId, judgment, actor: "local-user" }),
      });
      await refresh();
    } catch (err) {
      setError(err instanceof Error ? err.message : "Unable to save identity decision.");
    } finally {
      setBusy(false);
    }
  };

  const clearDiagnostics = async () => {
    await request("/api/diagnostics", { method: "DELETE" });
    setDiagnostics([]);
  };

  const toggleBidder = (id: number) => {
    setSelectedBidderIds((current) => current.includes(id) ? current.filter((item) => item !== id) : [...current, id]);
  };

  const selectSourceOnly = (source: Source) => {
    if (source.status !== "ready") return;
    setSelectedSources([source.key]);
    setPage("research");
  };

  const nav = [
    ["dashboard", "Dashboard", LayoutDashboard],
    ["database", "Bidder Database", Database],
    ["research", "Research", FileSearch],
    ["review", "Review", ClipboardCheck],
    ["sources", "Sources", ServerCog],
    ["activity", "Activity", Activity],
  ] as const;

  const readySources = useMemo(() => sources.filter((source) => source.status === "ready"), [sources]);
  const samSelected = selectedSources.includes("sam");
  const researchScopeCount = researchScope === "all" ? (dashboard?.bidder_count ?? 0) : selectedBidderIds.length;

  return (
    <div className="app-shell">
      <aside className="sidebar">
        <div className="brand">
          <div className="brand-mark"><ShieldCheck size={22} /></div>
          <div><strong>Paralegal</strong><span>Research Desk</span></div>
        </div>
        <nav>
          {nav.map(([key, label, Icon]) => (
            <button key={key} className={page === key ? "nav-item active" : "nav-item"} onClick={() => setPage(key)}>
              <Icon size={18} /><span>{label}</span>
              {key === "review" && dashboard?.pending_review_count ? <em>{dashboard.pending_review_count}</em> : null}
            </button>
          ))}
        </nav>
        <div className="sidebar-spacer" />
        <button className="nav-item"><Settings size={18} /><span>Settings</span></button>
        <div className="poc-tag">Proof of Concept · V2</div>
      </aside>

      <main className="main-area">
        <header className="topbar">
          <div>
            <div className="eyebrow">Paralegal Research Desk</div>
            <h1>{nav.find(([key]) => key === page)?.[1]}</h1>
          </div>
          <div className="top-actions">
            <button className="btn ghost" onClick={() => void refresh()}><RefreshCw size={16} /> Refresh</button>
            <button className="btn ghost" onClick={() => setDiagnosticsOpen(true)}><PanelRightOpen size={16} /> Diagnostics</button>
            <button className="btn primary" onClick={() => setPage("research")}><Play size={16} /> Run Research</button>
          </div>
        </header>

        {error && <div className="error-banner"><AlertTriangle size={17} /><span>{error}</span><button onClick={() => setError("")}><X size={16} /></button></div>}

        <div className="page-content">
          {page === "dashboard" && (
            <>
              <div className="metric-grid">
                <Metric label="Approved bidders" value={dashboard?.bidder_count ?? 0} hint={dashboard?.active_import?.filename || "No master CSV imported"} />
                <Metric label="Ready sources" value={dashboard?.implemented_source_count ?? 0} hint={`${dashboard?.source_count ?? 14} sources defined`} />
                <Metric label="Pending review" value={dashboard?.pending_review_count ?? 0} hint={`${dashboard?.pending_identity_count ?? 0} identity · ${dashboard?.pending_change_count ?? 0} data changes`} />
                <Metric label="Bidder schema" value={EXPECTED_BIDDER_FIELDS.length} hint="Example database fields supported" />
              </div>
              <div className="dashboard-grid">
                <Card>
                  <div className="card-heading"><div><span className="section-kicker">Master data</span><h2>Approved Bidder Database</h2></div><Database size={22} /></div>
                  {dashboard?.active_import ? (
                    <div className="master-summary">
                      <div><strong>{dashboard.active_import.filename}</strong><span>{dashboard.active_import.row_count} bidder records</span></div>
                      <div><strong>{formatDate(dashboard.active_import.imported_at)}</strong><span>Last imported</span></div>
                      <div><strong>{dashboard.active_import.columns.length}</strong><span>Preserved CSV fields</span></div>
                    </div>
                  ) : <Empty compact title="No master database imported" text="Import the firm's bidder CSV to establish the approved research scope." />}
                  <div className="button-row">
                    <button className="btn secondary" onClick={() => fileInput.current?.click()}><Upload size={16} /> Import CSV</button>
                    {dashboard?.active_import && <a className="btn ghost" href={`${API}/api/export`}><Download size={16} /> Export Approved CSV</a>}
                  </div>
                </Card>
                <Card>
                  <div className="card-heading"><div><span className="section-kicker">Workflow</span><h2>Research → Compare → Review</h2></div><ListChecks size={22} /></div>
                  <div className="workflow-list">
                    <WorkflowStep number="1" title="Research" text="Run implemented sources against approved bidders only." />
                    <WorkflowStep number="2" title="Verify identity" text="Ambiguous company matches stop for human judgment instead of guessing." />
                    <WorkflowStep number="3" title="Approve changes" text="Only reviewed findings can update the approved master." />
                  </div>
                  <button className="btn primary full" onClick={() => setPage("research")}><Play size={16} /> Configure Research Run</button>
                </Card>
              </div>
              <Card>
                <div className="card-heading"><div><span className="section-kicker">Source readiness</span><h2>Research Sources</h2></div><button className="text-button" onClick={() => setPage("sources")}>View all <ChevronRight size={16} /></button></div>
                <div className="source-strip">
                  {sources.slice(0, 6).map((source) => <SourceMini key={source.key} source={source} />)}
                </div>
              </Card>
            </>
          )}

          {page === "database" && (
            <Card className="table-card">
              <div className="card-heading database-heading">
                <div><span className="section-kicker">Approved master</span><h2>Bidder Database</h2><p>{bidderTotal} records · All {EXPECTED_BIDDER_FIELDS.length} expected fields are preserved; this grid shows a working summary.</p></div>
                <div className="button-row">
                  <button className="btn secondary" onClick={() => fileInput.current?.click()}><Upload size={16} /> Import CSV</button>
                  {dashboard?.active_import && <a className="btn ghost" href={`${API}/api/export`}><Download size={16} /> Export CSV</a>}
                </div>
              </div>
              <div className="table-toolbar">
                <label className="search-box"><Search size={16} /><input value={search} onChange={(e) => setSearch(e.target.value)} placeholder="Search name, ID, city, state…" /></label>
                <span>{selectedBidderIds.length} selected</span>
              </div>
              {!bidders.length ? <Empty title="No bidders to display" text="Import the example CSV or the firm's current bidder database to begin." /> : (
                <div className="table-wrap">
                  <table>
                    <thead><tr><th></th><th>ID</th><th>Contractor</th><th>Related companies</th><th>Location</th><th>DFI</th><th>WC</th><th>OSHA</th><th>Debarment</th></tr></thead>
                    <tbody>
                      {bidders.map((bidder) => (
                        <tr key={bidder._internal_id}>
                          <td><input type="checkbox" checked={selectedBidderIds.includes(bidder._internal_id)} onChange={() => toggleBidder(bidder._internal_id)} /></td>
                          <td>{String(bidder.id || "")}</td>
                          <td><button className="row-link" onClick={() => setSelectedBidder(bidder)}>{String(bidder.contractor_name || "")}</button></td>
                          <td>{String(bidder.related_companies || "—")}</td>
                          <td>{[bidder.city, bidder.state].filter(Boolean).join(", ") || "—"}</td>
                          <td><DataValue value={bidder.dfi} /></td>
                          <td><DataValue value={bidder.wc} /></td>
                          <td><DataValue value={bidder.osha} /></td>
                          <td><DataValue value={bidder.state_federal_debarment} /></td>
                        </tr>
                      ))}
                    </tbody>
                  </table>
                </div>
              )}
            </Card>
          )}

          {page === "research" && (
            <div className="research-layout">
              <Card>
                <div className="card-heading"><div><span className="section-kicker">Step 1</span><h2>Choose bidder scope</h2></div><Database size={21} /></div>
                <label className={`choice-card ${researchScope === "all" ? "selected" : ""}`}><input type="radio" checked={researchScope === "all"} onChange={() => setResearchScope("all")} /><div><strong>All approved bidders</strong><span>Run against all {dashboard?.bidder_count ?? 0} bidders in the current master.</span></div></label>
                <label className={`choice-card ${researchScope === "selected" ? "selected" : ""}`}><input type="radio" checked={researchScope === "selected"} onChange={() => setResearchScope("selected")} /><div><strong>Selected bidders</strong><span>{selectedBidderIds.length ? `${selectedBidderIds.length} bidders selected in Bidder Database.` : "Select bidders from the database table first."}</span></div></label>
              </Card>

              <Card>
                <div className="card-heading"><div><span className="section-kicker">Step 2</span><h2>Choose sources</h2><p>Only sources marked Ready can run.</p></div><ServerCog size={21} /></div>
                <div className="source-checklist">
                  {sources.map((source) => {
                    const ready = source.status === "ready";
                    return (
                      <label key={source.key} className="source-check-row" style={{ opacity: ready ? 1 : 0.55 }}>
                        <input
                          type="checkbox"
                          disabled={!ready}
                          checked={ready && selectedSources.includes(source.key)}
                          onChange={() => setSelectedSources((current) => current.includes(source.key) ? current.filter((key) => key !== source.key) : [...current, source.key])}
                        />
                        <div><strong>{source.name}</strong><span>{source.category}</span></div><StatusPill status={source.status} />
                      </label>
                    );
                  })}
                </div>
                <div className="selection-actions"><button className="text-button" onClick={() => setSelectedSources(readySources.map((source) => source.key))}>Select all ready</button><button className="text-button" onClick={() => setSelectedSources([])}>Clear</button></div>
              </Card>

              {samSelected && (
                <Card>
                  <div className="card-heading">
                    <div>
                      <span className="section-kicker">SAM dataset</span>
                      <h2>Upload & Compare SAM Exclusions</h2>
                      <p>No SAM API calls are used. Upload the official Public Exclusions V2 CSV or ZIP and the app immediately compares it against the bidder scope selected above.</p>
                    </div>
                    <ShieldCheck size={22} />
                  </div>
                  <div className="workflow-list">
                    <div className="workflow-step">
                      <div><Database size={14} /></div>
                      <section><strong>Comparison scope</strong><span>{researchScope === "all" ? `All ${researchScopeCount} approved bidders` : `${researchScopeCount} selected bidder${researchScopeCount === 1 ? "" : "s"}`}</span></section>
                    </div>
                    <div className="workflow-step">
                      <div><FileSearch size={14} /></div>
                      <section><strong>Current SAM file</strong><span>{samStatus?.cached_extract ? `${samStatus.cached_extract}${samStatus.cached_extract_date ? ` · ${samStatus.cached_extract_date}` : ""} · ${samStatus.cached_record_count} firm records` : "No SAM extract uploaded yet"}</span></section>
                    </div>
                  </div>
                  {samMessage && <div className="warning-box"><CheckCircle2 size={16} /><span>{samMessage}</span></div>}
                  <div className="button-row">
                    <button
                      className="btn primary"
                      disabled={busy || !dashboard?.active_import || (researchScope === "selected" && !selectedBidderIds.length)}
                      onClick={() => samResearchInput.current?.click()}
                    ><Upload size={16} /> Upload SAM CSV/ZIP & Compare</button>
                    {!dashboard?.active_import && <span className="muted">Import the approved bidder database first.</span>}
                  </div>
                </Card>
              )}

              <Card className="run-card">
                <div>
                  <span className="section-kicker">Step 3</span>
                  <h2>Run research</h2>
                  <p>{researchScope === "all" ? `${dashboard?.bidder_count ?? 0} bidders` : `${selectedBidderIds.length} selected bidders`} × {selectedSources.length} ready source{selectedSources.length === 1 ? "" : "s"}</p>
                  <p className="muted">For SAM, uploading a file above already performs the comparison. This button can rerun the currently cached SAM file without uploading it again.</p>
                </div>
                <button className="btn primary large" disabled={busy || !selectedSources.length || (researchScope === "selected" && !selectedBidderIds.length)} onClick={() => void createRun()}><Play size={17} /> Run Research</button>
              </Card>

              <Card className="run-history">
                <div className="card-heading"><div><span className="section-kicker">History</span><h2>Research Runs</h2></div></div>
                {!runs.length ? <Empty compact title="No research runs yet" text="Completed and partial runs will appear here with their source status." /> : runs.map((run) => <div className="run-row" key={run.id}><div className="run-id">#{run.id}</div><div><strong>{run.bidder_count} bidders · {run.source_count} sources</strong><span>{formatDate(run.created_at)}</span></div><StatusPill status={run.status} /><span className="run-message">{run.message}</span></div>)}
              </Card>
            </div>
          )}

          {page === "review" && (
            <div className="research-layout">
              {samMessage && <div className="warning-box"><CheckCircle2 size={16} /><span>{samMessage}</span></div>}
              <Card>
                <div className="card-heading"><div><span className="section-kicker">Identity review</span><h2>Confirm Company Matches</h2><p>Ambiguous source records stop here until a paralegal decides whether they are the same contractor.</p></div><ShieldCheck size={22} /></div>
                {!identityReviewItems.length ? <Empty compact title="No identity matches waiting" text="Possible company matches that cannot be safely confirmed automatically will appear here." /> : (
                  <div className="workflow-list">
                    {identityReviewItems.map((item) => (
                      <div key={item.snapshot_id} className="choice-card selected" style={{ alignItems: "flex-start" }}>
                        <div style={{ width: "100%" }}>
                          <div className="card-heading">
                            <div><strong>{item.contractor_name}</strong><span>Source: {item.source_key.toUpperCase()} · Run #{item.research_run_id} · {formatDate(item.retrieved_at)}</span></div>
                            <StatusPill status={item.result_status} />
                          </div>
                          {item.warnings?.map((warning) => <div className="warning-box" key={warning}><AlertTriangle size={16} /><span>{warning}</span></div>)}
                          <div className="detail-grid">
                            {item.candidates.map((candidate) => {
                              const record = candidate.record || {};
                              const location = [record.address_1, record.city, record.state, record.zip_code].filter(Boolean).join(", ");
                              return (
                                <div className="detail-field" key={candidate.source_record_id} style={{ alignItems: "flex-start" }}>
                                  <span>SAM record {candidate.source_record_id}</span>
                                  <strong>{record.name || candidate.matched_record_name || "Unnamed SAM record"}</strong>
                                  <small>{location || "No source address"}</small>
                                  <small>{record.exclusion_type || "Exclusion type not supplied"}{record.excluding_agency ? ` · ${record.excluding_agency}` : ""}</small>
                                  <small>Match confidence {formatPercent(candidate.score)} · name {formatPercent(candidate.name_score)} · address {formatPercent(candidate.address_score)}</small>
                                  <div className="button-row" style={{ marginTop: 8 }}>
                                    <button className="btn secondary small" disabled={busy} onClick={() => void resolveIdentity(item.snapshot_id, candidate.source_record_id, "SAME_ENTITY")}><CheckCircle2 size={14} /> Same Company</button>
                                    <button className="btn ghost small" disabled={busy} onClick={() => void resolveIdentity(item.snapshot_id, candidate.source_record_id, "DIFFERENT_ENTITY")}><XCircle size={14} /> Different Company</button>
                                  </div>
                                </div>
                              );
                            })}
                          </div>
                        </div>
                      </div>
                    ))}
                  </div>
                )}
              </Card>

              <Card>
                <div className="card-heading"><div><span className="section-kicker">Data review</span><h2>Proposed Master Changes</h2><p>Confirmed evidence still requires approval before the approved bidder database changes.</p></div><ClipboardCheck size={22} /></div>
                {!reviewItems.filter((item) => item.status === "pending").length ? <Empty compact title="No data changes waiting" text="Confirmed source findings that differ from the master will appear here." /> : (
                  <div className="table-wrap">
                    <table>
                      <thead><tr><th>Bidder</th><th>Field</th><th>Current</th><th>Proposed</th><th>Source</th><th>Evidence</th><th>Actions</th></tr></thead>
                      <tbody>
                        {reviewItems.filter((item) => item.status === "pending").map((item) => (
                          <tr key={item.id}>
                            <td>{item.contractor_name}</td>
                            <td>{prettyField(item.field_name)}</td>
                            <td>{item.current_value || "—"}</td>
                            <td><DataValue value={item.proposed_value} /></td>
                            <td>{item.source_key.toUpperCase()}</td>
                            <td>{item.source_url ? <a href={item.source_url} target="_blank" rel="noreferrer" className="text-button">Source <ExternalLink size={13} /></a> : "Stored snapshot"}</td>
                            <td><div className="button-row"><button className="btn secondary small" disabled={busy} onClick={() => void reviewChange(item.id, "approved")}><CheckCircle2 size={14} /> Approve</button><button className="btn ghost small" disabled={busy} onClick={() => void reviewChange(item.id, "dismissed")}><X size={14} /> Dismiss</button></div></td>
                          </tr>
                        ))}
                      </tbody>
                    </table>
                  </div>
                )}
              </Card>
            </div>
          )}

          {page === "sources" && (
            <div className="sources-grid">
              {sources.map((source) => {
                const ready = source.status === "ready";
                return (
                  <Card key={source.key} className="source-card">
                    <div className="source-card-top"><div className="source-icon"><ServerCog size={19} /></div><StatusPill status={source.status} /></div>
                    <h3>{source.name}</h3><p>{source.category}</p><a href={source.url} target="_blank" rel="noreferrer" className="source-url">{source.url}</a>
                    {source.key === "sam" && (
                      <div className="workflow-list" style={{ marginTop: 14 }}>
                        <div className="workflow-step"><div><ShieldCheck size={14} /></div><section><strong>Public Exclusions V2</strong><span>{samStatus?.cached_extract ? `Loaded: ${samStatus.cached_extract}${samStatus.cached_extract_date ? ` · ${samStatus.cached_extract_date}` : ""} · ${samStatus.cached_record_count} firm records` : "No local extract loaded yet"}</span></section></div>
                        <div className="workflow-step"><div><Upload size={14} /></div><section><strong>Manual file workflow</strong><span>SAM API calls are disabled. Upload the official CSV or ZIP; the app stores it locally and immediately compares it against all approved bidders.</span></section></div>
                        {samMessage && <div className="warning-box"><CheckCircle2 size={16} /><span>{samMessage}</span></div>}
                        <button className="btn secondary" disabled={busy || !dashboard?.active_import} onClick={() => samSourceInput.current?.click()}><Upload size={16} /> Upload SAM CSV/ZIP & Compare</button>
                      </div>
                    )}
                    <div className="source-card-footer"><span>{ready ? "Collector ready" : "Collector not started"}</span><button className="btn ghost small" disabled={!ready} onClick={() => selectSourceOnly(source)}>Configure run</button></div>
                  </Card>
                );
              })}
            </div>
          )}

          {page === "activity" && (
            <Card>
              <div className="card-heading"><div><span className="section-kicker">System activity</span><h2>Diagnostics & Activity</h2><p>Persistent operational events intended for troubleshooting and export.</p></div><div className="button-row"><button className="btn ghost" onClick={() => void clearDiagnostics()}>Clear</button><a className="btn secondary" href={`${API}/api/diagnostics/export`}><Download size={16} /> Export JSON</a></div></div>
              <DiagnosticsList items={diagnostics} />
            </Card>
          )}
        </div>
      </main>

      <input ref={fileInput} type="file" accept=".csv,text/csv" hidden onChange={(event) => void handleFile(event)} />
      <input ref={samSourceInput} type="file" accept=".zip,.csv,application/zip,text/csv" hidden onChange={(event) => void handleSamExtract(event, false)} />
      <input ref={samResearchInput} type="file" accept=".zip,.csv,application/zip,text/csv" hidden onChange={(event) => void handleSamExtract(event, true)} />

      {preview && (
        <div className="modal-backdrop">
          <div className="modal import-modal">
            <div className="modal-header"><div><span className="section-kicker">Import preview</span><h2>{preview.filename}</h2></div><button className="icon-button" onClick={() => setPreview(null)}><X size={20} /></button></div>
            <div className="preview-metrics"><div><strong>{preview.row_count}</strong><span>Bidder rows</span></div><div><strong>{preview.columns.length}</strong><span>Columns detected</span></div><div><strong>{preview.missing_expected_columns.length}</strong><span>Expected fields missing</span></div><div><strong>{preview.extra_columns.length}</strong><span>Additional fields</span></div></div>
            {preview.validation_errors.length > 0 && <div className="warning-box"><AlertTriangle size={17} /><div><strong>Import validation failed.</strong><span>{preview.validation_errors.join(" · ")}</span></div></div>}
            {preview.validation_warnings.length > 0 && <div className="warning-box"><AlertTriangle size={17} /><div><strong>Review these warnings.</strong><span>{preview.validation_warnings.join(" · ")}</span></div></div>}
            <p className="modal-note"><strong>Detected schema:</strong> {preview.columns.map(prettyField).join(" · ")}</p>
            <div className="preview-table table-wrap"><table><thead><tr>{preview.columns.map((column) => <th key={column}>{prettyField(column)}</th>)}</tr></thead><tbody>{preview.preview.map((row, index) => <tr key={index}>{preview.columns.map((column) => <td key={column}>{row[column] || "—"}</td>)}</tr>)}</tbody></table></div>
            <p className="modal-note">Importing creates a snapshot of the original CSV, replaces the active working master, preserves every source column for export, and supersedes stale pending proposals.</p>
            <div className="modal-actions"><button className="btn ghost" onClick={() => setPreview(null)}>Cancel</button><button className="btn primary" disabled={busy || !preview.valid_for_import} onClick={() => void confirmImport()}><CheckCircle2 size={16} /> Confirm Import</button></div>
          </div>
        </div>
      )}

      {selectedBidder && (
        <div className="detail-drawer">
          <div className="drawer-header"><div><span className="section-kicker">Approved bidder · complete record</span><h2>{String(selectedBidder.contractor_name || "Bidder")}</h2><p>ID {String(selectedBidder.id || "—")} · {EXPECTED_BIDDER_FIELDS.length} expected fields</p></div><button className="icon-button" onClick={() => setSelectedBidder(null)}><X size={20} /></button></div>
          {BIDDER_FIELD_GROUPS.map((group) => (
            <section key={group.title}>
              <span className="section-kicker">{group.title}</span>
              <div className="detail-grid">
                {group.fields.map((field) => <div className="detail-field" key={field}><span>{prettyField(field)}</span><strong>{String(selectedBidder[field] || "—")}</strong></div>)}
              </div>
            </section>
          ))}
          {Object.keys(selectedBidder).some((field) => field !== "_internal_id" && !EXPECTED_BIDDER_FIELDS.includes(field as any)) && (
            <section>
              <span className="section-kicker">Additional imported fields</span>
              <div className="detail-grid">
                {Object.entries(selectedBidder)
                  .filter(([field]) => field !== "_internal_id" && !EXPECTED_BIDDER_FIELDS.includes(field as any))
                  .map(([field, value]) => <div className="detail-field" key={field}><span>{prettyField(field)}</span><strong>{String(value || "—")}</strong></div>)}
              </div>
            </section>
          )}
        </div>
      )}

      {diagnosticsOpen && (
        <aside className="diagnostics-drawer">
          <div className="drawer-header"><div><span className="section-kicker">Troubleshooting</span><h2>Diagnostics Console</h2><p>{diagnostics.length} recorded events</p></div><button className="icon-button" onClick={() => setDiagnosticsOpen(false)}><X size={20} /></button></div>
          <div className="diagnostic-actions"><button className="btn ghost small" onClick={() => void refresh()}><RefreshCw size={14} /> Refresh</button><button className="btn ghost small" onClick={() => void clearDiagnostics()}>Clear</button><a className="btn secondary small" href={`${API}/api/diagnostics/export`}><Download size={14} /> Export</a></div>
          <DiagnosticsList items={diagnostics} compact />
        </aside>
      )}

      {busy && <div className="busy-indicator"><RefreshCw size={15} className="spin" /> Working…</div>}
    </div>
  );
}

function Metric({ label, value, hint }: { label: string; value: string | number; hint: string }) {
  return <Card className="metric"><span>{label}</span><strong>{value}</strong><small>{hint}</small></Card>;
}

function WorkflowStep({ number, title, text }: { number: string; title: string; text: string }) {
  return <div className="workflow-step"><div>{number}</div><section><strong>{title}</strong><span>{text}</span></section></div>;
}

function SourceMini({ source }: { source: Source }) {
  return <div className="source-mini"><div><ServerCog size={17} /><strong>{source.name}</strong></div><StatusPill status={source.status} /></div>;
}

function StatusPill({ status }: { status: string }) {
  return <span className={`status-pill status-${status.toLowerCase()}`}>{statusLabel(status)}</span>;
}

function DataValue({ value }: { value: unknown }) {
  const text = String(value || "").trim();
  if (!text) return <span className="data-unknown">—</span>;
  return <span className={text === "Y" ? "data-yes" : text === "N" ? "data-no" : "data-text"}>{text}</span>;
}

function Empty({ title, text, compact = false }: { title: string; text: string; compact?: boolean }) {
  return <div className={compact ? "empty compact" : "empty"}><div className="empty-icon"><FileSearch size={22} /></div><strong>{title}</strong><span>{text}</span></div>;
}

function DiagnosticsList({ items, compact = false }: { items: Diagnostic[]; compact?: boolean }) {
  if (!items.length) return <Empty compact title="No diagnostic events" text="Imports, research runs, source failures, and workflow events will be recorded here." />;
  return <div className={compact ? "diagnostics-list compact" : "diagnostics-list"}>{items.map((item) => (
    <div className="diagnostic-row" key={item.id}>
      <div className={`severity severity-${item.severity.toLowerCase()}`}>{item.severity}</div>
      <div className="diagnostic-body"><strong>{item.message}</strong><span>{[item.stage, item.source_key, item.bidder_name].filter(Boolean).join(" · ") || "Application"}</span><small>{formatDate(item.timestamp)}</small>{item.details && Object.keys(item.details).length > 0 && <code>{JSON.stringify(item.details)}</code>}</div>
    </div>
  ))}</div>;
}
