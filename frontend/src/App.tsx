import {
  Activity,
  AlertTriangle,
  CheckCircle2,
  ChevronRight,
  ClipboardCheck,
  Database,
  Download,
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
} from "lucide-react";
import { ChangeEvent, ReactNode, useEffect, useMemo, useRef, useState } from "react";

type Page = "dashboard" | "database" | "research" | "review" | "sources" | "activity";
type Bidder = Record<string, string | number> & { _internal_id: number };
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
  active_import: null | { filename: string; imported_at: string; row_count: number; columns: string[] };
  last_run: Run | null;
};
type ImportPreview = {
  filename: string;
  row_count: number;
  columns: string[];
  missing_expected_columns: string[];
  extra_columns: string[];
  preview: Record<string, string>[];
};

const API = import.meta.env.VITE_API_URL || "";

async function request<T>(path: string, options?: RequestInit): Promise<T> {
  const response = await fetch(`${API}${path}`, options);
  if (!response.ok) {
    let message = `Request failed (${response.status})`;
    try {
      const body = await response.json();
      message = body.detail || message;
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
  const [reviewItems, setReviewItems] = useState<any[]>([]);
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
  const fileInput = useRef<HTMLInputElement>(null);

  const refresh = async () => {
    setError("");
    try {
      const [dashboardData, sourceData, bidderData, runData, reviewData, diagnosticData] = await Promise.all([
        request<DashboardData>("/api/dashboard"),
        request<{ items: Source[] }>("/api/sources"),
        request<{ items: Bidder[]; total: number }>(`/api/bidders?search=${encodeURIComponent(search)}&limit=250`),
        request<{ items: Run[] }>("/api/runs"),
        request<{ items: any[] }>("/api/review"),
        request<{ items: Diagnostic[] }>("/api/diagnostics"),
      ]);
      setDashboard(dashboardData);
      setSources(sourceData.items);
      setBidders(bidderData.items);
      setBidderTotal(bidderData.total);
      setRuns(runData.items);
      setReviewItems(reviewData.items);
      setDiagnostics(diagnosticData.items);
      setSelectedSources((current) => current.length ? current : sourceData.items.map((source) => source.key));
    } catch (err) {
      setError(err instanceof Error ? err.message : "Unable to load application data.");
    }
  };

  useEffect(() => {
    void refresh();
  }, []);

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
    if (!uploadFile) return;
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
      setError(err instanceof Error ? err.message : "Unable to create research run.");
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

  const selectSourceOnly = (key: string) => {
    setSelectedSources([key]);
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

  const selectedSourceNames = useMemo(
    () => sources.filter((source) => selectedSources.includes(source.key)).map((source) => source.name),
    [sources, selectedSources]
  );

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
                <Metric label="Research sources" value={dashboard?.source_count ?? 14} hint={`${dashboard?.implemented_source_count ?? 0} collectors implemented`} />
                <Metric label="Pending review" value={dashboard?.pending_review_count ?? 0} hint="Human approval required" />
                <Metric label="Last research run" value={dashboard?.last_run ? `#${dashboard.last_run.id}` : "—"} hint={dashboard?.last_run ? formatDate(dashboard.last_run.created_at) : "No runs yet"} />
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
                    <WorkflowStep number="1" title="Research" text="Run selected sources against approved bidders only." />
                    <WorkflowStep number="2" title="Compare" text="Keep findings separate and compare them to current approved values." />
                    <WorkflowStep number="3" title="Review" text="Approve or dismiss proposed changes with evidence visible." />
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
                <div><span className="section-kicker">Approved master</span><h2>Bidder Database</h2><p>{bidderTotal} records · CSV is the import/export format; SQLite is the working store.</p></div>
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
                <div className="card-heading"><div><span className="section-kicker">Step 2</span><h2>Choose sources</h2></div><ServerCog size={21} /></div>
                <div className="source-checklist">
                  {sources.map((source) => (
                    <label key={source.key} className="source-check-row">
                      <input type="checkbox" checked={selectedSources.includes(source.key)} onChange={() => setSelectedSources((current) => current.includes(source.key) ? current.filter((key) => key !== source.key) : [...current, source.key])} />
                      <div><strong>{source.name}</strong><span>{source.category}</span></div><StatusPill status={source.status} />
                    </label>
                  ))}
                </div>
                <div className="selection-actions"><button className="text-button" onClick={() => setSelectedSources(sources.map((s) => s.key))}>Select all</button><button className="text-button" onClick={() => setSelectedSources([])}>Clear</button></div>
              </Card>
              <Card className="run-card">
                <div><span className="section-kicker">Step 3</span><h2>Create research run</h2><p>{researchScope === "all" ? `${dashboard?.bidder_count ?? 0} bidders` : `${selectedBidderIds.length} selected bidders`} × {selectedSources.length} sources</p><p className="muted">Collectors are intentionally not implemented yet. This creates the persisted workflow object and diagnostic trail only.</p></div>
                <button className="btn primary large" disabled={busy || !selectedSources.length || (researchScope === "selected" && !selectedBidderIds.length)} onClick={() => void createRun()}><Play size={17} /> Create Run</button>
              </Card>
              <Card className="run-history">
                <div className="card-heading"><div><span className="section-kicker">History</span><h2>Research Runs</h2></div></div>
                {!runs.length ? <Empty compact title="No research runs yet" text="Configured runs will appear here with their status and source scope." /> : runs.map((run) => <div className="run-row" key={run.id}><div className="run-id">#{run.id}</div><div><strong>{run.bidder_count} bidders · {run.source_count} sources</strong><span>{formatDate(run.created_at)}</span></div><StatusPill status={run.status} /><span className="run-message">{run.message}</span></div>)}
              </Card>
            </div>
          )}

          {page === "review" && (
            <Card>
              <div className="card-heading"><div><span className="section-kicker">Human approval</span><h2>Comparison Review Queue</h2><p>Research findings never overwrite the approved master automatically.</p></div><ClipboardCheck size={22} /></div>
              {!reviewItems.length ? <Empty title="No changes waiting for review" text="Once source collectors are added, new or different findings will appear here with the current value, proposed value, source evidence, and approve/dismiss controls." /> : (
                <div className="table-wrap"><table><thead><tr><th>Bidder</th><th>Field</th><th>Current</th><th>Proposed</th><th>Source</th><th>Status</th></tr></thead><tbody>{reviewItems.map((item) => <tr key={item.id}><td>{item.contractor_name}</td><td>{prettyField(item.field_name)}</td><td>{item.current_value || "—"}</td><td>{item.proposed_value || "—"}</td><td>{item.source_key}</td><td><StatusPill status={item.status} /></td></tr>)}</tbody></table></div>
              )}
            </Card>
          )}

          {page === "sources" && (
            <div className="sources-grid">
              {sources.map((source) => (
                <Card key={source.key} className="source-card">
                  <div className="source-card-top"><div className="source-icon"><ServerCog size={19} /></div><StatusPill status={source.status} /></div>
                  <h3>{source.name}</h3><p>{source.category}</p><a href={source.url} target="_blank" rel="noreferrer" className="source-url">{source.url}</a>
                  <div className="source-card-footer"><span>Collector not started</span><button className="btn ghost small" onClick={() => selectSourceOnly(source.key)}>Configure run</button></div>
                </Card>
              ))}
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

      {preview && (
        <div className="modal-backdrop">
          <div className="modal import-modal">
            <div className="modal-header"><div><span className="section-kicker">Import preview</span><h2>{preview.filename}</h2></div><button className="icon-button" onClick={() => setPreview(null)}><X size={20} /></button></div>
            <div className="preview-metrics"><div><strong>{preview.row_count}</strong><span>Bidder rows</span></div><div><strong>{preview.columns.length}</strong><span>Columns detected</span></div><div><strong>{preview.missing_expected_columns.length}</strong><span>Expected fields missing</span></div><div><strong>{preview.extra_columns.length}</strong><span>Additional fields</span></div></div>
            {preview.missing_expected_columns.length > 0 && <div className="warning-box"><AlertTriangle size={17} /><div><strong>Some example-schema fields are not present.</strong><span>{preview.missing_expected_columns.join(", ")}</span></div></div>}
            <div className="preview-table table-wrap"><table><thead><tr>{preview.columns.slice(0, 7).map((column) => <th key={column}>{prettyField(column)}</th>)}</tr></thead><tbody>{preview.preview.map((row, index) => <tr key={index}>{preview.columns.slice(0, 7).map((column) => <td key={column}>{row[column] || "—"}</td>)}</tr>)}</tbody></table></div>
            <p className="modal-note">Importing creates a snapshot of the original CSV, replaces the active working master, preserves the source columns for export, and clears stale review proposals.</p>
            <div className="modal-actions"><button className="btn ghost" onClick={() => setPreview(null)}>Cancel</button><button className="btn primary" disabled={busy} onClick={() => void confirmImport()}><CheckCircle2 size={16} /> Confirm Import</button></div>
          </div>
        </div>
      )}

      {selectedBidder && (
        <div className="detail-drawer">
          <div className="drawer-header"><div><span className="section-kicker">Approved bidder</span><h2>{String(selectedBidder.contractor_name || "Bidder")}</h2><p>ID {String(selectedBidder.id || "—")}</p></div><button className="icon-button" onClick={() => setSelectedBidder(null)}><X size={20} /></button></div>
          <div className="detail-grid">
            {Object.entries(selectedBidder).filter(([key]) => key !== "_internal_id").map(([key, value]) => <div className="detail-field" key={key}><span>{prettyField(key)}</span><strong>{String(value || "—")}</strong></div>)}
          </div>
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
  return <span className={`status-pill status-${status}`}>{statusLabel(status)}</span>;
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
