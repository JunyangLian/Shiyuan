import { useEffect, useRef, useState } from "react";
import { api } from "./api";
import ChatView from "./views/ChatView";
import LibraryView from "./views/LibraryView";
import ExternalView from "./views/ExternalView";
import HealthView from "./views/HealthView";

type View = "chat" | "library" | "external" | "health";

const SIDEBAR_COLLAPSED_KEY = "shiyuan.sidebar.collapsed";

const NAV: { key: View; label: string; hint: string; icon: string }[] = [
  { key: "library", label: "数据", hint: "授权 · 同步", icon: "数" },
  { key: "chat", label: "对话", hint: "知识库问答", icon: "话" },
  { key: "external", label: "外部调用", hint: "Agent · 只读", icon: "外" },
  { key: "health", label: "更新", hint: "索引 · 覆盖", icon: "更" },
];

export default function App() {
  const [view, setView] = useState<View>("chat");
  const [online, setOnline] = useState<boolean | null>(null);
  const [version, setVersion] = useState("");
  const [sidebarCollapsed, setSidebarCollapsed] = useState(
    () => window.localStorage.getItem(SIDEBAR_COLLAPSED_KEY) === "1",
  );
  const mainRef = useRef<HTMLElement>(null);

  const changeView = (next: View) => {
    setView(next);
    if (mainRef.current) mainRef.current.scrollTop = 0;
  };

  useEffect(() => {
    window.localStorage.setItem(SIDEBAR_COLLAPSED_KEY, sidebarCollapsed ? "1" : "0");
  }, [sidebarCollapsed]);

  useEffect(() => {
    let alive = true;
    const check = async () => {
      try {
        const health = await api.health();
        if (!alive) return;
        setOnline(true);
        setVersion(health.version);
      } catch {
        if (alive) setOnline(false);
      }
    };
    void check();
    const timer = window.setInterval(check, 10000);
    return () => {
      alive = false;
      window.clearInterval(timer);
    };
  }, []);

  return (
    <div className={`app${sidebarCollapsed ? " sidebar-collapsed" : ""}`}>
      <aside className="sidebar">
        <div className="brand">
          <span className="brand-mark" title="拾源">拾</span>
          <div className="brand-text">
            <div className="brand-name">拾源</div>
            <div className="brand-sub">原位知识工作台</div>
          </div>
          <button
            className="sidebar-toggle"
            onClick={() => setSidebarCollapsed((value) => !value)}
            aria-label={sidebarCollapsed ? "展开导航" : "收起导航"}
            title={sidebarCollapsed ? "展开导航" : "收起导航"}
          >
            {sidebarCollapsed ? "›" : "‹"}
          </button>
        </div>

        <nav className="nav" aria-label="主导航">
          {NAV.map((item) => (
            <button
              key={item.key}
              className={`nav-item${view === item.key ? " active" : ""}`}
              onClick={() => changeView(item.key)}
              title={item.label}
            >
              <span className="nav-icon" aria-hidden="true">{item.icon}</span>
              <span className="nav-text">
                <span className="nav-label">{item.label}</span>
                <span className="nav-hint">{item.hint}</span>
              </span>
            </button>
          ))}
        </nav>

        <div className="sidebar-foot">
          <span className={`status-dot ${online ? "ok" : online === false ? "bad" : ""}`} />
          <div>
            <div className="foot-line">
              {online === null ? "连接本地核心…" : online ? "本地核心已就绪" : "本地核心未启动"}
            </div>
            <div className="foot-sub">v{version || "0.1.3"} · 默认离线</div>
          </div>
        </div>
      </aside>

      <main className="main" ref={mainRef}>
        {view === "chat" ? (
          <ChatView />
        ) : view === "library" ? (
          <div className="page">
            <LibraryView />
          </div>
        ) : view === "external" ? (
          <div className="page">
            <ExternalView />
          </div>
        ) : (
          <div className="page">
            <HealthView />
          </div>
        )}
      </main>
    </div>
  );
}
