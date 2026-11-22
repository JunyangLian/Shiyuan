import { useCallback, useEffect, useRef, useState } from "react";
import { api, formatSize } from "../api";
import type { ContentPolicy, InventoryFileTypes } from "../api";

const POLICY_LABEL: Record<ContentPolicy, string> = {
  deep_parse: "深度解析",
  standard_parse: "标准解析",
  metadata_only: "仅元数据",
  exclude_content: "跳过内容",
};

const POLICY_OPTIONS: ContentPolicy[] = [
  "deep_parse",
  "standard_parse",
  "metadata_only",
  "exclude_content",
];

export interface InventorySource {
  source_id: string;
  label: string;
  root_uri: string;
}

export default function InventoryView({
  source,
  onBack,
}: {
  source: InventorySource;
  onBack: () => void;
}) {
  const [data, setData] = useState<InventoryFileTypes | null>(null);
  const [policies, setPolicies] = useState<Record<string, ContentPolicy>>({});
  const [loading, setLoading] = useState(true);
  const [waitingScan, setWaitingScan] = useState(false);
  const [saving, setSaving] = useState(false);
  const [error, setError] = useState<string | null>(null);
  const [note, setNote] = useState<string | null>(null);
  const [showAll, setShowAll] = useState(false);

  const mounted = useRef(true);
  const booted = useRef(false);

  useEffect(() => {
    mounted.current = true;
    return () => {
      mounted.current = false;
    };
  }, []);

  const load = useCallback(async () => {
    setLoading(true);
    setError(null);
    setNote(null);
    try {
      // 刚添加的数据源会立刻启动初次扫描；等 L0 完成再盘点，避免只看到部分文件。
      if (!booted.current) {
        for (let attempt = 0; attempt < 600; attempt += 1) {
          const list = await api.sources();
          if (!mounted.current) return;
          const current = list.items.find((entry) => entry.source_id === source.source_id);
          // L0 完成即可盘点；后续 L1/L2 建库可能持续很久，不必等整轮扫描结束。
          const busy = Boolean(current?.scanning) && !current?.l0_completed_at;
          setWaitingScan(busy);
          if (!busy) break;
          await new Promise((resolve) => window.setTimeout(resolve, 1500));
        }
        if (!mounted.current) return;
      }

      const result = await api.inventoryFileTypes(source.source_id);
      if (!mounted.current) return;
      const initial: Record<string, ContentPolicy> = {};
      for (const item of result.items) {
        if (item.effective_count <= 0) continue;
        initial[item.ext] = result.policies[item.ext] ?? item.recommended_policy;
      }
      setData(result);
      setPolicies(initial);
      booted.current = true;
    } catch (err) {
      if (mounted.current) setError(err instanceof Error ? err.message : String(err));
    } finally {
      if (mounted.current) {
        setLoading(false);
        setWaitingScan(false);
      }
    }
  }, [source.source_id]);

  useEffect(() => {
    void load();
  }, [load]);

  const items = (data?.items ?? []).filter((item) => item.effective_count > 0);
  const visible = showAll ? items : items.slice(0, 28);
  const selected = items.filter(
    (item) => (policies[item.ext] ?? item.recommended_policy) !== "exclude_content",
  );
  const includedFiles = selected.reduce((sum, item) => sum + item.effective_count, 0);
  const includedBytes = selected.reduce((sum, item) => sum + item.effective_bytes, 0);
  const totalFiles = data?.total_files ?? 0;
  const totalBytes = data?.total_bytes ?? 0;
  const systemExcluded = data?.system_excluded_files ?? 0;
  const excludedFiles = Math.max(0, totalFiles - includedFiles);

  const setPolicy = (ext: string, policy: ContentPolicy) => {
    setPolicies((current) => ({ ...current, [ext]: policy }));
    setNote(null);
  };

  const toggle = (ext: string) => {
    const item = items.find((entry) => entry.ext === ext);
    if (!item) return;
    const current = policies[ext] ?? item.recommended_policy;
    if (current === "exclude_content") {
      setPolicy(ext, item.recommended_policy === "exclude_content"
        ? "metadata_only"
        : item.recommended_policy);
    } else {
      setPolicy(ext, "exclude_content");
    }
  };

  const resetRecommended = () => {
    const next: Record<string, ContentPolicy> = {};
    for (const item of items) next[item.ext] = item.recommended_policy;
    setPolicies(next);
    setNote("已恢复推荐策略，尚未保存。");
  };

  const save = async () => {
    setSaving(true);
    setError(null);
    setNote(null);
    try {
      const result = await api.saveInventoryPolicies(source.source_id, policies);
      setData((current) => (current
        ? { ...current, policies: result.policies, confirmed: result.confirmed }
        : current));
      setNote(
        `已为「${source.label}」保存 ${Object.keys(policies).length} 个文件类型的策略，` +
        "并开始按策略建库；被跳过的类型仍保留轻量资产记录。",
      );
    } catch (err) {
      setError(err instanceof Error ? err.message : String(err));
    } finally {
      setSaving(false);
    }
  };

  return (
    <div className="view-section inventory-view">
      <div className="inventory-head">
        <div>
          <button className="ghost small inventory-back" onClick={onBack}>
            ← 返回数据源
          </button>
          <div className="inventory-kicker">盘点与策略</div>
          <h2>{source.label}</h2>
          <p className="sub">
            {source.root_uri}
            <br />
            该数据源的轻量盘点结果。扫描策略只对本数据源生效，不影响其他数据源。
          </p>
        </div>
        <div className="chip-row">
          {data && (
            <span className={`chip ${data.confirmed ? "ok" : "warn"}`}>
              {data.confirmed ? "策略已保存" : "待确认策略"}
            </span>
          )}
          <button className="ghost small" disabled={loading} onClick={() => void load()}>
            {loading ? "盘点中…" : "重新盘点"}
          </button>
        </div>
      </div>

      {error && <div className="alert error">{error}</div>}
      {loading && !data && (
        <div className="notice">
          {waitingScan
            ? "初次扫描进行中，完成后会自动显示该数据源的文件类型…"
            : "正在盘点该数据源的文件类型，首次可能需要十几秒…"}
        </div>
      )}

      {data && items.length === 0 && (
        <div className="notice">
          该数据源还没有可盘点的文件，或初次扫描尚未完成。
        </div>
      )}

      {data && (
        <>
          <div className="inventory-summary">
            <div>
              <span>全部文件</span>
              <strong>{totalFiles}</strong>
            </div>
            <div>
              <span>参与建库</span>
              <strong>{includedFiles}</strong>
            </div>
            <div>
              <span>跳过内容</span>
              <strong>{excludedFiles}</strong>
            </div>
            <div>
              <span>数据体积</span>
              <strong>{formatSize(totalBytes)}</strong>
            </div>
          </div>

          <section className="panel inventory-panel">
            <div className="inventory-panel-head">
              <h3>选择要建库的类型</h3>
              <span className="muted small-text">
                {items.length} 种类型 · 已选 {selected.length} 种 · 参与 {formatSize(includedBytes)}
              </span>
            </div>
            <div className="ext-grid">
              {visible.map((item) => {
                const policy = policies[item.ext] ?? item.recommended_policy;
                const active = policy !== "exclude_content";
                return (
                  <button
                    key={item.ext}
                    className={`ext-chip${active ? " selected" : ""}`}
                    title={
                      `${item.label} · 可配置 ${item.effective_count} 个` +
                      ` · 系统排除 ${item.system_excluded_count} 个` +
                      ` · 推荐 ${POLICY_LABEL[item.recommended_policy]}`
                    }
                    onClick={() => toggle(item.ext)}
                  >
                    <span className="ext-name">{item.label}</span>
                    <span className="ext-count">{item.effective_count}</span>
                  </button>
                );
              })}
            </div>
            {items.length > 28 && (
              <button
                className="ghost small inventory-more"
                onClick={() => setShowAll((value) => !value)}
              >
                {showAll ? "收起" : `显示全部 ${items.length} 种`}
              </button>
            )}
            <div className="inventory-legend">
              青色 = 参与建库；灰色 = 跳过内容。点击类型即可切换。
              系统规则已排除 {systemExcluded} 个文件（Eclipse/IDE/缓存），不参与类型选择。
            </div>
          </section>

          <section className="panel inventory-panel">
            <div className="inventory-panel-head">
              <h3>已选择类型</h3>
              <span className="muted small-text">
                {selected.length} 种 · {includedFiles} 个文件
              </span>
            </div>
            {selected.length === 0 ? (
              <p className="muted">尚未选择任何类型。</p>
            ) : (
              <div className="inventory-type-list">
                {selected.map((item) => {
                  const policy = policies[item.ext] ?? item.recommended_policy;
                  return (
                    <div className="inventory-type-row" key={item.ext}>
                      <span className="inventory-type-name">{item.label}</span>
                      <span className="inventory-type-meta">
                        {item.effective_count} 个 · {formatSize(item.effective_bytes)}
                      </span>
                      <select
                        value={policy}
                        onChange={(event) =>
                          setPolicy(item.ext, event.target.value as ContentPolicy)
                        }
                      >
                        {POLICY_OPTIONS.map((option) => (
                          <option key={option} value={option}>
                            {POLICY_LABEL[option]}
                          </option>
                        ))}
                      </select>
                    </div>
                  );
                })}
              </div>
            )}
          </section>

          <div className="inventory-actions">
            <span className="muted small-text">
              保存后该数据源会按所选策略开始建库；被跳过的类型只保留轻量资产记录。
            </span>
            <div className="chip-row">
              <button className="ghost small" onClick={resetRecommended}>
                恢复推荐
              </button>
              <button
                className="primary small"
                disabled={saving}
                onClick={() => void save()}
              >
                {saving ? "保存中…" : "保存并开始建库"}
              </button>
            </div>
          </div>

          {note && <div className="notice">{note}</div>}
        </>
      )}
    </div>
  );
}
