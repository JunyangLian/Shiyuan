import { useCallback, useEffect, useState } from "react";
import { api, formatTime } from "../api";
import type { OcrSettings, UpdateOverview } from "../api";
import Icon from "../Icon";

const PROFILES: { key: "quiet" | "balanced" | "fast"; label: string }[] = [
  { key: "quiet", label: "安静" },
  { key: "balanced", label: "平衡" },
  { key: "fast", label: "快速" },
];

const OCR_POLICY_LABEL: Record<string, string> = {
  auto: "自动",
  windows: "Windows OCR",
  rapidocr: "PP-OCRv6 small",
};

export default function HealthView() {
  const [overview, setOverview] = useState<UpdateOverview | null>(null);
  const [ocrInfo, setOcrInfo] = useState<OcrSettings | null>(null);
  const [ocrPolicy, setOcrPolicy] = useState<OcrSettings["policy"]>("auto");
  const [profileDraft, setProfileDraft] = useState<string | null>(null);
  const [loading, setLoading] = useState(true);
  const [error, setError] = useState<string | null>(null);
  const [note, setNote] = useState<string | null>(null);
  const [savingAuto, setSavingAuto] = useState(false);
  const [savingProfile, setSavingProfile] = useState(false);
  const [savingOcr, setSavingOcr] = useState(false);
  const [checking, setChecking] = useState(false);
  const [rebuilding, setRebuilding] = useState(false);
  const [retrying, setRetrying] = useState<string | null>(null);

  const load = useCallback(async () => {
    try {
      const data = await api.updatesOverview();
      setOverview(data);
      setError(null);
    } catch (err) {
      setError(err instanceof Error ? err.message : String(err));
    } finally {
      setLoading(false);
    }
  }, []);

  useEffect(() => {
    void load();
    const timer = window.setInterval(load, 5000);
    return () => window.clearInterval(timer);
  }, [load]);

  useEffect(() => {
    api
      .ocrSettings()
      .then((data) => {
        setOcrInfo(data);
        setOcrPolicy(data.policy);
      })
      .catch(() => setOcrInfo(null));
  }, []);

  const auto = overview?.auto_parse ?? true;
  const paused = overview?.paused ?? false;
  const watcher = overview?.watch_enabled
    ? overview.watcher_running
      ? "监听运行中"
      : "监听未启动"
    : "实时监听已关闭";
  const summary = overview?.summary;

  const toggleAuto = async () => {
    if (!overview) return;
    const next = !overview.auto_parse;
    setSavingAuto(true);
    setError(null);
    setNote(null);
    try {
      await api.saveIngestSettings({
        auto_parse: next,
        ...(next ? { paused: false } : {}),
      });
      if (next) await api.checkUpdates();
      setNote(
        next
          ? "已开启持续自动解析，正在检查新增或修改过的文件。"
          : "已关闭持续自动解析；新文件只登记元数据，可在数据页手动深度解析。",
      );
      await load();
    } catch (err) {
      setError(err instanceof Error ? err.message : String(err));
    } finally {
      setSavingAuto(false);
    }
  };

  const checkNow = async () => {
    setChecking(true);
    setError(null);
    setNote(null);
    try {
      const result = await api.checkUpdates();
      setNote(
        result.started.length
          ? `已开始检查 ${result.started.length} 个数据源。`
          : "没有可检查的数据源。",
      );
      await load();
    } catch (err) {
      setError(err instanceof Error ? err.message : String(err));
    } finally {
      setChecking(false);
    }
  };

  const rebuildFamilies = async () => {
    setRebuilding(true);
    setError(null);
    setNote(null);
    try {
      const result = await api.rebuildFamilies();
      setNote(
        result.state === "running"
          ? "已开始重建文档家族，完成后检索会自动按推荐版本归并。"
          : "文档家族正在重建中，请稍后再试。",
      );
    } catch (err) {
      setError(err instanceof Error ? err.message : String(err));
    } finally {
      setRebuilding(false);
    }
  };

  const resume = async () => {
    setSavingAuto(true);
    try {
      await api.saveIngestSettings({ paused: false });
      await api.checkUpdates();
      setNote("已恢复自动解析。");
      await load();
    } catch (err) {
      setError(err instanceof Error ? err.message : String(err));
    } finally {
      setSavingAuto(false);
    }
  };

  const saveProfile = async () => {
    setSavingProfile(true);
    setError(null);
    try {
      await api.saveIngestSettings({
        profile: profileDraft ?? overview?.profile ?? "balanced",
      });
      setProfileDraft(null);
      setNote("资源模式已保存；只影响处理速度，不会重复解析。");
      await load();
    } catch (err) {
      setError(err instanceof Error ? err.message : String(err));
    } finally {
      setSavingProfile(false);
    }
  };

  const saveOcrPolicy = async () => {
    setSavingOcr(true);
    setError(null);
    try {
      const saved = await api.saveOcrSettings({ policy: ocrPolicy });
      setOcrInfo(saved);
      setNote("OCR 策略已保存；对之后的深度解析生效。");
      await load();
    } catch (err) {
      setError(err instanceof Error ? err.message : String(err));
    } finally {
      setSavingOcr(false);
    }
  };

  const retry = async (jobId: string) => {
    setRetrying(jobId);
    setError(null);
    try {
      await api.retryJob(jobId);
      setNote("已提交重试。");
      await load();
    } catch (err) {
      setError(err instanceof Error ? err.message : String(err));
    } finally {
      setRetrying(null);
    }
  };

  return (
    <div className="view">
      <header className="view-head">
        <div>
          <p className="eyebrow">持续更新</p>
          <h1>更新</h1>
          <p className="sub">
            监听授权目录，只解析新增或修改后的文件；已解析且未变化的文件不会重复处理。
          </p>
        </div>
        <div className="chip-row">
          <span className={`chip ${auto && !paused ? "ok" : "warn"}`}>
            {paused ? "自动解析已暂停" : auto ? "自动解析已开启" : "自动解析已关闭"}
          </span>
          <button
            className="ghost small"
            disabled={rebuilding}
            onClick={() => void rebuildFamilies()}
          >
            {rebuilding ? "提交中…" : "重建文档家族"}
          </button>
          <button className="ghost small icon-label" disabled={checking} onClick={() => void checkNow()}>
            <Icon name="refresh" size={14} />
            {checking ? "检查中…" : "立即检查"}
          </button>
        </div>
      </header>

      {error && <div className="alert error">{error}</div>}
      {note && <div className="notice">{note}</div>}
      {loading && !overview && <div className="notice">正在读取更新状态…</div>}

      <section className="panel update-control">
        <div className="update-control-main">
          <div>
            <h2>持续自动解析</h2>
            <p className="muted small-text">
              开启后，新增或修改过的文件会自动完成初解析与深度解析；
              原文件未变化的条目直接跳过。
            </p>
          </div>
          <button
            role="switch"
            aria-checked={auto && !paused}
            aria-label="持续自动解析"
            className={`switch${auto && !paused ? " on" : ""}`}
            disabled={savingAuto || !overview}
            onClick={() => void toggleAuto()}
          >
            <span className="switch-knob" />
          </button>
        </div>

        <div className="update-status-line">
          <span>{watcher}</span>
          <span>最近同步 {formatTime(overview?.last_sync_at ?? null)}</span>
          <span>数据源 {summary?.sources ?? 0} 个</span>
          {paused && (
            <button className="ghost small" disabled={savingAuto} onClick={() => void resume()}>
              恢复解析
            </button>
          )}
        </div>

        <div className="update-options">
          <label>
            资源模式
            <select
              value={profileDraft ?? overview?.profile ?? "balanced"}
              disabled={savingProfile}
              onChange={(event) => setProfileDraft(event.target.value)}
            >
              {PROFILES.map((profile) => (
                <option key={profile.key} value={profile.key}>
                  {profile.label}
                </option>
              ))}
            </select>
          </label>
          <button className="ghost small" disabled={savingProfile} onClick={() => void saveProfile()}>
            {savingProfile ? "保存中…" : "保存"}
          </button>
          <label>
            OCR 引擎
            <select
              value={ocrPolicy}
              disabled={savingOcr}
              onChange={(event) =>
                setOcrPolicy(event.target.value as OcrSettings["policy"])
              }
            >
              <option value="auto">{OCR_POLICY_LABEL.auto}</option>
              <option value="windows">{OCR_POLICY_LABEL.windows}</option>
              <option value="rapidocr">{OCR_POLICY_LABEL.rapidocr}</option>
            </select>
          </label>
          <button className="ghost small" disabled={savingOcr} onClick={() => void saveOcrPolicy()}>
            {savingOcr ? "保存中…" : "保存"}
          </button>
          <span className="muted small-text">
            {ocrInfo?.engines
              .map((item) => `${item.label}：${item.available ? "可用" : "不可用"}`)
              .join(" · ") ?? "正在读取 OCR 引擎状态…"}
          </span>
        </div>
      </section>

      <div className="inventory-summary">
        <div>
          <span>待初解析</span>
          <strong>{summary?.pending_l1 ?? 0}</strong>
        </div>
        <div>
          <span>待深度解析</span>
          <strong>{summary?.pending_l2 ?? 0}</strong>
        </div>
        <div>
          <span>24 小时已解析</span>
          <strong>{summary?.parsed_24h ?? 0}</strong>
        </div>
        <div>
          <span>失败记录</span>
          <strong>{summary?.failed ?? 0}</strong>
        </div>
      </div>

      {summary && summary.awaiting_policy > 0 && (
        <div className="notice">
          有 {summary.awaiting_policy} 个数据源尚未确认盘点策略；确认后才会进入自动解析。
        </div>
      )}

      <section className="panel inventory-panel">
        <div className="inventory-panel-head">
          <h3>最近解析活动</h3>
          <span className="muted small-text">只显示新增或修改后文件的处理记录</span>
        </div>
        {overview && overview.recent.length === 0 ? (
          <p className="muted">还没有解析记录。新增或修改文件后会自动出现在这里。</p>
        ) : (
          <ul className="update-list">
            {overview?.recent.map((item, index) => (
              <li
                key={`${item.path}-${item.stage}-${item.created_at}-${index}`}
                className="update-item"
              >
                <div className="update-item-main">
                  <div className="update-item-title">
                    <span className="update-item-name">{item.name ?? "—"}</span>
                    <span className="chip subtle">
                      {item.stage === "L2" ? "OCR" : "正文"}
                    </span>
                    <span className="chip">{item.source_label}</span>
                  </div>
                  <div className="update-item-meta">
                    {item.state === "ok" ? `${item.chars} 字` : item.state}
                    {" · "}
                    {formatTime(item.created_at)}
                  </div>
                </div>
              </li>
            ))}
          </ul>
        )}
      </section>

      <section className="panel inventory-panel">
        <div className="inventory-panel-head">
          <h3>最近失败</h3>
          <span className="muted small-text">
            失败文件不会反复重试；完整记录可在数据页的「解析结果」查看
          </span>
        </div>
        {overview && overview.failures.length === 0 ? (
          <p className="muted">没有失败记录。</p>
        ) : (
          <ul className="update-list">
            {overview?.failures.map((item) => (
              <li key={item.job_id} className="update-item">
                <div className="update-item-main">
                  <div className="update-item-title">
                    <span className="update-item-name">{item.name ?? "—"}</span>
                    <span className="chip warn">
                      {item.stage === "l2" ? "OCR" : "正文"}
                    </span>
                    <span className="chip">{item.source_label}</span>
                  </div>
                  <div className="update-item-error">{item.error ?? "解析失败"}</div>
                  <div className="update-item-meta">
                    尝试 {item.attempts} 次 · {formatTime(item.updated_at)}
                  </div>
                </div>
                <button
                  className="ghost small"
                  disabled={retrying === item.job_id}
                  onClick={() => void retry(item.job_id)}
                >
                  {retrying === item.job_id ? "重试中…" : "重试"}
                </button>
              </li>
            ))}
          </ul>
        )}
      </section>

      <div className="notice">
        原文件只读，解析结果只写入应用缓存。关闭自动解析后，仍可在「数据」页对单个数据源手动深度解析。
      </div>
    </div>
  );
}
