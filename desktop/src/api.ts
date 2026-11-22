export interface ScanRun {
  run_id: string;
  state: "running" | "done" | "failed";
  files_seen: number;
  files_new: number;
  files_updated: number;
  files_moved: number;
  files_missing: number;
  errors: number;
  message: string | null;
  started_at: string;
  finished_at: string | null;
}

export interface SourceStats {
  counts: Record<string, number>;
  available: number;
  stale: number;
  missing: number;
  offline: number;
  last_run: ScanRun | null;
}

export interface Source {
  source_id: string;
  root_uri: string;
  label: string;
  state: "active" | "paused";
  health: string;
  last_scan_at: string | null;
  excludes: string;
  stats: SourceStats;
  scanning: boolean;
}

export interface SearchItem {
  location_id: string;
  asset_id: string;
  name: string;
  path: string;
  directory: string;
  rel_path: string;
  ext: string;
  size: number;
  mtime: string | null;
  availability: string;
  index_level: string;
  semantic: boolean;
  match: "name" | "content" | "both" | "semantic";
  snippet: string | null;
  page: number | null;
  sheet: string | null;
  bbox: number[][] | null;
  source_id: string;
  source_label: string;
}

export interface SearchResponse {
  query: string;
  total: number;
  items: SearchItem[];
  index_level: string;
  content_hits: number;
  truncated: boolean;
  rewritten: string | null;
  vector_used: boolean;
  vector_model: string | null;
  vector_error: string | null;
}

export interface IndexJob {
  job_id: string;
  source_id: string;
  version_id: string;
  asset_id: string;
  stage: "l1" | "l2";
  status: string;
  attempts: number;
  error: string | null;
  updated_at: string;
  path: string | null;
  ext: string;
  name: string | null;
}

export interface MaintenanceInfo {
  jobs: Record<string, number>;
  chunks: number;
  artifacts: number;
  db_bytes: number;
  running: {
    running: boolean;
    kind: string | null;
    source_id: string | null;
    started_at: string | null;
    message: string | null;
  };
  budget: {
    date: string;
    l1_used: number;
    l1_limit: number;
    l2_used: number;
    l2_limit: number;
  };
  vectors: number;
  embedding: {
    provider: "none" | "openai" | "ollama";
    base_url: string;
    model: string;
    allow_cloud: boolean;
    top_k: number;
    has_key: boolean;
  };
}

export interface OverviewStats {
  files: { available: number; missing: number; offline: number; stale: number };
  by_ext: { ext: string; count: number }[];
  sources: {
    source_id: string;
    label: string;
    root_uri: string;
    state: string;
    health: string;
    last_scan_at: string | null;
    available: number;
    stale: number;
  }[];
  coverage: {
    directory_level: string;
    directory_indexed: number;
    content_level: string;
    content_indexed: number;
    content_pending: number;
    content_empty: number;
    content_failed: number;
    content_unsupported: number;
    low_quality: number;
    ocr_indexed: number;
    ocr_empty: number;
    ocr_failed: number;
    ocr_pending: number;
    mixed_pdf: number;
    ocr_policy: "auto" | "windows" | "rapidocr";
    ocr_available: boolean;
    ocr_engine: string;
    ocr_languages: string[];
    ocr_message: string | null;
  };
  maintenance: MaintenanceInfo;
}

export interface Citation {
  n: number;
  chunk_id: string;
  location_id: string;
  name: string;
  path: string;
  page: number | null;
  sheet: string | null;
  snippet: string;
  ocr: boolean;
  semantic: boolean;
  version_id: string;
  mtime: string | null;
  bbox: number[][] | null;
}

export interface AnswerResult {
  question: string;
  answer: string;
  refused: boolean;
  reason: string | null;
  provider: "extractive" | "openai" | "ollama";
  model: string | null;
  citations: Citation[];
  evidence_count: number;
  warning: string | null;
  session_id?: string | null;
  coverage: {
    content_indexed: number;
    content_pending: number;
    content_empty: number;
    content_failed: number;
    ocr_indexed: number;
    ocr_pending: number;
  };
}

export interface ChatSession {
  session_id: string;
  title: string;
  created_at: string;
  updated_at: string;
  message_count: number;
}

export interface ChatMessage {
  message_id: string;
  role: "user" | "assistant";
  content: string;
  created_at: string;
  payload: AnswerResult | null;
}

export interface AnswerSettings {
  provider: "extractive" | "openai" | "ollama";
  base_url: string;
  model: string;
  allow_cloud: boolean;
  top_k: number;
  has_key: boolean;
}

export interface EmbeddingSettings {
  provider: "none" | "openai" | "ollama";
  base_url: string;
  model: string;
  allow_cloud: boolean;
  top_k: number;
  has_key: boolean;
}

export interface QuerySettings {
  custom: string[][];
  builtin_groups: number;
}

export interface OcrEngineInfo {
  available: boolean;
  engine: string;
  label: string;
  languages: string[];
  message: string | null;
}

export interface OcrSettings {
  policy: "auto" | "windows" | "rapidocr";
  engines: OcrEngineInfo[];
}

export interface MaintenanceEvent {
  event_id: number;
  type: string;
  source_id: string | null;
  detail: string | null;
  outcome: string | null;
  timestamp: string;
}

// 安装版由 Tauri 资源服务器加载，相对 /api 会返回 index.html；直接访问本机核心。
// 开发模式下同样可用（FastAPI CORS 允许 localhost:1420）。
const API_BASE = "http://127.0.0.1:8737";

async function request<T>(path: string, init?: RequestInit): Promise<T> {
  const response = await fetch(`${API_BASE}${path}`, {
    ...init,
    headers: { "Content-Type": "application/json", ...(init?.headers ?? {}) },
  });
  if (!response.ok) {
    let detail = `${response.status} ${response.statusText}`;
    try {
      const body = await response.json();
      if (body?.detail) detail = typeof body.detail === "string" ? body.detail : JSON.stringify(body.detail);
    } catch {
      /* 保留状态码信息 */
    }
    throw new Error(detail);
  }
  return (await response.json()) as T;
}

export const api = {
  health: () => request<{ status: string; version: string; db: string }>("/api/health"),
  sources: () => request<{ items: Source[] }>("/api/sources"),
  addSource: (root: string) =>
    request<{
      source_id: string;
      root_uri: string;
      label: string;
      created: boolean;
      covered_by: string | null;
      merged: { source_id: string; label: string; root_uri: string }[];
    }>("/api/sources", { method: "POST", body: JSON.stringify({ root }) }),
  scan: (id: string) => request<{ state: string }>(`/api/sources/${id}/scan`, { method: "POST" }),
  ocr: (id: string) =>
    request<{ state: string; pending: number }>(`/api/sources/${id}/ocr`, { method: "POST" }),
  setState: (id: string, state: "active" | "paused") =>
    request<{ state: string }>(`/api/sources/${id}/state?state=${state}`, { method: "POST" }),
  removeSource: (id: string) => request<{ assets_cleared: number }>(`/api/sources/${id}`, { method: "DELETE" }),
  search: (query: string, options: { limit?: number; sourceId?: string; ext?: string } = {}) => {
    const params = new URLSearchParams({ q: query, limit: String(options.limit ?? 50) });
    if (options.sourceId) params.set("source_id", options.sourceId);
    if (options.ext) params.set("ext", options.ext);
    return request<SearchResponse>(`/api/search?${params.toString()}`);
  },
  stats: () => request<OverviewStats>("/api/stats"),
  ask: (question: string, options: { sourceId?: string; ext?: string; topK?: number; sessionId?: string } = {}) =>
    request<AnswerResult>("/api/ask", {
      method: "POST",
      body: JSON.stringify({
        question,
        ...(options.sourceId ? { source_id: options.sourceId } : {}),
        ...(options.ext ? { ext: options.ext } : {}),
        ...(options.topK ? { top_k: options.topK } : {}),
        ...(options.sessionId ? { session_id: options.sessionId } : {}),
      }),
    }),
  chatSessions: () => request<{ items: ChatSession[] }>("/api/chat/sessions"),
  createChatSession: (title?: string) =>
    request<ChatSession>("/api/chat/sessions", {
      method: "POST",
      body: JSON.stringify(title ? { title } : {}),
    }),
  renameChatSession: (id: string, title: string) =>
    request<ChatSession>(`/api/chat/sessions/${id}`, {
      method: "PATCH",
      body: JSON.stringify({ title }),
    }),
  deleteChatSession: (id: string) =>
    request<{ session_id: string; deleted: boolean }>(`/api/chat/sessions/${id}`, {
      method: "DELETE",
    }),
  chatSession: (id: string) =>
    request<{ session: ChatSession; messages: ChatMessage[] }>(`/api/chat/sessions/${id}`),
  answerSettings: () => request<AnswerSettings>("/api/settings/answer"),
  saveAnswerSettings: (payload: Record<string, unknown>) =>
    request<AnswerSettings>("/api/settings/answer", {
      method: "PUT",
      body: JSON.stringify(payload),
    }),
  embeddingSettings: () => request<EmbeddingSettings>("/api/settings/embedding"),
  saveEmbeddingSettings: (payload: Record<string, unknown>) =>
    request<EmbeddingSettings>("/api/settings/embedding", {
      method: "PUT",
      body: JSON.stringify(payload),
    }),
  querySettings: () => request<QuerySettings>("/api/settings/query"),
  saveQuerySettings: (payload: Record<string, unknown>) =>
    request<QuerySettings>("/api/settings/query", {
      method: "PUT",
      body: JSON.stringify(payload),
    }),
  ocrSettings: () => request<OcrSettings>("/api/settings/ocr"),
  saveOcrSettings: (payload: Record<string, unknown>) =>
    request<OcrSettings>("/api/settings/ocr", {
      method: "PUT",
      body: JSON.stringify(payload),
    }),
  events: (limit = 30) => request<{ items: MaintenanceEvent[] }>(`/api/events?limit=${limit}`),
  maintenance: (
    action: "reindex" | "retry" | "ocr" | "cleanup" | "embed",
    sourceId?: string,
  ) =>
    request<{ kind: string }>(
      `/api/maintenance/${action}${sourceId ? `?source_id=${encodeURIComponent(sourceId)}` : ""}`,
      { method: "POST" },
    ),
  jobs: (options: { status?: string; limit?: number } = {}) => {
    const params = new URLSearchParams({ limit: String(options.limit ?? 20) });
    if (options.status) params.set("status", options.status);
    return request<{ items: IndexJob[] }>(`/api/jobs?${params.toString()}`);
  },
  retryJob: (jobId: string) =>
    request<{ job_id: string; state: string }>(`/api/jobs/${jobId}/retry`, { method: "POST" }),
};

export function formatSize(bytes: number): string {
  if (bytes < 1024) return `${bytes} B`;
  const units = ["KB", "MB", "GB", "TB"];
  let value = bytes / 1024;
  let index = 0;
  while (value >= 1024 && index < units.length - 1) {
    value /= 1024;
    index += 1;
  }
  return `${value < 10 ? value.toFixed(1) : Math.round(value)} ${units[index]}`;
}

export function formatTime(iso: string | null): string {
  if (!iso) return "未同步";
  const date = new Date(iso);
  if (Number.isNaN(date.getTime())) return iso;
  const pad = (n: number) => String(n).padStart(2, "0");
  return `${date.getFullYear()}-${pad(date.getMonth() + 1)}-${pad(date.getDate())} ${pad(date.getHours())}:${pad(date.getMinutes())}`;
}
