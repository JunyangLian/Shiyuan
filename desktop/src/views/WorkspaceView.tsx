import { useEffect, useState } from "react";
import SearchView from "./SearchView";
import ChatView from "./ChatView";

type Mode = "search" | "chat";

const TAB_KEY = "shiyuan.workspace.tab";

export default function WorkspaceView({ onGoLibrary }: { onGoLibrary: () => void }) {
  const [mode, setMode] = useState<Mode>(() =>
    window.localStorage.getItem(TAB_KEY) === "chat" ? "chat" : "search",
  );

  useEffect(() => {
    window.localStorage.setItem(TAB_KEY, mode);
  }, [mode]);

  return (
    <div className="view">
      <header className="view-head">
        <div>
          <h1>检索与对话</h1>
          <p className="sub">先找文件，再让对话只依据已解析的正文回答；两者共用同一份授权与索引。</p>
        </div>
        <div className="segmented" role="tablist" aria-label="检索与对话">
          <button
            role="tab"
            aria-selected={mode === "search"}
            className={mode === "search" ? "active" : ""}
            onClick={() => setMode("search")}
          >
            找文件
          </button>
          <button
            role="tab"
            aria-selected={mode === "chat"}
            className={mode === "chat" ? "active" : ""}
            onClick={() => setMode("chat")}
          >
            知识对话
          </button>
        </div>
      </header>

      <div hidden={mode !== "search"} data-testid="workspace-search">
        <SearchView embedded onGoSources={onGoLibrary} />
      </div>
      <div hidden={mode !== "chat"} data-testid="workspace-chat">
        <ChatView embedded active={mode === "chat"} />
      </div>
    </div>
  );
}
