import { useCallback, useEffect, useState } from "react";
import { api, formatTime } from "../api";
import type { Source } from "../api";
import { pickDirectory } from "../tauri";
import type { InventorySource } from "./InventoryView";
import Icon from "../Icon";

export default function SourcesView({
  embedded = false,
  onOpenInventory,
  onOpenResults,
}: {
  embedded?: boolean;
  onOpenInventory?: (source: InventorySource) => void;
  onOpenResults?: (source: InventorySource) => void;
}) {
  const [sources, setSources] = useState<Source[]>([]);
  const [loaded, setLoaded] = useState(false);
  const [error, setError] = useState<string | null>(null);
  const [busy, setBusy] = useState<string | null>(null);
  const [notice, setNotice] = useState<string | null>(null);
  const [hoveredSource, setHoveredSource] = useState<string | null>(null);

  const load = useCallback(async () => {
    try {
      const data = await api.sources();
      setSources(data.items);
      setError(null);
    } catch (err) {
      setError(err instanceof Error ? err.message : String(err));
    } finally {
      setLoaded(true);
    }
  }, []);

  useEffect(() => {
    void load();
    const timer = window.setInterval(load, 2500);
    return () => window.clearInterval(timer);
  }, [load]);

  const addFolder = async () => {
    const picked = await pickDirectory();
    if (!picked) return;
    setBusy("add");
    setNotice(null);
    try {
      const created = await api.addSource(picked);
      const target: InventorySource = {
        source_id: created.source_id,
        label: created.label,
        root_uri: created.root_uri,
      };
      if (created.covered_by) {
        setNotice(`该目录已包含在「${created.label}」中，直接使用现有授权。`);
        await load();
        onOpenInventory?.(target);
        return;
      }
      await api.scan(created.source_id);
      if (created.merged.length > 0) {
        setNotice(
          `已自动合并 ${created.merged.length} 个下级目录到「${created.root_uri}」，` +
          "原文件不会移动。正在做初次盘点…",
        );
      }
      await load();
      onOpenInventory?.(target);
    } catch (err) {
      setError(err instanceof Error ? err.message : String(err));
    } finally {
      setBusy(null);
    }
  };

  const runAction = async (id: string, action: () => Promise<unknown>) => {
    setBusy(id);
    try {
      await action();
      await load();
    } catch (err) {
      setError(err instanceof Error ? err.message : String(err));
    } finally {
      setBusy(null);
    }
  };

  const remove = (source: Source) => {
    const confirmed = window.confirm(
      `撤销对「${source.label}」的授权？\n\n这会删除该目录的全部索引与派生记录，但不会动你的原文件。`,
    );
    if (!confirmed) return;
    void runAction(source.source_id, () => api.removeSource(source.source_id));
  };

  return (
    <div className={embedded ? "view-section" : "view"}>
      {embedded ? (
        <div className="section-toolbar">
          <span className="muted small-text">
            只授权目录，不搬迁文件；任何时候都可以暂停或撤销。
          </span>
          <button className="primary icon-label" disabled={busy === "add"} onClick={() => void addFolder()}>
            <Icon name="folder-plus" size={16} />
            {busy === "add" ? "正在添加…" : "添加文件夹"}
          </button>
        </div>
      ) : (
        <header className="view-head">
          <div>
            <h1>数据源</h1>
            <p className="sub">只授权目录，不搬迁文件；任何时候都可以暂停或撤销。</p>
          </div>
          <button className="primary icon-label" disabled={busy === "add"} onClick={() => void addFolder()}>
            <Icon name="folder-plus" size={16} />
            {busy === "add" ? "正在添加…" : "添加文件夹"}
          </button>
        </header>
      )}

      {error && <div className="alert error">{error}</div>}
      {notice && <div className="notice">{notice}</div>}

      <div className="notice">
        <strong>原件只读。</strong>
        扫描时只读取文件名、大小、修改时间与稳定文件 ID，内容摘要只写入应用自己的缓存目录。
        排除规则已默认忽略 .git、node_modules 等开发目录。
      </div>

      {loaded && sources.length === 0 && (
        <div className="empty">
          <div className="empty-title">还没有授权目录</div>
          <p>建议先添加 1—3 个真正在用的文件夹，首次扫描完成后即可按文件名或路径搜索。</p>
        </div>
      )}

      <ul className="source-list">
        {sources.map((source) => {
          const run = source.stats.last_run;
          const isBusy = busy === source.source_id;
          const paused = source.state === "paused";
          const running = source.scanning && !paused;
          const hovering = hoveredSource === source.source_id;
          const deepLabel = running
            ? hovering
              ? "暂停解析"
              : "解析中"
            : paused
              ? "继续解析"
              : "深度解析";
          return (
            <li key={source.source_id} className="source-card">
              <div className="source-main">
                <div className="source-title">
                  <span className="source-label">{source.label}</span>
                  <span className={`chip ${source.state === "paused" ? "warn" : "ok"}`}>
                    {source.state === "paused" ? "已暂停" : source.health === "offline" ? "目录离线" : "正常"}
                  </span>
                  {source.scanning && <span className="chip accent">扫描中</span>}
                </div>
                <div className="source-path" title={source.root_uri}>
                  {source.root_uri}
                </div>
                <div className="source-stats">
                  <span>可检索 {source.stats.available}</span>
                  {source.stats.stale > 0 && <span>待刷新 {source.stats.stale}</span>}
                  <span>失联 {source.stats.missing}</span>
                  <span>离线 {source.stats.offline}</span>
                  <span>最后同步 {formatTime(source.last_scan_at)}</span>
                </div>
                {run && run.state !== "running" && (
                  <div className="run-line">
                    上次扫描：发现 {run.files_seen} · 新增 {run.files_new} · 更新 {run.files_updated} · 移动{" "}
                    {run.files_moved} · 失联 {run.files_missing}
                    {run.errors > 0 ? ` · 错误 ${run.errors}` : ""}
                  </div>
                )}
                {run && run.state === "failed" && run.message && (
                  <div className="run-line error-text">失败原因：{run.message}</div>
                )}
              </div>
              <div className="source-actions">
                <button
                  className={`scan-icon${source.scanning ? " spinning" : ""}`}
                  disabled={isBusy || source.scanning}
                  title={source.scanning ? "扫描中…" : "扫描"}
                  aria-label={source.scanning ? "扫描中" : "扫描"}
                  onClick={() => void runAction(source.source_id, () => api.scan(source.source_id))}
                >
                  <Icon name="refresh" size={20} strokeWidth={1.8} />
                </button>
                <button
                  className={
                    `ghost small strong deep-parse` +
                    `${running ? " running" : ""}` +
                    `${running && hovering ? " stopping" : ""}` +
                    `${paused ? " paused" : ""}`
                  }
                  disabled={isBusy}
                  title={
                    running
                      ? "暂停当前解析"
                      : paused
                        ? "继续解析"
                        : "选择文件类型与策略，然后开始深度解析"
                  }
                  onMouseEnter={() => setHoveredSource(source.source_id)}
                  onMouseLeave={() => setHoveredSource(null)}
                  onFocus={() => setHoveredSource(source.source_id)}
                  onBlur={() => setHoveredSource(null)}
                  onClick={() => {
                    if (running) {
                      void runAction(source.source_id, () =>
                        api.setState(source.source_id, "paused"),
                      );
                      return;
                    }
                    if (paused) {
                      void runAction(source.source_id, async () => {
                        await api.setState(source.source_id, "active");
                        if (!source.scanning) await api.scan(source.source_id);
                      });
                      return;
                    }
                    onOpenInventory?.(source);
                  }}
                >
                  {deepLabel}
                </button>
                {onOpenResults && (
                  <button
                    className="ghost small"
                    disabled={isBusy}
                    onClick={() => onOpenResults(source)}
                  >
                    解析结果
                  </button>
                )}
                <button className="ghost small danger" disabled={isBusy} onClick={() => remove(source)}>
                  撤销授权
                </button>
              </div>
            </li>
          );
        })}
      </ul>
    </div>
  );
}
