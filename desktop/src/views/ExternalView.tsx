import { useCallback, useEffect, useState } from "react";
import { api, formatTime } from "../api";
import type {
  ExternalClient,
  ExternalClientConfig,
  ExternalPreset,
  ExternalTransport,
} from "../api";

const PRESETS: {
  key: ExternalPreset;
  label: string;
  hint: string;
  transport: "mcp" | "both";
}[] = [
  { key: "codex", label: "Codex", hint: "OpenAI Codex CLI", transport: "mcp" },
  { key: "claude_code", label: "Claude Code", hint: "Anthropic Claude Code", transport: "mcp" },
  { key: "deepseek", label: "DeepSeek Harness", hint: "DeepSeek 本地 Agent", transport: "mcp" },
  { key: "custom", label: "自定义 Agent", hint: "通用 MCP 客户端或 HTTP Agent", transport: "both" },
];

function configText(config: ExternalClientConfig): string {
  if (config.transport === "mcp") return config.mcp?.snippet ?? "";
  if (!config.http) return "";
  const endpoints = config.http.endpoints
    .map((item) => `${item.method} ${item.path}  ${item.description}`)
    .join("\n");
  return (
    `Base URL: ${config.http.base_url}\n` +
    `Authorization: ${config.http.authorization}\n\n${endpoints}`
  );
}

function statusChip(client: ExternalClient): { className: string; label: string } {
  if (client.last_status === "ok") return { className: "chip ok", label: "已连接" };
  if (client.last_status === "failed") return { className: "chip warn", label: "连接失败" };
  return { className: "chip subtle", label: "未测试" };
}

export default function ExternalView() {
  const [clients, setClients] = useState<ExternalClient[]>([]);
  const [loading, setLoading] = useState(true);
  const [error, setError] = useState<string | null>(null);
  const [note, setNote] = useState<string | null>(null);
  const [creating, setCreating] = useState<ExternalPreset | null>(null);
  const [expandedId, setExpandedId] = useState<string | null>(null);
  const [configs, setConfigs] = useState<Record<string, ExternalClientConfig>>({});
  const [configLoadingId, setConfigLoadingId] = useState<string | null>(null);
  const [testingId, setTestingId] = useState<string | null>(null);
  const [copiedId, setCopiedId] = useState<string | null>(null);

  const load = useCallback(async () => {
    try {
      const data = await api.externalClients();
      setClients(data.items);
      setError(null);
    } catch (err) {
      setError(err instanceof Error ? err.message : String(err));
    } finally {
      setLoading(false);
    }
  }, []);

  useEffect(() => {
    void load();
    const timer = window.setInterval(load, 6000);
    return () => window.clearInterval(timer);
  }, [load]);

  const loadConfig = async (id: string) => {
    setConfigLoadingId(id);
    try {
      const config = await api.externalClientConfig(id);
      setConfigs((current) => ({ ...current, [id]: config }));
      return config;
    } catch (err) {
      setError(err instanceof Error ? err.message : String(err));
      return null;
    } finally {
      setConfigLoadingId(null);
    }
  };

  const create = async (preset: ExternalPreset, transport: ExternalTransport = "mcp") => {
    setCreating(preset);
    setError(null);
    setNote(null);
    try {
      const result = await api.createExternalClient({ preset, transport });
      setClients((current) => [result.client, ...current]);
      setExpandedId(result.client.client_id);
      await loadConfig(result.client.client_id);
      setNote(`已添加「${result.client.name}」。复制下方配置到对应 Agent 即可。`);
    } catch (err) {
      setError(err instanceof Error ? err.message : String(err));
    } finally {
      setCreating(null);
    }
  };

  const toggleConfig = async (id: string) => {
    if (expandedId === id) {
      setExpandedId(null);
      return;
    }
    setExpandedId(id);
    if (!configs[id]) await loadConfig(id);
  };

  const test = async (client: ExternalClient) => {
    setTestingId(client.client_id);
    setNote(null);
    try {
      const result = await api.testExternalClient(client.client_id);
      setClients((current) =>
        current.map((item) =>
          item.client_id === client.client_id
            ? {
                ...item,
                last_test_at: new Date().toISOString(),
                last_status: result.status,
                last_error: result.ok ? null : result.message,
              }
            : item,
        ),
      );
      setNote(`${client.name}：${result.message}（${result.latency_ms} ms）`);
    } catch (err) {
      setError(err instanceof Error ? err.message : String(err));
    } finally {
      setTestingId(null);
    }
  };

  const remove = async (client: ExternalClient) => {
    const confirmed = window.confirm(
      `删除「${client.name}」？\n\n删除后这个 Agent 的令牌会立即失效，但不会动知识库文件。`,
    );
    if (!confirmed) return;
    try {
      await api.deleteExternalClient(client.client_id);
      setClients((current) => current.filter((item) => item.client_id !== client.client_id));
      if (expandedId === client.client_id) setExpandedId(null);
      setNote(`已删除「${client.name}」，令牌已失效。`);
    } catch (err) {
      setError(err instanceof Error ? err.message : String(err));
    }
  };

  const copy = async (id: string) => {
    const config = configs[id] ?? (await loadConfig(id));
    if (!config) return;
    try {
      await navigator.clipboard.writeText(configText(config));
      setCopiedId(id);
      window.setTimeout(() => setCopiedId((value) => (value === id ? null : value)), 1600);
    } catch {
      setError("复制失败；可以手动选中配置文本复制。");
    }
  };

  return (
    <div className="view external-view">
      <header className="view-head">
        <div>
          <p className="eyebrow">Agent 接入</p>
          <h1>外部调用</h1>
          <p className="sub">
            把拾源知识库以只读方式连接给本机 Agent。每个连接使用独立令牌，可单独测试和撤销。
          </p>
        </div>
        <span className="chip ok">只读访问</span>
      </header>

      {error && <div className="alert error">{error}</div>}
      {note && <div className="notice">{note}</div>}

      <div className="external-presets">
        {PRESETS.map((preset) => (
          <article className="panel external-preset" key={preset.key}>
            <div>
              <h3>{preset.label}</h3>
              <p className="muted small-text">{preset.hint}</p>
            </div>
            <div className="chip-row">
              <span className="chip subtle">
                {preset.transport === "both" ? "MCP / HTTP" : "MCP · stdio"}
              </span>
              {preset.transport === "both" ? (
                <div className="chip-row">
                  <button
                    className="ghost small"
                    disabled={creating !== null}
                    onClick={() => void create(preset.key, "mcp")}
                  >
                    MCP
                  </button>
                  <button
                    className="ghost small"
                    disabled={creating !== null}
                    onClick={() => void create(preset.key, "http")}
                  >
                    HTTP
                  </button>
                </div>
              ) : (
                <button
                  className="primary small"
                  disabled={creating !== null}
                  onClick={() => void create(preset.key, "mcp")}
                >
                  {creating === preset.key ? "添加中…" : "添加连接"}
                </button>
              )}
            </div>
          </article>
        ))}
      </div>

      <section className="panel inventory-panel">
        <div className="inventory-panel-head">
          <h3>已添加的连接</h3>
          <span className="muted small-text">{clients.length} 个 · 令牌只保存在本机</span>
        </div>

        {loading && clients.length === 0 ? (
          <p className="muted">正在读取连接…</p>
        ) : clients.length === 0 ? (
          <div className="empty">
            <div className="empty-title">还没有外部连接</div>
            <p>选择上面的 Agent 品牌添加一个；连接后可在对应 Agent 里检索知识库。</p>
          </div>
        ) : (
          <div className="external-list">
            {clients.map((client) => {
              const status = statusChip(client);
              const config = configs[client.client_id];
              return (
                <div className="external-client" key={client.client_id}>
                  <div className="external-client-main">
                    <div className="external-client-title">
                      <strong>{client.name}</strong>
                      <span className="chip">{client.preset_label}</span>
                      <span className="chip">{client.transport_label}</span>
                      <span className={status.className}>{status.label}</span>
                    </div>
                    <div className="external-client-meta">
                      创建于 {formatTime(client.created_at)} · 调用 {client.call_count} 次
                      {client.last_call_at ? ` · 最近 ${formatTime(client.last_call_at)}` : ""}
                      {client.last_test_at ? ` · 测试 ${formatTime(client.last_test_at)}` : ""}
                    </div>
                    {client.last_status === "failed" && client.last_error && (
                      <div className="run-line error-text">{client.last_error}</div>
                    )}
                  </div>
                  <div className="source-actions">
                    <button
                      className="ghost small"
                      onClick={() => void toggleConfig(client.client_id)}
                    >
                      {expandedId === client.client_id ? "收起配置" : "查看配置"}
                    </button>
                    <button
                      className="ghost small strong"
                      disabled={testingId === client.client_id}
                      onClick={() => void test(client)}
                    >
                      {testingId === client.client_id ? "测试中…" : "测试连接"}
                    </button>
                    <button
                      className="ghost small danger"
                      onClick={() => void remove(client)}
                    >
                      删除
                    </button>
                  </div>

                  {expandedId === client.client_id && (
                    <div className="external-config">
                      {configLoadingId === client.client_id ? (
                        <p className="muted">正在读取配置…</p>
                      ) : config ? (
                        <>
                          <div className="external-config-head">
                            <span className="muted small-text">
                              {config.transport === "mcp"
                                ? "MCP 客户端配置（JSON）"
                                : "HTTP 接口信息"}
                            </span>
                            <button
                              className="ghost small"
                              onClick={() => void copy(client.client_id)}
                            >
                              {copiedId === client.client_id ? "已复制" : "复制配置"}
                            </button>
                          </div>
                          <pre>{configText(config)}</pre>
                          {config.mcp && (
                            <div className="external-command">
                              启动命令：
                              <code>
                                {config.mcp.command} {config.mcp.args.join(" ")}
                              </code>
                            </div>
                          )}
                          <ul className="external-notes">
                            {config.notes.map((item) => (
                              <li key={item}>{item}</li>
                            ))}
                          </ul>
                        </>
                      ) : (
                        <p className="muted">配置读取失败。</p>
                      )}
                    </div>
                  )}
                </div>
              );
            })}
          </div>
        )}
      </section>

      <div className="notice">
        MCP 与 HTTP 都只读取已建库内容：不会扫描目录、不会修改原文件。
        新文件仍需先在「数据」模块里完成扫描与策略建库。
      </div>
    </div>
  );
}
