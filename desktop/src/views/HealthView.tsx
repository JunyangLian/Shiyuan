import { useCallback, useEffect, useState } from "react";
import { api, formatSize, formatTime } from "../api";
import type { IndexJob, MaintenanceEvent, OcrSettings, OverviewStats } from "../api";

const OCR_POLICY_LABEL: Record<string, string> = {
  auto: "自动（文本 PDF → Windows / 扫描 PDF → PP-OCRv6 small）",
  windows: "全部 Windows OCR",
  rapidocr: "全部 PP-OCRv6 small",
};

const EVENT_LABEL: Record<string, string> = {
  source_added: "新增数据源",
  source_merged: "合并数据源",
  source_removed: "撤销授权",
  source_state: "状态变更",
  scan_completed: "扫描完成",
  scan_failed: "扫描失败",
  location_moved: "文件移动",
  index_completed: "内容解析",
  index_failed: "解析失败",
  watch_started: "监听启动",
  watch_stopped: "监听停止",
  watch_failed: "监听失败",
  sync_completed: "增量同步",
  sync_failed: "同步失败",
  maintenance_reindex: "重建索引",
  maintenance_retry: "重试失败",
  maintenance_ocr: "重建 OCR",
  maintenance_cleanup: "清理缓存",
  maintenance_embed: "补建向量索引",
  job_retry: "单文件重试",
  maintenance_reindex_failed: "重建索引失败",
  maintenance_retry_failed: "重试失败",
  maintenance_ocr_failed: "重建 OCR 失败",
  maintenance_cleanup_failed: "清理失败",
  maintenance_embed_failed: "补建向量失败",
};

const ACTION_LABEL: Record<string, string> = {
  reindex: "重建正文索引",
  retry: "重试失败任务",
  ocr: "重建 OCR",
  cleanup: "清理旧版本缓存",
  embed: "补建向量索引",
};

export default function HealthView({ embedded = false }: { embedded?: boolean }) {
  const [stats, setStats] = useState<OverviewStats | null>(null);
  const [events, setEvents] = useState<MaintenanceEvent[]>([]);
  const [jobs, setJobs] = useState<IndexJob[]>([]);
  const [error, setError] = useState<string | null>(null);
  const [loading, setLoading] = useState(false);
  const [actionBusy, setActionBusy] = useState<string | null>(null);
  const [jobBusy, setJobBusy] = useState<string | null>(null);

  const [ocrInfo, setOcrInfo] = useState<OcrSettings | null>(null);
  const [ocrPolicy, setOcrPolicy] = useState<OcrSettings["policy"]>("auto");
  const [ocrSaving, setOcrSaving] = useState(false);
  const [ocrNote, setOcrNote] = useState<string | null>(null);

  useEffect(() => {
    api
      .ocrSettings()
      .then((data) => {
        setOcrInfo(data);
        setOcrPolicy(data.policy);
      })
      .catch(() => setOcrInfo(null));
  }, []);

  const load = useCallback(async () => {
    setLoading(true);
    try {
      const [overview, log, failed] = await Promise.all([
        api.stats(),
        api.events(40),
        api.jobs({ status: "failed", limit: 10 }),
      ]);
      setStats(overview);
      setEvents(log.items);
      setJobs(failed.items);
      setError(null);
    } catch (err) {
      setError(err instanceof Error ? err.message : String(err));
    } finally {
      setLoading(false);
    }
  }, []);

  useEffect(() => {
    void load();
  }, [load]);

  const maintenanceRunning = stats?.maintenance.running.running ?? false;

  useEffect(() => {
    if (!maintenanceRunning) return;
    const timer = window.setInterval(() => void load(), 1500);
    return () => window.clearInterval(timer);
  }, [maintenanceRunning, load]);

  const runMaintenance = async (action: "reindex" | "retry" | "ocr" | "cleanup" | "embed") => {
    const label = ACTION_LABEL[action];
    if (action !== "retry" &&
        !window.confirm(`${label}会重建派生数据，不会修改原文件。继续？`)) {
      return;
    }
    setActionBusy(action);
    setError(null);
    try {
      await api.maintenance(action);
      await load();
    } catch (err) {
      setError(err instanceof Error ? err.message : String(err));
    } finally {
      setActionBusy(null);
    }
  };

  const retryJob = async (jobId: string) => {
    setJobBusy(jobId);
    setError(null);
    try {
      await api.retryJob(jobId);
      await load();
    } catch (err) {
      setError(err instanceof Error ? err.message : String(err));
    } finally {
      setJobBusy(null);
    }
  };

  const saveOcrPolicy = async () => {
    setOcrSaving(true);
    setOcrNote(null);
    setError(null);
    try {
      const saved = await api.saveOcrSettings({ policy: ocrPolicy });
      setOcrInfo(saved);
      setOcrNote("OCR 策略已保存；新策略对之后的深度解析生效。");
      await load();
    } catch (err) {
      setError(err instanceof Error ? err.message : String(err));
    } finally {
      setOcrSaving(false);
    }
  };

  const jobCounts = stats?.maintenance.jobs ?? {};
  const failedJobs = Object.entries(jobCounts)
    .filter(([key]) => key.endsWith(":failed"))
    .reduce((sum, [, count]) => sum + count, 0);
  const activeJobs = Object.entries(jobCounts)
    .filter(([key]) => key.endsWith(":running") || key.endsWith(":pending"))
    .reduce((sum, [, count]) => sum + count, 0);

  const maxExt = Math.max(1, ...(stats?.by_ext.map((entry) => entry.count) ?? [1]));

  return (
    <div className={embedded ? "view-section" : "view"}>
      {embedded ? (
        <div className="section-toolbar">
          <span className="muted small-text">
            最近一次同步、内容覆盖和自动维护记录，都可以在这里检查。
          </span>
          <button className="ghost small" disabled={loading} onClick={() => void load()}>
            {loading ? "刷新中…" : "刷新"}
          </button>
        </div>
      ) : (
        <header className="view-head">
          <div>
            <h1>健康与维护</h1>
            <p className="sub">最近一次同步、覆盖程度和自动维护记录，都应当可以被检查。</p>
          </div>
          <button className="ghost" disabled={loading} onClick={() => void load()}>
            {loading ? "刷新中…" : "刷新"}
          </button>
        </header>
      )}

      {error && <div className="alert error">{error}</div>}

      <div className="stat-grid">
        <div className="stat">
          <span className="stat-label">可检索文件</span>
          <span className="stat-value">{stats?.files.available ?? 0}</span>
          <span className="stat-foot">L0 目录层</span>
        </div>
        <div className="stat">
          <span className="stat-label">失联</span>
          <span className="stat-value">{stats?.files.missing ?? 0}</span>
          <span className="stat-foot">扫描确认原位置不在</span>
        </div>
        <div className="stat">
          <span className="stat-label">离线</span>
          <span className="stat-value">{stats?.files.offline ?? 0}</span>
          <span className="stat-foot">数据源当前不可达</span>
        </div>
        <div className="stat">
          <span className="stat-label">内容索引</span>
          <span className="stat-value">{stats?.coverage.content_indexed ?? 0}</span>
          <span className="stat-foot">
            {stats?.coverage.content_level ?? "L1"} · 待解析 {stats?.coverage.content_pending ?? 0}
          </span>
        </div>
      </div>

      <section className="panel">
        <div className="panel-head">
          <h2>内容覆盖（L1）</h2>
          <span className="muted small-text">统计当前内容版本的解析结果，不含历史版本</span>
        </div>
        <table className="table">
          <tbody>
            <tr>
              <td>已解析，可检索正文</td>
              <td>{stats?.coverage.content_indexed ?? 0}</td>
            </tr>
            <tr>
              <td>待解析</td>
              <td>{stats?.coverage.content_pending ?? 0}</td>
            </tr>
            <tr>
              <td>无文本层（可能是扫描件）</td>
              <td>{stats?.coverage.content_empty ?? 0}</td>
            </tr>
            <tr>
              <td>低质量文本（已尝试 OCR 补充）</td>
              <td>{stats?.coverage.low_quality ?? 0}</td>
            </tr>
            <tr>
              <td>解析失败</td>
              <td>{stats?.coverage.content_failed ?? 0}</td>
            </tr>
            <tr>
              <td>暂不支持的类型</td>
              <td>{stats?.coverage.content_unsupported ?? 0}</td>
            </tr>
            <tr>
              <td>OCR 已识别（可检索）</td>
              <td>{stats?.coverage.ocr_indexed ?? 0}</td>
            </tr>
            <tr>
              <td>待 OCR（图片 / 无文本层 PDF / PPTX 内嵌图）</td>
              <td>{stats?.coverage.ocr_pending ?? 0}</td>
            </tr>
            <tr>
              <td>混合版式 PDF（部分页缺文本层）</td>
              <td>{stats?.coverage.mixed_pdf ?? 0}</td>
            </tr>
            <tr>
              <td>OCR 未识别到文字</td>
              <td>{stats?.coverage.ocr_empty ?? 0}</td>
            </tr>
            <tr>
              <td>OCR 失败</td>
              <td>{stats?.coverage.ocr_failed ?? 0}</td>
            </tr>
          </tbody>
        </table>
        <div className="muted small-text" style={{ marginTop: 10 }}>
          OCR 策略：{OCR_POLICY_LABEL[stats?.coverage.ocr_policy ?? ocrPolicy] ?? "自动"}
          {stats?.coverage.ocr_languages?.length
            ? ` · Windows 语言包：${stats.coverage.ocr_languages.join(" / ")}`
            : ""}
          {stats?.coverage.ocr_available === false
            ? ` · Windows OCR 不可用：${stats.coverage.ocr_message ?? "未安装语言包"}`
            : ""}
        </div>
      </section>

      <section className="panel">
        <div className="panel-head">
          <h2>知识库管理</h2>
          <span className="muted small-text">
            {maintenanceRunning
              ? `正在执行：${ACTION_LABEL[stats?.maintenance.running.kind ?? ""] ?? "维护"}…`
              : "维护只改派生缓存，不动原文件"}
          </span>
        </div>
        <table className="table">
          <tbody>
            <tr>
              <td>索引任务失败</td>
              <td>{failedJobs}</td>
            </tr>
            <tr>
              <td>排队 / 运行中的任务</td>
              <td>{activeJobs}</td>
            </tr>
            <tr>
              <td>缓存块数</td>
              <td>{stats?.maintenance.chunks ?? 0}</td>
            </tr>
            <tr>
              <td>数据库大小</td>
              <td>{formatSize(stats?.maintenance.db_bytes ?? 0)}</td>
            </tr>
            <tr>
              <td>今日预算</td>
              <td>
                正文 {stats?.maintenance.budget.l1_used ?? 0}/
                {stats?.maintenance.budget.l1_limit ?? 0} · OCR{" "}
                {stats?.maintenance.budget.l2_used ?? 0}/
                {stats?.maintenance.budget.l2_limit ?? 0}
              </td>
            </tr>
            <tr>
              <td>向量条数</td>
              <td>{stats?.maintenance.vectors ?? 0}</td>
            </tr>
            <tr>
              <td>语义检索</td>
              <td>
                {stats?.maintenance.embedding.provider === "none"
                  ? "关闭"
                  : stats?.maintenance.embedding.provider === "ollama"
                    ? "Ollama 本机"
                    : "OpenAI 兼容"}
                {stats?.maintenance.embedding.model
                  ? ` · ${stats.maintenance.embedding.model}`
                  : ""}
              </td>
            </tr>
            <tr>
              <td>OCR 策略</td>
              <td>
                <select
                  value={ocrPolicy}
                  onChange={(event) =>
                    setOcrPolicy(event.target.value as OcrSettings["policy"])
                  }
                >
                  <option value="auto">{OCR_POLICY_LABEL.auto}</option>
                  <option value="windows">{OCR_POLICY_LABEL.windows}</option>
                  <option value="rapidocr">{OCR_POLICY_LABEL.rapidocr}</option>
                </select>
                <button
                  className="ghost small"
                  style={{ marginLeft: 8 }}
                  disabled={ocrSaving}
                  onClick={() => void saveOcrPolicy()}
                >
                  {ocrSaving ? "保存中…" : "保存"}
                </button>
                <div className="muted small-text" style={{ marginTop: 4 }}>
                  {ocrInfo?.engines
                    .map((item) => `${item.label}：${item.available ? "可用" : "不可用"}`)
                    .join(" · ") ?? "正在读取引擎状态…"}
                </div>
              </td>
            </tr>
          </tbody>
        </table>
        {ocrNote && <div className="notice" style={{ marginTop: 10 }}>{ocrNote}</div>}
        <div className="panel-actions">
          <button
            className="ghost small"
            disabled={maintenanceRunning || actionBusy !== null}
            onClick={() => void runMaintenance("retry")}
          >
            {actionBusy === "retry" ? "提交中…" : "重试失败"}
          </button>
          <button
            className="ghost small"
            disabled={maintenanceRunning || actionBusy !== null}
            onClick={() => void runMaintenance("reindex")}
          >
            {actionBusy === "reindex" ? "提交中…" : "重建正文索引"}
          </button>
          <button
            className="ghost small"
            disabled={maintenanceRunning || actionBusy !== null}
            onClick={() => void runMaintenance("ocr")}
          >
            {actionBusy === "ocr" ? "提交中…" : "重建 OCR"}
          </button>
          <button
            className="ghost small"
            disabled={maintenanceRunning || actionBusy !== null}
            onClick={() => void runMaintenance("cleanup")}
          >
            {actionBusy === "cleanup" ? "提交中…" : "清理缓存"}
          </button>
          <button
            className="ghost small"
            disabled={
              maintenanceRunning ||
              actionBusy !== null ||
              stats?.maintenance.embedding.provider === "none" ||
              !stats?.maintenance.embedding.model
            }
            title="为尚未生成向量的文本块补建向量索引"
            onClick={() => void runMaintenance("embed")}
          >
            {actionBusy === "embed" ? "提交中…" : "补建向量索引"}
          </button>
        </div>
      </section>

      <section className="panel">
        <div className="panel-head">
          <h2>失败任务</h2>
          <span className="muted small-text">自动重试 3 次后停在这里，可单条重试</span>
        </div>
        {jobs.length ? (
          <table className="table">
            <thead>
              <tr>
                <th>文件</th>
                <th>阶段</th>
                <th>尝试</th>
                <th>错误</th>
                <th>操作</th>
              </tr>
            </thead>
            <tbody>
              {jobs.map((job) => (
                <tr key={job.job_id}>
                  <td className="ellipsis" style={{ maxWidth: 220 }} title={job.path ?? ""}>
                    {job.name ?? "—"}
                  </td>
                  <td>
                    <span className={`chip ${job.stage === "l2" ? "warn" : "subtle"}`}>
                      {job.stage === "l2" ? "OCR" : "正文"}
                    </span>
                  </td>
                  <td>{job.attempts}</td>
                  <td className="ellipsis" style={{ maxWidth: 260 }} title={job.error ?? ""}>
                    {job.error ?? "—"}
                  </td>
                  <td>
                    <button
                      className="ghost small"
                      disabled={jobBusy === job.job_id}
                      onClick={() => void retryJob(job.job_id)}
                    >
                      {jobBusy === job.job_id ? "重试中…" : "重试"}
                    </button>
                  </td>
                </tr>
              ))}
            </tbody>
          </table>
        ) : (
          <p className="muted">没有失败任务。</p>
        )}
      </section>

      <section className="panel">
        <div className="panel-head">
          <h2>数据源同步</h2>
        </div>
        {stats?.sources.length ? (
          <table className="table">
            <thead>
              <tr>
                <th>数据源</th>
                <th>根目录</th>
                <th>状态</th>
                <th>可检索</th>
                <th>待刷新</th>
                <th>最后同步</th>
              </tr>
            </thead>
            <tbody>
              {stats.sources.map((source) => (
                <tr key={source.source_id}>
                  <td>{source.label}</td>
                  <td className="mono ellipsis" title={source.root_uri}>
                    {source.root_uri}
                  </td>
                  <td>
                    <span className={`chip ${source.health === "offline" ? "warn" : "ok"}`}>
                      {source.state === "paused" ? "已暂停" : source.health === "offline" ? "离线" : "正常"}
                    </span>
                  </td>
                  <td>{source.available}</td>
                  <td>
                    {source.stale > 0 ? <span className="chip warn">{source.stale}</span> : "0"}
                  </td>
                  <td>{formatTime(source.last_scan_at)}</td>
                </tr>
              ))}
            </tbody>
          </table>
        ) : (
          <p className="muted">尚未添加数据源。</p>
        )}
      </section>

      <section className="panel">
        <div className="panel-head">
          <h2>文件类型分布</h2>
        </div>
        {stats?.by_ext.length ? (
          <ul className="bars">
            {stats.by_ext.map((entry) => (
              <li key={entry.ext || "none"}>
                <span className="bar-label">{entry.ext || "无扩展名"}</span>
                <span className="bar-track">
                  <span className="bar-fill" style={{ width: `${(entry.count / maxExt) * 100}%` }} />
                </span>
                <span className="bar-count">{entry.count}</span>
              </li>
            ))}
          </ul>
        ) : (
          <p className="muted">暂无数据。</p>
        )}
      </section>

      <section className="panel">
        <div className="panel-head">
          <h2>维护日志</h2>
          <span className="muted small-text">只记录索引侧动作，不涉及原件修改</span>
        </div>
        {events.length ? (
          <ul className="event-list">
            {events.map((event) => (
              <li key={event.event_id}>
                <span className="event-time">{formatTime(event.timestamp)}</span>
                <span className={`chip ${event.type === "scan_failed" ? "warn" : "subtle"}`}>
                  {EVENT_LABEL[event.type] ?? event.type}
                </span>
                <span className="event-detail ellipsis" title={event.detail ?? ""}>
                  {event.detail ?? "—"}
                </span>
              </li>
            ))}
          </ul>
        ) : (
          <p className="muted">还没有维护记录。</p>
        )}
      </section>
    </div>
  );
}
