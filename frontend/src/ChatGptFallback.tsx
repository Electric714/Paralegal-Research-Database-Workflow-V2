import {
  AlertTriangle,
  CheckCircle2,
  Clipboard,
  Download,
  ExternalLink,
  FileSearch,
  Upload,
  X,
} from "lucide-react";
import { ChangeEvent, useMemo, useRef, useState } from "react";

type Source = { key: string; name: string; url: string; category: string; status: string };
type DashboardData = {
  bidder_count: number;
  active_import: null | { filename: string; row_count: number };
};
type Bidder = Record<string, string | number | boolean> & { _internal_id: number };
type SourceFieldMapping = { owned_fields: string[]; notes: string };
type SchemaData = { source_field_mappings: Record<string, SourceFieldMapping> };
type ChatGptResult = {
  bidder_id?: string | number;
  contractor_name?: string;
  status?: string;
  findings?: Record<string, unknown>;
  source_urls?: string[];
  evidence_summary?: string;
  notes?: string;
};
type ChatGptPayload = {
  source_key?: string;
  source_name?: string;
  results?: ChatGptResult[];
};
type ComparisonDifference = {
  bidderId: string;
  contractorName: string;
  field: string;
  currentValue: string;
  chatGptValue: string;
  sourceUrls: string[];
  evidenceSummary: string;
};
type Comparison = {
  source: Source;
  fileName: string;
  returnedResults: number;
  expectedBidders: number;
  matchedBidders: number;
  missingBidders: string[];
  unknownResults: string[];
  ignoredFields: string[];
  statusCounts: Record<string, number>;
  differences: ComparisonDifference[];
  evidenceOnly: boolean;
};

const API = import.meta.env.VITE_API_URL || "";
const CHATGPT_URL = "https://chatgpt.com/";

const OFFICIAL_URL_OVERRIDES: Record<string, string> = {
  wcca: "https://wcca.wicourts.gov/index.xsl",
};

async function request<T>(path: string): Promise<T> {
  const response = await fetch(`${API}${path}`);
  if (!response.ok) throw new Error(`Request failed (${response.status})`);
  return response.json() as Promise<T>;
}

function normalize(value: unknown): string {
  if (value === null || value === undefined) return "";
  if (typeof value === "string") return value.trim();
  if (typeof value === "number" || typeof value === "boolean") return String(value).trim();
  return JSON.stringify(value);
}

function officialUrl(source: Source): string {
  return OFFICIAL_URL_OVERRIDES[source.key] || source.url;
}

function buildPrompt(source: Source, bidderCount: number, mapping?: SourceFieldMapping): string {
  const fields = mapping?.owned_fields || [];
  const fieldsText = fields.length
    ? fields.map((field) => `- ${field}`).join("\n")
    : "- None. This source is evidence-only in the current database, so findings must stay in evidence_summary and findings must be {}.";
  const mappingNote = mapping?.notes || "No source-to-master-field ownership is currently defined.";

  return `You are performing a manual fallback research run for the Paralegal Research Database Workflow V2.

SOURCE TO RESEARCH
Name: ${source.name}
Primary source: ${officialUrl(source)}
Category: ${source.category}
Source key: ${source.key}

INPUT
I will attach the current approved bidder database CSV. It contains ${bidderCount} contractor/bidder records. Research EVERY row in that CSV against the source above. Use the CSV's exact bidder id and contractor_name in your output. Related companies, addresses, city/state, and other identity fields may be used to confirm that a source record belongs to the correct bidder.

RESEARCH RULES
1. Search only for the bidders already present in the attached CSV. Do not discover or add unrelated companies.
2. Use the named source above as the authoritative research target. You may use search engines to locate pages from that source, but do not substitute unrelated third-party information for the named source.
3. Match identities conservatively. A similar company name by itself is not enough. Use address, location, related-company names, and other available identifiers when possible.
4. Never guess. If identity is uncertain, use status AMBIGUOUS.
5. A blocked page, CAPTCHA, login wall, incomplete pagination, timeout, or inaccessible result is PARTIAL, BLOCKED, or FAILED — never NO_MATCH.
6. Use NO_MATCH only when you actually completed a reasonable search of the source for that bidder and found no applicable record.
7. Preserve source URLs for positive findings whenever possible.
8. Research all ${bidderCount} bidders. Do not stop after finding a few matches.
9. Do not modify the attached CSV and do not claim that the master database was changed.

MASTER FIELDS THIS SOURCE IS CURRENTLY ALLOWED TO COMPARE
${fieldsText}

Field-ownership note: ${mappingNote}

For a confirmed MATCH, put only the allowed master fields above in the findings object. If this source owns no fields, findings must be {} and the useful information goes in evidence_summary. Do not invent a field mapping just because something sounds related.

REQUIRED STATUS VALUES
MATCH
NO_MATCH
AMBIGUOUS
PARTIAL
BLOCKED
FAILED

REQUIRED MACHINE-READABLE RESULT
When research is complete, create a downloadable JSON file named ${source.key}_chatgpt_research.json using EXACTLY this top-level structure:

{
  "source_key": "${source.key}",
  "source_name": "${source.name}",
  "results": [
    {
      "bidder_id": "EXACT id FROM CSV",
      "contractor_name": "EXACT contractor_name FROM CSV",
      "status": "MATCH | NO_MATCH | AMBIGUOUS | PARTIAL | BLOCKED | FAILED",
      "findings": {},
      "source_urls": [],
      "evidence_summary": "Concise description of what was found or why the search was incomplete",
      "notes": "Optional identity or research notes"
    }
  ]
}

There must be exactly one result object for every bidder row in the attached CSV. Keep bidder_id unchanged from the CSV. The JSON file is what the local V2 program will import and compare against the approved master.

Also create a human-readable Markdown report named ${source.key}_chatgpt_research_report.md summarizing the same research for manual review. The JSON and Markdown must agree with each other.

Do not return only prose in chat. Finish by providing both downloadable files.`;
}

function buildComparisonMarkdown(comparison: Comparison): string {
  const lines = [
    `# ChatGPT Fallback Comparison — ${comparison.source.name}`,
    "",
    `Returned file: ${comparison.fileName}`,
    `Expected bidders: ${comparison.expectedBidders}`,
    `Returned results: ${comparison.returnedResults}`,
    `Matched to master: ${comparison.matchedBidders}`,
    `Differences found: ${comparison.differences.length}`,
    "",
    "## Status Counts",
    "",
    ...Object.entries(comparison.statusCounts).sort(([a], [b]) => a.localeCompare(b)).map(([status, count]) => `- ${status}: ${count}`),
    "",
    "## Master-field Differences",
    "",
  ];

  if (!comparison.differences.length) {
    lines.push("No allowed master-field differences were detected.");
  } else {
    for (const item of comparison.differences) {
      lines.push(`### ${item.contractorName} (${item.bidderId})`);
      lines.push(`- Field: ${item.field}`);
      lines.push(`- Current master: ${item.currentValue || "(blank)"}`);
      lines.push(`- ChatGPT finding: ${item.chatGptValue || "(blank)"}`);
      if (item.evidenceSummary) lines.push(`- Evidence: ${item.evidenceSummary}`);
      if (item.sourceUrls.length) lines.push(`- Sources: ${item.sourceUrls.join(", ")}`);
      lines.push("");
    }
  }

  if (comparison.evidenceOnly) {
    lines.push("## Evidence-only source");
    lines.push("");
    lines.push("This source currently owns no master fields in V2, so the imported ChatGPT result was reviewed for coverage/status only and did not create field differences.");
    lines.push("");
  }

  if (comparison.missingBidders.length) {
    lines.push("## Missing bidder results");
    lines.push("");
    comparison.missingBidders.forEach((item) => lines.push(`- ${item}`));
    lines.push("");
  }

  if (comparison.unknownResults.length) {
    lines.push("## Returned results not found in current master");
    lines.push("");
    comparison.unknownResults.forEach((item) => lines.push(`- ${item}`));
    lines.push("");
  }

  if (comparison.ignoredFields.length) {
    lines.push("## Ignored fields");
    lines.push("");
    comparison.ignoredFields.forEach((item) => lines.push(`- ${item}`));
    lines.push("");
  }

  lines.push("---");
  lines.push("This comparison is read-only. It does not modify the approved bidder database.");
  return lines.join("\n");
}

function downloadText(filename: string, text: string, type: string) {
  const blob = new Blob([text], { type });
  const url = URL.createObjectURL(blob);
  const anchor = document.createElement("a");
  anchor.href = url;
  anchor.download = filename;
  document.body.appendChild(anchor);
  anchor.click();
  anchor.remove();
  URL.revokeObjectURL(url);
}

export default function ChatGptFallback() {
  const [open, setOpen] = useState(false);
  const [sources, setSources] = useState<Source[]>([]);
  const [dashboard, setDashboard] = useState<DashboardData | null>(null);
  const [schema, setSchema] = useState<SchemaData | null>(null);
  const [bidders, setBidders] = useState<Bidder[]>([]);
  const [activeSource, setActiveSource] = useState<Source | null>(null);
  const [prompt, setPrompt] = useState("");
  const [message, setMessage] = useState("");
  const [error, setError] = useState("");
  const [busy, setBusy] = useState(false);
  const [comparison, setComparison] = useState<Comparison | null>(null);
  const resultInput = useRef<HTMLInputElement>(null);

  const mapping = useMemo(
    () => (activeSource && schema ? schema.source_field_mappings[activeSource.key] : undefined),
    [activeSource, schema],
  );

  const load = async () => {
    setBusy(true);
    setError("");
    try {
      const [sourceData, dashboardData, schemaData, bidderData] = await Promise.all([
        request<{ items: Source[] }>("/api/sources"),
        request<DashboardData>("/api/dashboard"),
        request<SchemaData>("/api/schema"),
        request<{ items: Bidder[]; total: number }>("/api/bidders?limit=500"),
      ]);
      setSources(sourceData.items);
      setDashboard(dashboardData);
      setSchema(schemaData);
      setBidders(bidderData.items);
      if (bidderData.total > bidderData.items.length) {
        setError(`The manual ChatGPT fallback currently loads the first ${bidderData.items.length} bidders. The active master contains ${bidderData.total}.`);
      }
    } catch (err) {
      setError(err instanceof Error ? err.message : "Unable to load ChatGPT fallback data.");
    } finally {
      setBusy(false);
    }
  };

  const show = () => {
    setOpen(true);
    if (!sources.length) void load();
  };

  const launchChatGpt = async (source: Source) => {
    setError("");
    setMessage("");
    setComparison(null);
    setActiveSource(source);

    if (!dashboard?.active_import) {
      setError("Import the approved bidder database before using the ChatGPT fallback.");
      return;
    }

    const sourceMapping = schema?.source_field_mappings[source.key];
    const generatedPrompt = buildPrompt(source, dashboard.bidder_count, sourceMapping);
    setPrompt(generatedPrompt);

    window.open(CHATGPT_URL, "_blank", "noopener,noreferrer");

    const exportLink = document.createElement("a");
    exportLink.href = `${API}/api/export`;
    document.body.appendChild(exportLink);
    exportLink.click();
    exportLink.remove();

    try {
      await navigator.clipboard.writeText(generatedPrompt);
      setMessage(`Opened ChatGPT, downloaded the current master CSV, and copied the ${source.name} prompt. Upload the CSV in ChatGPT, paste the prompt, then import the returned JSON here.`);
    } catch {
      setMessage(`Opened ChatGPT and downloaded the current master CSV. Clipboard access was blocked, so use Copy Prompt below before pasting into ChatGPT.`);
    }
  };

  const copyPrompt = async () => {
    if (!prompt) return;
    try {
      await navigator.clipboard.writeText(prompt);
      setMessage("Prompt copied to clipboard.");
    } catch {
      setError("The browser blocked clipboard access. Select the prompt text below and copy it manually.");
    }
  };

  const chooseResultFor = (source: Source) => {
    setActiveSource(source);
    setComparison(null);
    setError("");
    resultInput.current?.click();
  };

  const compareResult = async (event: ChangeEvent<HTMLInputElement>) => {
    const file = event.target.files?.[0];
    event.target.value = "";
    if (!file || !activeSource) return;
    setBusy(true);
    setError("");
    setMessage("");
    try {
      const payload = JSON.parse(await file.text()) as ChatGptPayload;
      if (payload.source_key !== activeSource.key) {
        throw new Error(`This file says source_key=${payload.source_key || "(missing)"}, but you selected ${activeSource.key}.`);
      }
      if (!Array.isArray(payload.results)) throw new Error("The returned JSON does not contain a results array.");

      const masterById = new Map(bidders.map((bidder) => [normalize(bidder.id), bidder]));
      const masterIds = new Set(masterById.keys());
      const seenIds = new Set<string>();
      const unknownResults: string[] = [];
      const ignoredFields = new Set<string>();
      const differences: ComparisonDifference[] = [];
      const statusCounts: Record<string, number> = {};
      let matchedBidders = 0;
      const allowedFields = new Set(mapping?.owned_fields || []);

      for (const result of payload.results) {
        const bidderId = normalize(result.bidder_id);
        const status = normalize(result.status).toUpperCase() || "UNKNOWN";
        statusCounts[status] = (statusCounts[status] || 0) + 1;
        if (!bidderId || !masterById.has(bidderId)) {
          unknownResults.push(`${bidderId || "(missing id)"} — ${result.contractor_name || "Unnamed result"}`);
          continue;
        }
        if (seenIds.has(bidderId)) {
          unknownResults.push(`${bidderId} — duplicate result row`);
          continue;
        }
        seenIds.add(bidderId);
        matchedBidders += 1;
        const bidder = masterById.get(bidderId)!;

        const findings = result.findings && typeof result.findings === "object" ? result.findings : {};
        for (const [field, rawValue] of Object.entries(findings)) {
          if (!allowedFields.has(field)) {
            ignoredFields.add(`${bidderId} ${field} — source does not own this master field`);
            continue;
          }
          if (status !== "MATCH") {
            ignoredFields.add(`${bidderId} ${field} — findings ignored because status is ${status}, not MATCH`);
            continue;
          }
          const currentValue = normalize(bidder[field]);
          const chatGptValue = normalize(rawValue);
          if (chatGptValue !== currentValue) {
            differences.push({
              bidderId,
              contractorName: normalize(bidder.contractor_name) || normalize(result.contractor_name) || bidderId,
              field,
              currentValue,
              chatGptValue,
              sourceUrls: Array.isArray(result.source_urls) ? result.source_urls.map(normalize).filter(Boolean) : [],
              evidenceSummary: normalize(result.evidence_summary),
            });
          }
        }
      }

      const missingBidders = bidders
        .filter((bidder) => !seenIds.has(normalize(bidder.id)))
        .map((bidder) => `${normalize(bidder.id)} — ${normalize(bidder.contractor_name)}`);

      const result: Comparison = {
        source: activeSource,
        fileName: file.name,
        returnedResults: payload.results.length,
        expectedBidders: masterIds.size,
        matchedBidders,
        missingBidders,
        unknownResults,
        ignoredFields: [...ignoredFields],
        statusCounts,
        differences,
        evidenceOnly: allowedFields.size === 0,
      };
      setComparison(result);
      setMessage(`Compared ${matchedBidders} returned bidder results with the current master. ${differences.length} allowed master-field difference${differences.length === 1 ? "" : "s"} found. Nothing was written to the database.`);
    } catch (err) {
      setError(err instanceof Error ? err.message : "Unable to compare the returned ChatGPT JSON.");
    } finally {
      setBusy(false);
    }
  };

  const downloadComparison = () => {
    if (!comparison) return;
    downloadText(
      `${comparison.source.key}_chatgpt_comparison.md`,
      buildComparisonMarkdown(comparison),
      "text/markdown;charset=utf-8",
    );
  };

  return (
    <>
      <button className="chatgpt-fallback-launcher" onClick={show} title="Manual ChatGPT research fallback">
        <FileSearch size={17} /> ChatGPT Fallback
      </button>

      {open && (
        <div className="chatgpt-fallback-backdrop">
          <section className="chatgpt-fallback-modal">
            <header>
              <div>
                <span className="chatgpt-fallback-kicker">Manual test workflow · no API</span>
                <h2>Search with ChatGPT</h2>
                <p>Pick a source. V2 downloads the current approved CSV and copies a source-specific prompt. When ChatGPT finishes, import its JSON file here to compare it with the master.</p>
              </div>
              <button className="chatgpt-fallback-icon" onClick={() => setOpen(false)}><X size={19} /></button>
            </header>

            {error && <div className="chatgpt-fallback-alert error"><AlertTriangle size={16} /><span>{error}</span></div>}
            {message && <div className="chatgpt-fallback-alert success"><CheckCircle2 size={16} /><span>{message}</span></div>}

            <div className="chatgpt-fallback-summary">
              <div><strong>{dashboard?.bidder_count ?? 0}</strong><span>approved bidders</span></div>
              <div><strong>{sources.length}</strong><span>configured sources</span></div>
              <div><strong>{dashboard?.active_import?.filename || "No CSV"}</strong><span>current master</span></div>
            </div>

            <div className="chatgpt-fallback-source-list">
              {sources.map((source) => (
                <div className={activeSource?.key === source.key ? "chatgpt-fallback-source active" : "chatgpt-fallback-source"} key={source.key}>
                  <div>
                    <strong>{source.name}</strong>
                    <span>{source.category}</span>
                    <a href={officialUrl(source)} target="_blank" rel="noreferrer">Source <ExternalLink size={12} /></a>
                  </div>
                  <div className="chatgpt-fallback-actions">
                    <button disabled={busy || !dashboard?.active_import} onClick={() => void launchChatGpt(source)}><ExternalLink size={14} /> Use ChatGPT</button>
                    <button disabled={busy || !dashboard?.active_import} onClick={() => chooseResultFor(source)}><Upload size={14} /> Compare Result</button>
                  </div>
                </div>
              ))}
            </div>

            {activeSource && prompt && (
              <div className="chatgpt-fallback-prompt">
                <div className="chatgpt-fallback-section-heading">
                  <div><strong>{activeSource.name} prompt</strong><span>{mapping?.owned_fields?.length ? `Mapped fields: ${mapping.owned_fields.join(", ")}` : "Evidence-only source; no mapped master fields"}</span></div>
                  <button onClick={() => void copyPrompt()}><Clipboard size={14} /> Copy Prompt</button>
                </div>
                <textarea readOnly value={prompt} />
              </div>
            )}

            {comparison && (
              <div className="chatgpt-fallback-comparison">
                <div className="chatgpt-fallback-section-heading">
                  <div><strong>Comparison result</strong><span>{comparison.fileName}</span></div>
                  <button onClick={downloadComparison}><Download size={14} /> Download Report</button>
                </div>
                <div className="chatgpt-fallback-summary compact">
                  <div><strong>{comparison.returnedResults}</strong><span>returned</span></div>
                  <div><strong>{comparison.matchedBidders}</strong><span>matched to master</span></div>
                  <div><strong>{comparison.differences.length}</strong><span>field differences</span></div>
                  <div><strong>{comparison.missingBidders.length}</strong><span>missing bidders</span></div>
                </div>
                {comparison.differences.length > 0 ? (
                  <div className="chatgpt-fallback-differences">
                    {comparison.differences.map((item, index) => (
                      <div key={`${item.bidderId}-${item.field}-${index}`}>
                        <strong>{item.contractorName}</strong>
                        <span>{item.field}: <code>{item.currentValue || "(blank)"}</code> → <code>{item.chatGptValue || "(blank)"}</code></span>
                        {item.evidenceSummary && <small>{item.evidenceSummary}</small>}
                      </div>
                    ))}
                  </div>
                ) : (
                  <div className="chatgpt-fallback-alert success"><CheckCircle2 size={16} /><span>No allowed master-field differences detected. {comparison.evidenceOnly ? "This source is currently evidence-only in V2." : ""}</span></div>
                )}
                {(comparison.missingBidders.length > 0 || comparison.unknownResults.length > 0 || comparison.ignoredFields.length > 0) && (
                  <div className="chatgpt-fallback-alert warning"><AlertTriangle size={16} /><span>{comparison.missingBidders.length} missing bidder result(s), {comparison.unknownResults.length} unknown/duplicate result(s), and {comparison.ignoredFields.length} ignored field finding(s). Download the comparison report for details.</span></div>
                )}
                <p className="chatgpt-fallback-safety">Read-only comparison only. ChatGPT results never update the approved master database in this test workflow.</p>
              </div>
            )}

            {!dashboard?.active_import && !busy && <div className="chatgpt-fallback-alert warning"><AlertTriangle size={16} /><span>Import the approved bidder CSV in V2 before using this workflow.</span></div>}
          </section>
        </div>
      )}

      <input ref={resultInput} hidden type="file" accept=".json,application/json" onChange={(event) => void compareResult(event)} />
    </>
  );
}
