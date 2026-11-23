import { useCallback, useEffect, useRef, useState } from "react";
import ReactMarkdown from "react-markdown";
import remarkGfm from "remark-gfm";
import { api, formatTime } from "../api";
import type {
  AnswerResult,
  AnswerSettings,
  ChatMessage,
  ChatSession,
  Citation,
  DocumentFamilyMember,
  EmbeddingSettings,
  QuerySettings,
} from "../api";
import { openFile } from "../tauri";

const PROVIDER_LABEL: Record<string, string> = {
  extractive: "本地摘录",
  openai: "OpenAI 兼容",
  ollama: "Ollama 本机",
  library: "本地统计",
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
const SESSIONS_COLLAPSED_KEY = "shiyuan.chat.sessions.collapsed";

function rememberActiveSession(sessionId: string | null) {
  if (sessionId) {
    window.localStorage.setItem(ACTIVE_CHAT_KEY, sessionId);
  } else {
    window.localStorage.removeItem(ACTIVE_CHAT_KEY);
  }
}

function CitationRow({
  citation,
  inFamily = false,
}: {
  citation: Citation;
  inFamily?: boolean;
}) {
  return (
    <li>
      <span className="chip citation-index">[{citation.n}]</span>
      <div className="citation-body">
        <div className="citation-name">
          {citation.name}
          {citation.page != null && <span className="chip subtle">第 {citation.page} 页</span>}
          {citation.sheet && <span className="chip subtle">工作表 {citation.sheet}</span>}
          {citation.ocr && <span className="chip subtle">OCR</span>}
          {citation.semantic && <span className="chip warn">语义</span>}
          {!inFamily && citation.family_size != null && citation.family_size > 1 && (
            <span className="chip subtle">同一文档 {citation.family_size} 个版本</span>
          )}
          {citation.mtime && (
            <span className="chip subtle">更新于 {formatTime(citation.mtime)}</span>
          )}
        </div>
        <div className="citation-snippet">{citation.snippet}</div>
        {citation.sources && citation.sources.length > 1 && (
          <div className="citation-sources">
            同源版本：
            {citation.sources
              .slice(0, 6)
              .map((source) => source.name ?? source.path ?? "—")
              .join("、")}
            {citation.sources.length > 6
              ? ` 等 ${citation.sources.length} 个版本`
              : ""}
          </div>
        )}
      </div>
      <button className="ghost small" onClick={() => void openFile(citation.path)}>
        打开
      </button>
    </li>
  );
}

interface FamilyCitationGroup {
  familyId: string;
  title: string;
  size: number;
  citations: Citation[];
}

function AnswerCard({ result }: { result: AnswerResult }) {
  const familyGroups: FamilyCitationGroup[] = [];
  const groupIndex = new Map<string, number>();
  const standalone: Citation[] = [];

  for (const citation of result.citations) {
    if (citation.family_id && (citation.family_size ?? 1) > 1) {
      const index = groupIndex.get(citation.family_id);
      if (index === undefined) {
        groupIndex.set(citation.family_id, familyGroups.length);
        familyGroups.push({
          familyId: citation.family_id,
          title: citation.family_title || citation.name,
          size: citation.family_size ?? 1,
          citations: [citation],
        });
      } else {
        familyGroups[index].citations.push(citation);
      }
    } else {
      standalone.push(citation);
    }
  }

  const units: ({ order: number; group: FamilyCitationGroup }
    | { order: number; citation: Citation })[] = [
    ...familyGroups.map((group) => ({ order: group.citations[0].n, group })),
    ...standalone.map((citation) => ({ order: citation.n, citation })),
  ];
  units.sort((first, second) => first.order - second.order);

  const [openFamily, setOpenFamily] = useState<FamilyCitationGroup | null>(null);
  const [familyMembers, setFamilyMembers] = useState<DocumentFamilyMember[]>([]);
  const [familyLoading, setFamilyLoading] = useState(false);
  const [familyError, setFamilyError] = useState<string | null>(null);

  const showFamilyVersions = async (group: FamilyCitationGroup) => {
    setOpenFamily(group);
    setFamilyMembers([]);
    setFamilyError(null);
    setFamilyLoading(true);
    try {
      const primary = group.citations[0];
      const anchor = primary?.asset_id || primary?.sources?.[0]?.asset_id;
      // 家族 ID 会在重建后变化，优先按文件解析当前家族。
      const data = anchor
        ? await api.familyMembersByAsset(anchor)
        : await api.documentFamilyMembers(group.familyId);
      setFamilyMembers(data.members);
      setOpenFamily((current) => (current
        ? {
          ...current,
          familyId: data.family_id,
          title: data.title || current.title,
          size: data.members.length || current.size,
        }
        : current));
    } catch (err) {
      setFamilyError(err instanceof Error ? err.message : String(err));
    } finally {
      setFamilyLoading(false);
    }
  };

  useEffect(() => {
    if (!openFamily) return;
    const onKey = (event: KeyboardEvent) => {
      if (event.key === "Escape") setOpenFamily(null);
    };
    window.addEventListener("keydown", onKey);
    return () => window.removeEventListener("keydown", onKey);
  }, [openFamily]);

  return (
    <section className="chat-answer">
      <div className="chat-answer-head">
        <span className="assistant-mark">拾</span>
        <span className="assistant-name">拾源</span>
        <span className={`chip ${result.refused ? "warn" : "ok"}`}>
          {result.reason === "source_recall"
            ? "数据源记忆"
            : PROVIDER_LABEL[result.provider] ?? result.provider}
        </span>
        {result.model && <span className="chip subtle">{result.model}</span>}
        {result.refused && <span className="chip warn">已拒答</span>}
      </div>
      <div className={`answer-text${result.refused ? " refused" : ""}`}>
        <ReactMarkdown remarkPlugins={[remarkGfm]}>{result.answer}</ReactMarkdown>
      </div>
      {result.warning && <div className="alert warn answer-warning">{result.warning}</div>}

      {result.plan && (
        <div className="answer-plan">
          <span className="chip subtle">{result.plan.label}</span>
          <span className="muted small-text">
            候选 {result.plan.candidates ?? 0} · 采用 {result.plan.used ?? result.citations.length}
            {" "}· 覆盖 {result.plan.families ?? 0} 个家族
          </span>
          {(result.plan.missing?.length ?? 0) > 0 && (
            <span className="chip warn" title="这些关键词没有在证据中出现">
              证据缺口：{result.plan.missing!.join("、")}
            </span>
          )}
        </div>
      )}

      {result.citations.length > 0 && (
        <div className="answer-sources">
          <div className="answer-sources-head">
            来源 · {result.citations.length}
            {familyGroups.length > 0 && (
              <span className="muted small-text">
                {" "}· 归并为 {familyGroups.length} 个文档家族
              </span>
            )}
          </div>
          <ul className="citation-list">
            {units.map((unit) => (
              "group" in unit ? (
                <li key={unit.group.familyId} className="citation-family">
                  <div className="citation-family-head">
                    <span className="citation-family-title">{unit.group.title}</span>
                    <button
                      type="button"
                      className="chip accent family-chip-button"
                      title="查看全部关联版本"
                      aria-label={`查看 ${unit.group.title} 的 ${unit.group.size} 个关联版本`}
                      onClick={() => void showFamilyVersions(unit.group)}
                    >
                      {unit.group.size} 个关联版本
                    </button>
                    <span className="muted small-text">
                      本回答命中 {unit.group.citations.length} 段，重复段落已合并
                    </span>
                  </div>
                  <ul className="citation-family-list">
                    {unit.group.citations.map((citation) => (
                      <CitationRow key={citation.chunk_id} citation={citation} inFamily />
                    ))}
                  </ul>
                </li>
              ) : (
                <CitationRow key={unit.citation.chunk_id} citation={unit.citation} />
              )
            ))}
          </ul>
        </div>
      )}

      <div className="answer-coverage">
        覆盖：已解析 {result.coverage.content_indexed} 个文件 · OCR {result.coverage.ocr_indexed} ·
        待解析 {result.coverage.content_pending} · 无文本层 {result.coverage.content_empty}
      </div>

      {openFamily && (
        <div
          className="family-modal-backdrop"
          role="presentation"
          onClick={() => setOpenFamily(null)}
        >
          <div
            className="family-modal"
            role="dialog"
            aria-modal="true"
            aria-label={`${openFamily.title} 的关联版本`}
            onClick={(event) => event.stopPropagation()}
          >
            <div className="family-modal-head">
              <div>
                <h3>{openFamily.title}</h3>
                <p className="muted small-text">
                  {openFamily.size} 个关联版本 · 原文件保持不变，拾源不判断哪个版本更好
                </p>
              </div>
              <button className="ghost small" onClick={() => setOpenFamily(null)}>
                关闭
              </button>
            </div>

            {familyError && <div className="alert error">{familyError}</div>}
            {familyLoading ? (
              <p className="muted small-text family-modal-loading">正在读取版本列表…</p>
            ) : (
              <ul className="family-version-list">
                {familyMembers.map((member) => (
                  <li key={member.asset_id} className="family-version-item">
                    <div className="family-version-main">
                      <span className="family-version-name" title={member.path ?? ""}>
                        {member.name || member.path || member.asset_id}
                      </span>
                      <span className="chip subtle">
                        {member.version_label || "未标注版本"}
                      </span>
                    </div>
                    <div className="family-version-path" title={member.path ?? ""}>
                      {member.rel_path || member.path || "路径不可用"}
                    </div>
                    {member.path && (
                      <button
                        className="ghost small"
                        onClick={() => void openFile(member.path as string)}
                      >
                        打开
                      </button>
                    )}
                  </li>
                ))}
              </ul>
            )}
          </div>
        </div>
      )}
    </section>
  );
}

export default function ChatView({ active = true }: { active?: boolean }) {
  const [question, setQuestion] = useState("");
  const [loading, setLoading] = useState(false);
  const [error, setError] = useState<string | null>(null);

  const [sessions, setSessions] = useState<ChatSession[]>([]);
  const [activeId, setActiveId] = useState<string | null>(null);
  const [messages, setMessages] = useState<ChatMessage[]>([]);
  const [messageLoading, setMessageLoading] = useState(false);
  const [sessionsCollapsed, setSessionsCollapsed] = useState(
    () => window.localStorage.getItem(SESSIONS_COLLAPSED_KEY) === "1",
  );
  const messagesEndRef = useRef<HTMLDivElement>(null);
  const initialLoadRef = useRef(true);
  const openSeqRef = useRef(0);

  useEffect(() => {
    window.localStorage.setItem(SESSIONS_COLLAPSED_KEY, sessionsCollapsed ? "1" : "0");
  }, [sessionsCollapsed]);

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

  useEffect(() => {
    if (!showSettings) return;
    const end = messagesEndRef.current;
    if (!end) return;
    const frame = window.requestAnimationFrame(() => {
      const container = end.closest(".main");
      if (container instanceof HTMLElement) {
        container.scrollTop = container.scrollHeight;
      }
    });
    return () => window.cancelAnimationFrame(frame);
  }, [showSettings]);

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

  const activeSession = sessions.find((item) => item.session_id === activeId) ?? null;

  return (
    <div className="chat-view">
      <div className={`chat-shell${sessionsCollapsed ? " sessions-collapsed" : ""}`}>
        <aside className="chat-sessions">
          <div className="chat-sessions-head">
            {!sessionsCollapsed && <strong>对话</strong>}
            {!sessionsCollapsed && (
              <button className="ghost small" onClick={() => void newSession()}>
                新建
              </button>
            )}
            <button
              className="icon-btn sessions-toggle"
              onClick={() => setSessionsCollapsed((value) => !value)}
              aria-label={sessionsCollapsed ? "展开对话列表" : "收起对话列表"}
              title={sessionsCollapsed ? "展开对话列表" : "收起对话列表"}
            >
              {sessionsCollapsed ? "›" : "‹"}
            </button>
          </div>
          {sessionsCollapsed ? (
            <div className="sessions-rail">对话</div>
          ) : sessions.length === 0 ? (
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
          <div className="chat-head">
            <div>
              <h2>{activeSession?.title ?? "知识对话"}</h2>
              <p className="chat-head-sub">
                {messageLoading
                  ? "正在加载对话…"
                  : activeSession
                    ? `${Math.floor(activeSession.message_count / 2)} 轮 · 更新于 ${formatTime(activeSession.updated_at)}`
                    : "只依据已解析的正文回答，并标注来源"}
              </p>
            </div>
          </div>

          {showSettings && (
        <section className="panel settings-panel">
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
            <div ref={messagesEndRef} className="chat-end" />
          </div>

          <div className="chat-composer">
            {error && <div className="alert error">{error}</div>}

            <div className="composer-box">
              <textarea
                rows={2}
                value={question}
                placeholder="找点啥？"
                onChange={(event) => setQuestion(event.target.value)}
                onKeyDown={(event) => {
                  if (event.key === "Enter" && (event.ctrlKey || event.metaKey)) {
                    event.preventDefault();
                    void ask();
                  }
                }}
              />
              <div className="composer-bar">
                <span className="composer-hint">Ctrl + Enter 发送 · 对话自动保存在本机</span>
                <div className="composer-actions">
                  <button
                    className="composer-action"
                    onClick={() => setShowSettings((value) => !value)}
                  >
                    {showSettings ? "收起设置" : "回答设置"}
                  </button>
                  <button
                    className="primary small"
                    disabled={loading || !question.trim()}
                    onClick={() => void ask()}
                  >
                    {loading ? "检索中…" : "提问"}
                  </button>
                </div>
              </div>
            </div>
          </div>
        </div>
      </div>
    </div>
  );
}
