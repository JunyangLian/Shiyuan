import { useCallback, useEffect, useState } from "react";
import { api, formatTime } from "../api";
import type { ParseResultStatus, ParseResults } from "../api";
import { openFile } from "../tauri";
import type { InventorySource } from "./InventoryView";
import Icon from "../Icon";

type Filter = "all" | ParseResultStatus;

const FILTERS: { key: Filter; label: string }[] = [
  { key: "all", label: "全部" },
  { key: "ok", label: "解析成功" },
  { key: "failed", label: "失败记录" },
  { key: "empty", label: "无文本" },
  { key: "pending", label: "待解析" },
  { key: "locked", label: "被占用" },
  { key: "unsupported", label: "暂不支持" },
];

const STATUS_LABEL: Record<ParseResultStatus, string> = {
  ok: "解析成功",
  failed: "解析失败",
  empty: "无文本",
  pending: "待解析",
  locked: "文件被占用",
  unsupported: "暂不支持",
};

const STATUS_CHIP: Record<ParseResultStatus, string> = {
  ok: "chip ok",
  failed: "chip warn",
  empty: "chip subtle",
  pending: "chip subtle",
  locked: "chip warn",
  unsupported: "chip subtle",
};

const STATUS_HINT: Record<ParseResultStatus, string> = {
  ok: "",
  failed: "",
  empty: "没有可提取的文本；扫描件可尝试 OCR",
  pending: "尚未进入解析队列",
  locked: "解析时文件被其他程序占用",
  unsupported: "当前解析器暂不支持此类型",
};

function stageLabel(stage: string): string {
  if (stage === "L1") return "正文";
  if (stage === "L2") return "OCR";
  return "正文 + OCR";
}

export default function ParseResultsView({
  source,
  onBack,
}: {
  source: InventorySource;
  onBack: () => void;
}) {
  const [data, setData] = useState<ParseResults | null>(null);
  const [filter, setFilter] = useState<Filter>("all");
  const [loading, setLoading] = useState(true);
  const [loadingMore, setLoadingMore] = useState(false);
  const [error, setError] = useState<string | null>(null);

  const load = useCallback(async (nextFilter: Filter) => {
    setLoading(true);
    setError(null);
    try {
      const result = await api.sourceParseResults(source.source_id, {
        status: nextFilter === "all" ? undefined : nextFilter,
        limit: 100,
      });
      setData(result);
    } catch (err) {
      setError(err instanceof Error ? err.message : String(err));
    } finally {
      setLoading(false);
    }
  }, [source.source_id]);

  useEffect(() => {
    void load(filter);
  }, [filter, load]);

  const loadMore = async () => {
    if (!data) return;
    setLoadingMore(true);
    try {
      const result = await api.sourceParseResults(source.source_id, {
        status: filter === "all" ? undefined : filter,
        limit: 100,
        offset: data.items.length,
      });
      setData((current) => current
        ? { ...result, items: [...current.items, ...result.items] }
        : result);
    } catch (err) {
      setError(err instanceof Error ? err.message : String(err));
    } finally {
      setLoadingMore(false);
    }
  };

  const summary = data?.summary;
  const counts: Record<Filter, number> = {
    all: summary?.total ?? 0,
    ok: summary?.parsed ?? 0,
    failed: summary?.failed ?? 0,
    empty: summary?.empty ?? 0,
    pending: summary?.pending ?? 0,
    locked: summary?.locked ?? 0,
    unsupported: summary?.unsupported ?? 0,
  };
  const currentLabel = FILTERS.find((item) => item.key === filter)?.label ?? "全部";

  return (
    <div className="view-section parse-results-view">
      <div className="inventory-head">
        <div>
          <button className="ghost small icon-label inventory-back" onClick={onBack}>
            <Icon name="arrow-left" size={15} />
            返回数据源
          </button>
          <div className="inventory-kicker">解析结果</div>
          <h2>{source.label}</h2>
          <p className="sub">
            {source.root_uri}
            <br />
            逐文件查看解析成功、失败与待处理记录。
          </p>
        </div>
        <button className="ghost small" disabled={loading} onClick={() => void load(filter)}>
          {loading ? "刷新中…" : "刷新"}
        </button>
      </div>

      {error && <div className="alert error">{error}</div>}

      <div className="inventory-summary">
        <div>
          <span>解析成功</span>
          <strong>{summary?.parsed ?? 0}</strong>
        </div>
        <div>
          <span>失败记录</span>
          <strong>{summary?.failed ?? 0}</strong>
        </div>
        <div>
          <span>无文本</span>
          <strong>{summary?.empty ?? 0}</strong>
        </div>
        <div>
          <span>待解析</span>
          <strong>{summary?.pending ?? 0}</strong>
        </div>
      </div>

      <section className="panel inventory-panel">
        <div className="inventory-panel-head">
          <h3>{currentLabel}</h3>
          <span className="muted small-text">
            {data ? `${data.total} 条` : "正在读取…"}
          </span>
        </div>

        <div className="parse-filters">
          {FILTERS.map((item) => (
            <button
              key={item.key}
              className={`parse-filter${filter === item.key ? " active" : ""}`}
              onClick={() => setFilter(item.key)}
            >
              {item.label}
              <span>{counts[item.key]}</span>
            </button>
          ))}
        </div>

        {loading && !data ? (
          <p className="muted">正在读取解析结果…</p>
        ) : data && data.items.length === 0 ? (
          <div className="empty">
            <div className="empty-title">暂无{filter === "all" ? "记录" : currentLabel}</div>
            <p>开始深度解析后，逐文件的处理结果会出现在这里。</p>
          </div>
        ) : (
          <ul className="parse-list">
            {data?.items.map((item) => (
              <li key={item.location_id} className="parse-item">
                <div className="parse-item-main">
                  <div className="parse-item-title">
                    <span className="parse-item-name" title={item.path}>
                      {item.name}
                    </span>
                    <span className={STATUS_CHIP[item.status]}>
                      {STATUS_LABEL[item.status]}
                    </span>
                    {item.stage && (
                      <span className="chip subtle">{stageLabel(item.stage)}</span>
                    )}
                  </div>
                  <div className="parse-item-path" title={item.path}>
                    {item.rel_path || item.path}
                  </div>
                  {item.error ? (
                    <div className="parse-item-error">{item.error}</div>
                  ) : (
                    <div className="parse-item-meta">
                      {item.status === "ok"
                        ? `${item.chars} 字${item.pages ? ` · ${item.pages} 页` : ""}`
                        : STATUS_HINT[item.status]}
                      {item.updated_at ? ` · ${formatTime(item.updated_at)}` : ""}
                    </div>
                  )}
                </div>
                <button
                  className="ghost small"
                  onClick={() => void openFile(item.path)}
                >
                  打开
                </button>
              </li>
            ))}
          </ul>
        )}

        {data && data.items.length < data.total && (
          <button
            className="ghost small parse-more"
            disabled={loadingMore}
            onClick={() => void loadMore()}
          >
            {loadingMore
              ? "加载中…"
              : `加载更多（已显示 ${data.items.length}/${data.total}）`}
          </button>
        )}
      </section>
    </div>
  );
}
