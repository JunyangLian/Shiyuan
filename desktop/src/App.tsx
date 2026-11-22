import { useEffect, useState } from "react";
import { api } from "./api";
import WorkspaceView from "./views/WorkspaceView";
import LibraryView from "./views/LibraryView";

type View = "workspace" | "library";

const NAV: { key: View; label: string; hint: string }[] = [
  { key: "workspace", label: "检索与对话", hint: "找文件 · 问答" },
  { key: "library", label: "数据源与维护", hint: "授权 · 同步" },
];

export default function App() {
  const [view, setView] = useState<View>("workspace");
  const [online, setOnline] = useState<boolean | null>(null);
  const [version, setVersion] = useState("");

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
    <div className="app">
      <aside className="sidebar">
        <div className="brand">
          <span className="brand-mark">拾</span>
          <div>
            <div className="brand-name">拾源</div>
            <div className="brand-sub">原位知识工作台</div>
          </div>
        </div>

        <nav className="nav">
          {NAV.map((item) => (
            <button
              key={item.key}
              className={`nav-item${view === item.key ? " active" : ""}`}
              onClick={() => setView(item.key)}
            >
              <span className="nav-label">{item.label}</span>
              <span className="nav-hint">{item.hint}</span>
            </button>
          ))}
        </nav>

        <div className="sidebar-foot">
          <div className={`status-dot ${online ? "ok" : online === false ? "bad" : ""}`} />
          <div>
            <div className="foot-line">{online === null ? "连接本地核心…" : online ? "本地核心已就绪" : "本地核心未启动"}</div>
            <div className="foot-sub">v{version || "0.1.0"} · 默认离线</div>
          </div>
        </div>
      </aside>

      <main className="main">
        {view === "workspace" && <WorkspaceView onGoLibrary={() => setView("library")} />}
        {view === "library" && <LibraryView />}
      </main>
    </div>
  );
}
