import { useCallback, useEffect, useState } from "react";
import { api } from "../api";
import type { DocumentFamilySummary } from "../api";
import SourcesView from "./SourcesView";
import InventoryView from "./InventoryView";
import ParseResultsView from "./ParseResultsView";
import FamilyDetailView from "./FamilyDetailView";
import type { InventorySource } from "./InventoryView";

export default function LibraryView() {
  const [inventorySource, setInventorySource] = useState<InventorySource | null>(null);
  const [resultsSource, setResultsSource] = useState<InventorySource | null>(null);
  const [family, setFamily] = useState<DocumentFamilySummary | null>(null);
  const [families, setFamilies] = useState<DocumentFamilySummary[]>([]);
  const [familiesLoaded, setFamiliesLoaded] = useState(false);
  const [familiesLoading, setFamiliesLoading] = useState(false);
  const [familiesError, setFamiliesError] = useState<string | null>(null);

  const loadFamilies = useCallback(async () => {
    setFamiliesLoading(true);
    setFamiliesError(null);
    try {
      const result = await api.documentFamilies();
      setFamilies(result.items);
    } catch (err) {
      setFamiliesError(err instanceof Error ? err.message : String(err));
    } finally {
      setFamiliesLoading(false);
      setFamiliesLoaded(true);
    }
  }, []);

  useEffect(() => {
    void loadFamilies();
  }, [loadFamilies]);

  if (inventorySource) {
    return (
      <div className="view">
        <InventoryView
          source={inventorySource}
          onBack={() => setInventorySource(null)}
        />
      </div>
    );
  }

  if (resultsSource) {
    return (
      <div className="view">
        <ParseResultsView
          source={resultsSource}
          onBack={() => setResultsSource(null)}
        />
      </div>
    );
  }

  if (family) {
    return (
      <div className="view">
        <FamilyDetailView
          family={family}
          onBack={() => setFamily(null)}
        />
      </div>
    );
  }

  return (
    <div className="view">
      <header className="view-head view-head-stack">
        <div>
          <p className="eyebrow">授权与盘点</p>
          <h1>数据</h1>
          <p className="sub">授权目录与盘点策略集中在这里；同步覆盖与索引状态在「更新」里查看。</p>
        </div>
      </header>

      <SourcesView
        embedded
        onOpenInventory={setInventorySource}
        onOpenResults={setResultsSource}
      />

      <section className="panel family-panel family-list-panel">
        <div className="inventory-panel-head">
          <h3>文档家族</h3>
          <div className="family-list-actions">
            <span className="muted small-text">{families.length} 个家族</span>
            <button
              className="ghost small"
              disabled={familiesLoading}
              onClick={() => void loadFamilies()}
            >
              {familiesLoading ? "刷新中…" : "刷新"}
            </button>
          </div>
        </div>
        <p className="muted small-text family-intro">
          同一份文档的多个版本会归在这里。拾源不推荐版本、不删除文件，
          检索时自动合并重复段落并保留全部来源。
        </p>

        {familiesError && <div className="alert error">{familiesError}</div>}

        {!familiesLoaded && familiesLoading ? (
          <p className="muted small-text">正在读取文档家族…</p>
        ) : families.length === 0 ? (
          <p className="muted small-text">
            还没有识别到多版本文档。扫描并解析后，这里会自动出现。
          </p>
        ) : (
          <ul className="family-list">
            {families.map((item) => {
              const labels = [...new Set(
                item.members.map((member) => member.version_label).filter(Boolean),
              )];
              return (
                <li key={item.family_id} className="family-list-item">
                  <div className="family-list-main">
                    <div className="family-list-title">{item.title}</div>
                    <div className="family-list-meta">
                      {item.size} 个版本
                      {labels.length > 0 ? ` · ${labels.slice(0, 4).join(" / ")}` : ""}
                    </div>
                  </div>
                  <button className="ghost small" onClick={() => setFamily(item)}>
                    查看详情
                  </button>
                </li>
              );
            })}
          </ul>
        )}
      </section>
    </div>
  );
}
