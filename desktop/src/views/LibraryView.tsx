import { useEffect, useState } from "react";
import SourcesView from "./SourcesView";
import HealthView from "./HealthView";

type Tab = "sources" | "health";

const TAB_KEY = "shiyuan.library.tab";

export default function LibraryView() {
  const [tab, setTab] = useState<Tab>(() =>
    window.localStorage.getItem(TAB_KEY) === "health" ? "health" : "sources",
  );

  useEffect(() => {
    window.localStorage.setItem(TAB_KEY, tab);
  }, [tab]);

  return (
    <div className="view">
      <header className="view-head">
        <div>
          <h1>数据源与维护</h1>
          <p className="sub">授权目录、扫描状态、内容覆盖与索引维护集中在这里。</p>
        </div>
        <div className="segmented" role="tablist" aria-label="数据源与维护">
          <button
            role="tab"
            aria-selected={tab === "sources"}
            className={tab === "sources" ? "active" : ""}
            onClick={() => setTab("sources")}
          >
            数据源
          </button>
          <button
            role="tab"
            aria-selected={tab === "health"}
            className={tab === "health" ? "active" : ""}
            onClick={() => setTab("health")}
          >
            维护
          </button>
        </div>
      </header>

      <div hidden={tab !== "sources"} data-testid="library-sources">
        <SourcesView embedded />
      </div>
      <div hidden={tab !== "health"} data-testid="library-health">
        <HealthView embedded />
      </div>
    </div>
  );
}
