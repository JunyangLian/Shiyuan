import { useCallback, useEffect, useRef, useState } from "react";
import { api, formatTime } from "../api";
import type {
  AnswerResult,
  AnswerSettings,
  ChatMessage,
  ChatSession,
  EmbeddingSettings,
  QuerySettings,
} from "../api";
import { openFile } from "../tauri";

const PROVIDER_LABEL: Record<string, string> = {
  extractive: "本地摘录",
  openai: "OpenAI 兼容",
  ollama: "Ollama 本机",
};

interface Draft {
  provider: "extractive" | "openai" | "ollama";
  baseUrl: string;
  model: string;
  apiKey: string;
  allowCloud: boolean;
  topK: number;
}

const EMPTY_DRAFT: Draft = {
  provider: "extractive",
  baseUrl: "",
  model: "",
  apiKey: "",
  allowCloud: false,
  topK: 6,
};

const ACTIVE_CHAT_KEY = "shiyuan.chat.active";

function rememberActiveSession(sessionId: string | null) {
  if (sessionId) {
    window.localStorage.setItem(ACTIVE_CHAT_KEY, sessionId);
  } else {
    window.localStorage.removeItem(ACTIVE_CHAT_KEY);
  }
}

function AnswerCard({ result }: { result: AnswerResult }) {
  return (
    <section className="panel chat-answer">
      <div className="panel-head">
        <h2>回答</h2>
        <div className="chip-row">
          <span className={`chip ${result.refused ? "warn" : "ok"}`}>
            {PROVIDER_LABEL[result.provider] ?? result.provider}
          </span>
          {result.model && <span className="chip subtle">{result.model}</span>}
          {result.refused && <span className="chip warn">已拒答</span>}
        </div>
      </div>
      <div className={`answer-text${result.refused ? " refused" : ""}`}>{result.answer}</div>
      {result.warning && <div className="alert warn" style={{ marginTop: 10 }}>{result.warning}</div>}

      {result.citations.length > 0 && (
        <>
          <div className="muted small-text" style={{ marginTop: 14 }}>
            来源（点击「打开」定位原件）
          </div>
          <ul className="citation-list">
            {result.citations.map((citation) => (
              <li key={citation.chunk_id}>
                <span className="chip">[{citation.n}]</span>
                <div className="citation-body">
                  <div className="citation-name">
                    {citation.name}
                    {citation.page != null && <span className="chip subtle">第 {citation.page} 页</span>}
                    {citation.sheet && <span className="chip subtle">工作表 {citation.sheet}</span>}
                    {citation.ocr && <span className="chip subtle">OCR</span>}
                    {citation.semantic && <span className="chip warn">语义</span>}
                    {citation.mtime && (
                      <span className="chip subtle">更新于 {formatTime(citation.mtime)}</span>
                    )}
                  </div>
                  <div className="citation-snippet">{citation.snippet}</div>
                </div>
                <button className="ghost small" onClick={() => void openFile(citation.path)}>
                  打开
                </button>
              </li>
            ))}
          </ul>
        </>
      )}

      <div className="muted small-text" style={{ marginTop: 12 }}>
        覆盖：已解析 {result.coverage.content_indexed} 个文件 · OCR{" "}
        {result.coverage.ocr_indexed} · 待解析 {result.coverage.content_pending} · 无文本层{" "}
        {result.coverage.content_empty}
      </div>
    </section>
  );
}

export default function ChatView({
  embedded = false,
  active = true,
}: {
  embedded?: boolean;
  active?: boolean;
}) {
  const [question, setQuestion] = useState("");
  const [loading, setLoading] = useState(false);
  const [error, setError] = useState<string | null>(null);

  const [sessions, setSessions] = useState<ChatSession[]>([]);
  const [activeId, setActiveId] = useState<string | null>(null);
  const [messages, setMessages] = useState<ChatMessage[]>([]);
  const [messageLoading, setMessageLoading] = useState(false);
  const messagesEndRef = useRef<HTMLDivElement>(null);
  const initialLoadRef = useRef(true);
  const openSeqRef = useRef(0);

  const [showSettings, setShowSettings] = useState(false);
  const [settings, setSettings] = useState<AnswerSettings | null>(null);
  const [draft, setDraft] = useState<Draft>(EMPTY_DRAFT);
  const [saving, setSaving] = useState(false);
  const [saveNote, setSaveNote] = useState<string | null>(null);

  const [embedding, setEmbedding] = useState<EmbeddingSettings | null>(null);
  const [embedDraft, setEmbedDraft] = useState({
    provider: "none" as EmbeddingSettings["provider"],
    baseUrl: "",
    model: "",
    apiKey: "",
    allowCloud: false,
  });
  const [embedSaving, setEmbedSaving] = useState(false);
  const [embedNote, setEmbedNote] = useState<string | null>(null);

  const [queryInfo, setQueryInfo] = useState<QuerySettings | null>(null);
  const [synonymDraft, setSynonymDraft] = useState("");
  const [synonymSaving, setSynonymSaving] = useState(false);
  const [synonymNote, setSynonymNote] = useState<string | null>(null);

  const loadSettings = useCallback(async () => {
    try {
      const [data, vector, query] = await Promise.all([
        api.answerSettings(),
        api.embeddingSettings(),
        api.querySettings(),
      ]);
      setSettings(data);
      setDraft({
        provider: data.provider,
        baseUrl: data.base_url,
        model: data.model,
        apiKey: "",
        allowCloud: data.allow_cloud,
        topK: data.top_k,
      });
      setEmbedding(vector);
      setEmbedDraft({
        provider: vector.provider,
        baseUrl: vector.base_url,
        model: vector.model,
        apiKey: "",
        allowCloud: vector.allow_cloud,
      });
      setQueryInfo(query);
      setSynonymDraft(query.custom.map((group) => group.join("、")).join("\n"));
    } catch {
      setSettings(null);
    }
  }, []);

  useEffect(() => {
    void loadSettings();
  }, [loadSettings]);

  const openSession = useCallback(async (sessionId: string) => {
    initialLoadRef.current = false;
    const seq = openSeqRef.current + 1;
    openSeqRef.current = seq;
    setMessageLoading(true);
    setError(null);
    try {
      const data = await api.chatSession(sessionId);
      if (seq !== openSeqRef.current) return;
      setActiveId(sessionId);
      rememberActiveSession(sessionId);
      setMessages(data.messages);
      setSessions((current) =>
        current.map((item) => (item.session_id === sessionId ? data.session : item)),
      );
    } catch (err) {
      setError(err instanceof Error ? err.message : String(err));
    } finally {
      setMessageLoading(false);
    }
  }, []);

  useEffect(() => {
    let cancelled = false;
    (async () => {
      try {
        const data = await api.chatSessions();
        if (cancelled) return;
        setSessions(data.items);
        if (data.items.length > 0 && initialLoadRef.current) {
          const saved = window.localStorage.getItem(ACTIVE_CHAT_KEY);
          const preferred = data.items.find((item) => item.session_id === saved)
            ?? data.items[0];
          await openSession(preferred.session_id);
        }
      } catch {
        /* 首次启动没有会话或核心未就绪时保持空状态 */
      }
    })();
    return () => {
      cancelled = true;
    };
  }, [openSession]);

  useEffect(() => {
    if (!active) return;
    const end = messagesEndRef.current;
    if (!end) return;
    // 页面滚动容器是外层 main；切回对话标签时 display 刚恢复，延迟一帧再量高度。
    const scrollToEnd = () => {
      const container = end.closest(".main");
      if (container instanceof HTMLElement) {
        container.scrollTop = container.scrollHeight;
      } else {
        end.scrollIntoView({ block: "end" });
      }
    };
    const frame = window.requestAnimationFrame(() => {
      window.requestAnimationFrame(scrollToEnd);
    });
    return () => window.cancelAnimationFrame(frame);
  }, [messages, loading, active]);

  const newSession = async () => {
    setError(null);
    try {
      const created = await api.createChatSession();
      setSessions((current) => [created, ...current]);
      setActiveId(created.session_id);
      rememberActiveSession(created.session_id);
      setMessages([]);
    } catch (err) {
      setError(err instanceof Error ? err.message : String(err));
    }
  };

  const renameSession = async (session: ChatSession) => {
    const title = window.prompt("新的对话名称", session.title);
    if (title === null) return;
    const value = title.trim();
    if (!value || value === session.title) return;
    setError(null);
    try {
      const saved = await api.renameChatSession(session.session_id, value);
      setSessions((current) =>
        current.map((item) => (item.session_id === saved.session_id ? saved : item)),
      );
    } catch (err) {
      setError(err instanceof Error ? err.message : String(err));
    }
  };

  const removeSession = async (session: ChatSession) => {
    if (!window.confirm(`删除对话「${session.title}」？消息记录会一并删除，不影响原文件。`)) {
      return;
    }
    setError(null);
    try {
      await api.deleteChatSession(session.session_id);
      const remaining = sessions.filter((item) => item.session_id !== session.session_id);
      setSessions(remaining);
      if (activeId === session.session_id) {
        if (remaining.length > 0) {
          await openSession(remaining[0].session_id);
        } else {
          setActiveId(null);
          setMessages([]);
          rememberActiveSession(null);
        }
      }
    } catch (err) {
      setError(err instanceof Error ? err.message : String(err));
    }
  };

  const ask = async () => {
    const text = question.trim();
    if (!text || loading) return;
    setLoading(true);
    setError(null);
    try {
      let sessionId = activeId;
      if (!sessionId) {
        const created = await api.createChatSession();
        sessionId = created.session_id;
        setSessions((current) => [created, ...current]);
        setActiveId(sessionId);
        rememberActiveSession(sessionId);
      }
      const data = await api.ask(text, { sessionId });
      setQuestion("");
      setMessages((current) => [
        ...current,
        {
          message_id: `local-user-${Date.now()}`,
          role: "user",
          content: text,
          created_at: new Date().toISOString(),
          payload: null,
        },
        {
          message_id: `local-assistant-${Date.now()}`,
          role: "assistant",
          content: data.answer,
          created_at: new Date().toISOString(),
          payload: data,
        },
      ]);
      const refreshed = await api.chatSessions();
      setSessions(refreshed.items);
    } catch (err) {
      setError(err instanceof Error ? err.message : String(err));
    } finally {
      setLoading(false);
    }
  };

  const saveSettings = async () => {
    setSaving(true);
    setSaveNote(null);
    setError(null);
    try {
      const payload: Record<string, unknown> = {
        provider: draft.provider,
        base_url: draft.baseUrl.trim(),
        model: draft.model.trim(),
        allow_cloud: draft.allowCloud,
        top_k: draft.topK,
      };
      if (draft.apiKey.trim()) payload.api_key = draft.apiKey.trim();
      const saved = await api.saveAnswerSettings(payload);
      setSettings(saved);
      setDraft((current) => ({ ...current, apiKey: "" }));
      setSaveNote("设置已保存。");
    } catch (err) {
      setError(err instanceof Error ? err.message : String(err));
    } finally {
      setSaving(false);
    }
  };

  const cloudRisk = draft.provider === "openai" && draft.allowCloud;

  const saveEmbedding = async () => {
    setEmbedSaving(true);
    setEmbedNote(null);
    setError(null);
    try {
      const payload: Record<string, unknown> = {
        provider: embedDraft.provider,
        base_url: embedDraft.baseUrl.trim(),
        model: embedDraft.model.trim(),
        allow_cloud: embedDraft.allowCloud,
      };
      if (embedDraft.apiKey.trim()) payload.api_key = embedDraft.apiKey.trim();
      const saved = await api.saveEmbeddingSettings(payload);
      setEmbedding(saved);
      setEmbedDraft((current) => ({ ...current, apiKey: "" }));
      setEmbedNote("语义检索设置已保存；扫描时会自动补建向量，也可在健康页手动补建。");
    } catch (err) {
      setError(err instanceof Error ? err.message : String(err));
    } finally {
      setEmbedSaving(false);
    }
  };

  const saveSynonyms = async () => {
    setSynonymSaving(true);
    setSynonymNote(null);
    setError(null);
    try {
      const groups = synonymDraft
        .split("\n")
        .map((line) =>
          line
            .split(/[、,，;；|]/)
            .map((term) => term.trim())
            .filter((term) => term.length > 0),
        )
        .map((group) => Array.from(new Set(group)))
        .filter((group) => group.length >= 2);
      const saved = await api.saveQuerySettings({ custom: groups });
      setQueryInfo(saved);
      setSynonymDraft(saved.custom.map((group) => group.join("、")).join("\n"));
      setSynonymNote(`已保存 ${saved.custom.length} 组自定义同义词。`);
    } catch (err) {
      setError(err instanceof Error ? err.message : String(err));
    } finally {
      setSynonymSaving(false);
    }
  };

  return (
    <div className={embedded ? "view-section chat-view" : "view chat-view"}>
      {!embedded && (
        <header className="view-head">
          <div>
            <h1>知识对话</h1>
            <p className="sub">只依据你授权目录里已解析的正文回答，并逐条标注来源；证据不足会直接拒答。</p>
          </div>
          <div className="chip-row">
            <button className="ghost" onClick={() => void newSession()}>
              新建对话
            </button>
            <button className="ghost" onClick={() => setShowSettings((value) => !value)}>
              {showSettings ? "收起设置" : "回答设置"}
            </button>
          </div>
        </header>
      )}

      {embedded && (
        <div className="section-toolbar">
          <span className="muted small-text">
            对话会自动保存在本机，切换标签或页面不会丢失。
          </span>
          <div className="chip-row">
            <button className="ghost small" onClick={() => void newSession()}>
              新建对话
            </button>
            <button className="ghost small" onClick={() => setShowSettings((value) => !value)}>
              {showSettings ? "收起设置" : "回答设置"}
            </button>
          </div>
        </div>
      )}

      {showSettings && (
        <section className="panel">
          <div className="panel-head">
            <h2>回答设置</h2>
            <span className="muted small-text">
              {settings?.has_key ? "已保存 API Key（不会回显）" : "尚未保存 API Key"}
            </span>
          </div>
          <div className="settings-grid">
            <label htmlFor="provider">回答方式</label>
            <select
              id="provider"
              value={draft.provider}
              onChange={(event) =>
                setDraft({ ...draft, provider: event.target.value as Draft["provider"] })
              }
            >
              <option value="extractive">本地摘录（默认，不联网）</option>
              <option value="openai">OpenAI 兼容接口（云端）</option>
              <option value="ollama">Ollama（本机服务）</option>
            </select>

            <label htmlFor="base-url">接口地址</label>
            <input
              id="base-url"
              type="text"
              value={draft.baseUrl}
              placeholder={
                draft.provider === "ollama" ? "http://127.0.0.1:11434" : "https://api.openai.com/v1"
              }
              onChange={(event) => setDraft({ ...draft, baseUrl: event.target.value })}
            />

            <label htmlFor="model">模型名称</label>
            <input
              id="model"
              type="text"
              value={draft.model}
              placeholder="gpt-4o-mini / qwen2.5:7b"
              onChange={(event) => setDraft({ ...draft, model: event.target.value })}
            />

            <label htmlFor="api-key">API Key</label>
            <input
              id="api-key"
              type="password"
              value={draft.apiKey}
              placeholder={settings?.has_key ? "已保存，留空表示不修改" : "只保存在本机数据库"}
              onChange={(event) => setDraft({ ...draft, apiKey: event.target.value })}
            />

            <label htmlFor="top-k">证据条数</label>
            <input
              id="top-k"
              type="number"
              min={1}
              max={12}
              value={draft.topK}
              onChange={(event) =>
                setDraft({ ...draft, topK: Number(event.target.value) || 6 })
              }
            />

            <label htmlFor="allow-cloud">云端授权</label>
            <label className="checkbox-line" htmlFor="allow-cloud">
              <input
                id="allow-cloud"
                type="checkbox"
                checked={draft.allowCloud}
                onChange={(event) => setDraft({ ...draft, allowCloud: event.target.checked })}
              />
              允许把入选的证据片段发送到上面的接口（不上传整个文件）
            </label>
          </div>
          {cloudRisk && (
            <div className="alert warn" style={{ marginTop: 12 }}>
              已开启云端授权：提问时最多 {draft.topK} 条证据片段会离开本机，请确认接口可信。
            </div>
          )}
          <div className="ask-actions" style={{ marginTop: 14 }}>
            <span className="muted small-text">本地摘录模式不需要任何密钥，也不会发出网络请求。</span>
            <button className="primary small" disabled={saving} onClick={() => void saveSettings()}>
              {saving ? "保存中…" : "保存设置"}
            </button>
          </div>
          {saveNote && <div className="notice" style={{ marginTop: 10 }}>{saveNote}</div>}

          <div className="settings-divider" />
          <div className="panel-head" style={{ marginBottom: 8 }}>
            <h2>语义检索（向量）</h2>
            <span className="muted small-text">
              {embedding?.has_key ? "已保存密钥（不会回显）" : "未保存密钥"}
            </span>
          </div>
          <div className="settings-grid">
            <label htmlFor="embed-provider">向量方式</label>
            <select
              id="embed-provider"
              value={embedDraft.provider}
              onChange={(event) =>
                setEmbedDraft({
                  ...embedDraft,
                  provider: event.target.value as EmbeddingSettings["provider"],
                })
              }
            >
              <option value="none">关闭（仅关键词召回）</option>
              <option value="openai">OpenAI 兼容 embeddings</option>
              <option value="ollama">Ollama（本机服务）</option>
            </select>

            <label htmlFor="embed-base">接口地址</label>
            <input
              id="embed-base"
              type="text"
              value={embedDraft.baseUrl}
              placeholder={
                embedDraft.provider === "ollama"
                  ? "http://127.0.0.1:11434"
                  : "https://api.openai.com/v1"
              }
              onChange={(event) => setEmbedDraft({ ...embedDraft, baseUrl: event.target.value })}
            />

            <label htmlFor="embed-model">向量模型</label>
            <input
              id="embed-model"
              type="text"
              value={embedDraft.model}
              placeholder="text-embedding-3-small / bge-m3"
              onChange={(event) => setEmbedDraft({ ...embedDraft, model: event.target.value })}
            />

            <label htmlFor="embed-key">API Key</label>
            <input
              id="embed-key"
              type="password"
              value={embedDraft.apiKey}
              placeholder={embedding?.has_key ? "已保存，留空表示不修改" : "只保存在本机数据库"}
              onChange={(event) => setEmbedDraft({ ...embedDraft, apiKey: event.target.value })}
            />

            <label htmlFor="embed-consent">云端授权</label>
            <label className="checkbox-line" htmlFor="embed-consent">
              <input
                id="embed-consent"
                type="checkbox"
                checked={embedDraft.allowCloud}
                onChange={(event) =>
                  setEmbedDraft({ ...embedDraft, allowCloud: event.target.checked })
                }
              />
              允许把用于建索引的文本片段发送到上面的向量接口
            </label>
          </div>
          <div className="ask-actions" style={{ marginTop: 12 }}>
            <span className="muted small-text">
              默认关闭；开启后只在上传建索引时联网，检索本身仍在本机完成。
            </span>
            <button className="ghost small" disabled={embedSaving} onClick={() => void saveEmbedding()}>
              {embedSaving ? "保存中…" : "保存语义设置"}
            </button>
          </div>
          {embedNote && <div className="notice" style={{ marginTop: 10 }}>{embedNote}</div>}

          <div className="settings-divider" />
          <div className="panel-head" style={{ marginBottom: 8 }}>
            <h2>自定义同义词</h2>
            <span className="muted small-text">
              内置 {queryInfo?.builtin_groups ?? 0} 组 · 自定义 {queryInfo?.custom.length ?? 0} 组
            </span>
          </div>
          <label className="muted small-text" htmlFor="synonyms">
            每行一组，用顿号或逗号分隔（至少两个词）。保存后搜索与问答同时生效。
          </label>
          <textarea
            id="synonyms"
            className="synonym-input"
            rows={4}
            value={synonymDraft}
            placeholder={"入组人数、招募人数\n测序深度、coverage、覆盖度"}
            onChange={(event) => setSynonymDraft(event.target.value)}
          />
          <div className="ask-actions" style={{ marginTop: 12 }}>
            <span className="muted small-text">只在本机求值时展开，不会上传到任何接口。</span>
            <button className="ghost small" disabled={synonymSaving} onClick={() => void saveSynonyms()}>
              {synonymSaving ? "保存中…" : "保存同义词"}
            </button>
          </div>
          {synonymNote && <div className="notice" style={{ marginTop: 10 }}>{synonymNote}</div>}
        </section>
      )}

      <div className="chat-shell">
        <aside className="chat-sessions">
          <div className="chat-sessions-head">
            <strong>对话列表</strong>
            <button className="ghost small" onClick={() => void newSession()}>
              新建
            </button>
          </div>
          {sessions.length === 0 ? (
            <div className="muted small-text">还没有对话</div>
          ) : (
            <ul className="chat-session-list">
              {sessions.map((session) => (
                <li
                  key={session.session_id}
                  className={session.session_id === activeId ? "active" : ""}
                >
                  <button
                    className="chat-session-main"
                    onClick={() => void openSession(session.session_id)}
                  >
                    <span className="chat-session-title">{session.title}</span>
                    <span className="chat-session-meta">
                      {Math.floor(session.message_count / 2)} 轮 · {formatTime(session.updated_at)}
                    </span>
                  </button>
                  <div className="chat-session-actions">
                    <button onClick={() => void renameSession(session)}>改名</button>
                    <button onClick={() => void removeSession(session)}>删除</button>
                  </div>
                </li>
              ))}
            </ul>
          )}
        </aside>

        <div className="chat-main">
          <div className="chat-messages">
            {messages.map((message) =>
              message.role === "user" ? (
                <div key={message.message_id} className="chat-user">
                  <div className="chat-user-text">{message.content}</div>
                  <div className="chat-time">{formatTime(message.created_at)}</div>
                </div>
              ) : message.payload ? (
                <AnswerCard key={message.message_id} result={message.payload} />
              ) : null,
            )}
            {messageLoading && <div className="notice">正在加载对话…</div>}
            {!messageLoading && messages.length === 0 && (
              <div className="empty">
                <div className="empty-title">
                  {activeId ? "这个对话还没有消息" : "开始一段新对话"}
                </div>
                <p>
                  拾源会先在本机检索已解析的正文，把命中的片段交给回答方式组织答案。
                  没有足够证据时不会编造；不同文件说法不一致时，会把各自的来源一起列出。
                </p>
              </div>
            )}
            {loading && <div className="notice">检索中…</div>}
            <div ref={messagesEndRef} className="chat-end" />
          </div>

          <div className="chat-composer">
            {error && <div className="alert error">{error}</div>}

            <div className="ask-box">
              <textarea
                rows={3}
                value={question}
                placeholder="例如：项目A 的最终样本量是多少？去年 BLAST 结果的阈值设置是什么？"
                onChange={(event) => setQuestion(event.target.value)}
                onKeyDown={(event) => {
                  if (event.key === "Enter" && (event.ctrlKey || event.metaKey)) {
                    event.preventDefault();
                    void ask();
                  }
                }}
              />
            </div>
            <div className="ask-actions">
              <span className="muted small-text">
                Ctrl + Enter 发送；对话会自动保存在本机，切换页面不会丢失。
              </span>
              <button className="primary" disabled={loading || !question.trim()} onClick={() => void ask()}>
                {loading ? "检索中…" : "提问"}
              </button>
            </div>
          </div>
        </div>
      </div>
    </div>
  );
}
