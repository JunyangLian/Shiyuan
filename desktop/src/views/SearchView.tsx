import { useCallback, useEffect, useMemo, useRef, useState } from "react";
import type { ReactNode } from "react";
import { api, formatSize, formatTime } from "../api";
import type { OverviewStats, SearchItem, SearchResponse } from "../api";
import { openFile } from "../tauri";

function Highlight({ text, tokens }: { text: string; tokens: string[] }) {
  if (!tokens.length) return <>{text}</>;
  const lower = text.toLowerCase();
  const ranges: [number, number][] = [];
  for (const token of tokens) {
    let from = 0;
    while (from < lower.length) {
      const at = lower.indexOf(token, from);
      if (at < 0) break;
      ranges.push([at, at + token.length]);
      from = at + token.length;
    }
  }
  if (!ranges.length) return <>{text}</>;
  ranges.sort((a, b) => a[0] - b[0]);
  const merged: [number, number][] = [];
  for (const range of ranges) {
    const last = merged[merged.length - 1];
    if (last && range[0] <= last[1]) last[1] = Math.max(last[1], range[1]);
    else merged.push([range[0], range[1]]);
  }
  const parts: ReactNode[] = [];
  let cursor = 0;
  merged.forEach(([start, end], index) => {
    if (start > cursor) parts.push(text.slice(cursor, start));
    parts.push(<mark key={index}>{text.slice(start, end)}</mark>);
    cursor = end;
  });
  if (cursor < text.length) parts.push(text.slice(cursor));
  return <>{parts}</>;
}

function ExtBadge({ ext }: { ext: string }) {
  return <span className="ext-badge">{(ext || "file").slice(0, 4).toUpperCase()}</span>;
}

export default function SearchView({
  onGoSources,
  embedded = false,
}: {
  onGoSources: () => void;
  embedded?: boolean;
}) {
  const [query, setQuery] = useState("");
  const [result, setResult] = useState<SearchResponse | null>(null);
  const [loading, setLoading] = useState(false);
  const [error, setError] = useState<string | null>(null);
  const [stats, setStats] = useState<OverviewStats | null>(null);
  const [ext, setExt] = useState("");
  const [sourceId, setSourceId] = useState("");
  const requestId = useRef(0);

  const tokens = useMemo(
    () => query.toLowerCase().split(/\s+/).filter(Boolean),
    [query],
  );

  useEffect(() => {
    api.stats().then(setStats).catch(() => setStats(null));
  }, []);

  const runSearch = useCallback(
    async (text: string, extFilter: string, sourceFilter: string) => {
      const trimmed = text.trim();
      if (!trimmed) {
        setResult(null);
        setError(null);
        return;
      }
      const id = ++requestId.current;
      setLoading(true);
      try {
        const data = await api.search(trimmed, {
          ext: extFilter || undefined,
          sourceId: sourceFilter || undefined,
        });
        if (id !== requestId.current) return;
        setResult(data);
        setError(null);
      } catch (err) {
        if (id !== requestId.current) return;
        setError(err instanceof Error ? err.message : String(err));
      } finally {
        if (id === requestId.current) setLoading(false);
      }
    },
    [],
  );

  useEffect(() => {
    const timer = window.setTimeout(() => void runSearch(query, ext, sourceId), 220);
    return () => window.clearTimeout(timer);
  }, [query, ext, sourceId, runSearch]);

  const hasSources = (stats?.sources.length ?? 0) > 0;
  const items: SearchItem[] = result?.items ?? [];

  return (
    <div className={embedded ? "view-section" : "view"}>
      {!embedded && (
        <header className="view-head">
          <div>
            <h1>找文件</h1>
            <p className="sub">按文件名、路径与目录定位原件，不需要先把文件搬进应用。</p>
          </div>
        </header>
      )}

      <div className="search-box">
        <input
          autoFocus
          value={query}
          placeholder="例如：BLAST 结果 / 2025 实验记录 / 项目A"
          onChange={(event) => setQuery(event.target.value)}
          onKeyDown={(event) => {
            if (event.key === "Enter") void runSearch(query, ext, sourceId);
          }}
        />
        {query && (
          <button className="ghost" onClick={() => setQuery("")}>
            清空
          </button>
        )}
      </div>

      <div className="filter-row">
        <select value={ext} onChange={(event) => setExt(event.target.value)}>
          <option value="">全部类型</option>
          {(stats?.by_ext ?? []).map((entry) => (
            <option key={entry.ext || "none"} value={entry.ext}>
              {entry.ext || "无扩展名"}（{entry.count}）
            </option>
          ))}
        </select>
        <select value={sourceId} onChange={(event) => setSourceId(event.target.value)}>
          <option value="">全部数据源</option>
          {(stats?.sources ?? []).map((source) => (
            <option key={source.source_id} value={source.source_id}>
              {source.label}
            </option>
          ))}
        </select>
        <span className="coverage-note">
          覆盖：L0 文件名/路径 · L1 正文已解析 {stats?.coverage.content_indexed ?? 0}
          {stats && stats.coverage.content_pending > 0
            ? `（待解析 ${stats.coverage.content_pending}）`
            : ""}
          {" · 语义："}
          {stats?.maintenance.embedding.provider === "none" ? "关" : "开"}
        </span>
      </div>

      {error && <div className="alert error">检索失败：{error}</div>}
      {result?.vector_error && (
        <div className="alert warn">语义召回失败，已回退关键词检索：{result.vector_error}</div>
      )}
      {result?.rewritten && (
        <div className="muted small-text">已按改写后的查询「{result.rewritten}」检索。</div>
      )}

      {!query.trim() && (
        <div className="empty">
          <div className="empty-title">从你原来的文件夹开始</div>
          <p>
            拾源只登记授权目录中的文件名、路径与元数据，不复制、不移动、不改名。
            {!hasSources && "先添加一个数据源，再回来搜索。"}
          </p>
          {!hasSources ? (
            <button className="primary" onClick={onGoSources}>
              去添加数据源
            </button>
          ) : (
            <div className="hint-grid">
              <div>
                <span className="hint-k">可检索文件</span>
                <span className="hint-v">{stats?.files.available ?? 0}</span>
              </div>
              <div>
                <span className="hint-k">数据源</span>
                <span className="hint-v">{stats?.sources.length ?? 0}</span>
              </div>
              <div>
                <span className="hint-k">失联</span>
                <span className="hint-v">{stats?.files.missing ?? 0}</span>
              </div>
            </div>
          )}
        </div>
      )}

      {query.trim() && (
        <>
          <div className="result-meta">
            <span>
              {loading ? "检索中…" : `${result?.total ?? 0} 个结果`}
              {result?.truncated ? "（结果过多，仅保留前 5000 个候选）" : ""}
            </span>
            {result && result.total > items.length && (
              <span className="muted">显示前 {items.length} 条</span>
            )}
          </div>

          {!loading && result && items.length === 0 && (
            <div className="empty">
              <div className="empty-title">没有匹配的文件</div>
              <p>
                当前检索文件名、路径与已解析的正文。扫描件或尚未完成解析的文件可能还没有正文索引，
                可在「健康」页查看内容覆盖情况。
              </p>
            </div>
          )}

          <ul className="result-list">
            {items.map((item) => (
              <li key={item.location_id} className="result-card">
                <ExtBadge ext={item.ext} />
                <div className="result-body">
                  <div className="result-name">
                    <Highlight text={item.name} tokens={tokens} />
                  </div>
                  <div className="result-path">
                    <Highlight text={item.directory} tokens={tokens} />
                  </div>
                  {item.snippet && (
                    <div className="result-snippet">
                      <Highlight text={item.snippet} tokens={tokens} />
                    </div>
                  )}
                  <div className="result-meta-line">
                    <span>{formatSize(item.size)}</span>
                    <span>{formatTime(item.mtime)}</span>
                    <span className="chip">{item.source_label}</span>
                    {item.page != null && <span className="chip subtle">第 {item.page} 页</span>}
                    {item.sheet && <span className="chip subtle">工作表 {item.sheet}</span>}
                    {item.index_level === "L2" && <span className="chip subtle">OCR 正文</span>}
                    {item.semantic && <span className="chip warn">语义命中</span>}
                    <span className="chip subtle">
                      {item.match === "name"
                        ? "文件名命中"
                        : item.match === "both"
                          ? "文件名 + 正文"
                          : item.match === "semantic"
                            ? "语义召回"
                            : "正文命中"}
                    </span>
                  </div>
                </div>
                <div className="result-actions">
                  <button className="primary small" onClick={() => void openFile(item.path)}>
                    打开
                  </button>
                  <button
                    className="ghost small"
                    onClick={() => void navigator.clipboard.writeText(item.path)}
                    title="复制完整路径"
                  >
                    复制路径
                  </button>
                </div>
              </li>
            ))}
          </ul>
        </>
      )}
    </div>
  );
}
