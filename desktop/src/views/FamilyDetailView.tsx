import { useCallback, useEffect, useState } from "react";
import { api } from "../api";
import type {
  DocumentFamilyDetail,
  DocumentFamilySummary,
  FamilyDiff,
  FamilyDiffItem,
} from "../api";
import { openFile } from "../tauri";
import Icon from "../Icon";

const PAGE = 8;

function SourceLine({ item }: { item: FamilyDiffItem }) {
  const names = [...new Set(
    item.sources.map((source) => source.name || source.path || "未知文件"),
  )];
  return (
    <div className="family-sources">
      来源：{names.length > 0 ? names.join("、") : "未记录来源"}
    </div>
  );
}

function DiffText({ label, item }: { label?: string; item: FamilyDiffItem }) {
  return (
    <div className="family-diff-block">
      {label && <span className="family-diff-label">{label}</span>}
      <p className="family-diff-text">{item.text}</p>
      <SourceLine item={item} />
    </div>
  );
}

export default function FamilyDetailView({
  family,
  onBack,
}: {
  family: DocumentFamilySummary;
  onBack: () => void;
}) {
  const [detail, setDetail] = useState<DocumentFamilyDetail | null>(null);
  const [loading, setLoading] = useState(true);
  const [error, setError] = useState<string | null>(null);
  const [leftId, setLeftId] = useState("");
  const [rightId, setRightId] = useState("");
  const [diff, setDiff] = useState<FamilyDiff | null>(null);
  const [diffLoading, setDiffLoading] = useState(false);
  const [diffError, setDiffError] = useState<string | null>(null);
  const [expanded, setExpanded] = useState<Record<string, boolean>>({});
  const [showAllGroups, setShowAllGroups] = useState(false);

  const load = useCallback(async () => {
    setLoading(true);
    setError(null);
    try {
      const result = await api.documentFamily(family.family_id);
      setDetail(result);
      setLeftId((current) => current || result.members[0]?.asset_id || "");
      setRightId((current) => current || result.members[1]?.asset_id || "");
    } catch (err) {
      setError(err instanceof Error ? err.message : String(err));
    } finally {
      setLoading(false);
    }
  }, [family.family_id]);

  useEffect(() => {
    void load();
  }, [load]);

  useEffect(() => {
    if (!leftId || !rightId || leftId === rightId) {
      setDiff(null);
      return;
    }
    let active = true;
    setDiffLoading(true);
    setDiffError(null);
    api.familyDiff(family.family_id, leftId, rightId)
      .then((result) => {
        if (active) setDiff(result);
      })
      .catch((err) => {
        if (active) {
          setDiff(null);
          setDiffError(err instanceof Error ? err.message : String(err));
        }
      })
      .finally(() => {
        if (active) setDiffLoading(false);
      });
    return () => {
      active = false;
    };
  }, [family.family_id, leftId, rightId]);

  const toggle = (key: string) => {
    setExpanded((current) => ({ ...current, [key]: !current[key] }));
  };

  const members = detail?.members ?? [];
  const groups = detail?.content_groups ?? [];
  const visibleGroups = showAllGroups ? groups : groups.slice(0, PAGE);

  return (
    <div className="view-section family-view">
      <div className="inventory-head">
        <div>
          <button className="ghost small icon-label inventory-back" onClick={onBack}>
            <Icon name="arrow-left" size={15} />
            返回数据
          </button>
          <div className="inventory-kicker">文档家族</div>
          <h2>{family.title}</h2>
          <p className="sub">
            {family.size} 个关联版本。原文件保持不变，拾源只整理关系，不判断哪个版本更好。
          </p>
        </div>
        <button className="ghost small icon-label" disabled={loading} onClick={() => void load()}>
          <Icon name="refresh" size={14} />
          {loading ? "刷新中…" : "刷新"}
        </button>
      </div>

      {error && <div className="alert error">{error}</div>}

      {loading && !detail ? (
        <p className="muted">正在读取家族详情…</p>
      ) : detail && (
        <>
          <section className="panel family-panel">
            <div className="inventory-panel-head">
              <h3>版本成员</h3>
              <span className="muted small-text">{members.length} 个文件</span>
            </div>
            <ul className="family-member-list">
              {members.map((member) => (
                <li key={member.asset_id} className="family-member">
                  <div className="family-member-main">
                    <span className="family-member-name" title={member.path ?? ""}>
                      {member.name || member.path || member.asset_id}
                    </span>
                    <span className="chip subtle">
                      {member.version_label || "未标注版本"}
                    </span>
                  </div>
                  <div className="family-member-path" title={member.path ?? ""}>
                    {member.rel_path || member.path || "路径不可用"}
                  </div>
                  {member.path && (
                    <button
                      className="ghost small icon-label"
                      onClick={() => void openFile(member.path as string)}
                    >
                      <Icon name="arrow-up-right" size={14} />
                      打开
                    </button>
                  )}
                </li>
              ))}
            </ul>
          </section>

          <section className="panel family-panel">
            <div className="inventory-panel-head">
              <h3>版本差异</h3>
              <span className="muted small-text">逐段对比，保留差异来源</span>
            </div>

            {members.length < 2 ? (
              <p className="muted small-text">至少需要两个版本才能对比。</p>
            ) : (
              <>
                <div className="family-diff-pickers">
                  <label>
                    基准版本
                    <select value={leftId} onChange={(event) => setLeftId(event.target.value)}>
                      {members.map((member) => (
                        <option
                          key={member.asset_id}
                          value={member.asset_id}
                          disabled={member.asset_id === rightId}
                        >
                          {member.name || member.path || member.asset_id}
                        </option>
                      ))}
                    </select>
                  </label>
                  <span className="family-diff-arrow">对比</span>
                  <label>
                    对比版本
                    <select value={rightId} onChange={(event) => setRightId(event.target.value)}>
                      {members.map((member) => (
                        <option
                          key={member.asset_id}
                          value={member.asset_id}
                          disabled={member.asset_id === leftId}
                        >
                          {member.name || member.path || member.asset_id}
                        </option>
                      ))}
                    </select>
                  </label>
                </div>

                {diffError && <div className="alert error">{diffError}</div>}
                {diffLoading && <p className="muted small-text">正在对比…</p>}

                {diff && !diffLoading && (
                  <div className="family-diff-result">
                    <div className="family-diff-summary">
                      <span className="chip subtle">共享 {diff.summary.shared}</span>
                      <span className="chip warn">仅基准 {diff.summary.removed}</span>
                      <span className="chip ok">仅对比 {diff.summary.added}</span>
                      <span className="chip accent">近似修改 {diff.summary.modified}</span>
                    </div>
                    <p className="muted small-text">
                      共享段落只展示一份；「仅基准」表示该内容在对比版本中不存在，
                      「仅对比」表示只在对比版本中出现。
                    </p>

                    <section className="family-diff-section">
                      <div className="family-diff-head">
                        <h4>新增段落</h4>
                        <span className="muted small-text">仅对比版本存在</span>
                      </div>
                      {diff.added.length === 0 ? (
                        <p className="muted small-text">没有新增段落。</p>
                      ) : (
                        <>
                          {(expanded.added ? diff.added : diff.added.slice(0, PAGE)).map((item) => (
                            <DiffText key={item.content_group_id} item={item} />
                          ))}
                          {diff.added.length > PAGE && (
                            <button className="ghost small" onClick={() => toggle("added")}>
                              {expanded.added ? "收起" : `显示更多（共 ${diff.summary.added} 段）`}
                            </button>
                          )}
                        </>
                      )}
                    </section>

                    <section className="family-diff-section">
                      <div className="family-diff-head">
                        <h4>删除段落</h4>
                        <span className="muted small-text">仅基准版本存在</span>
                      </div>
                      {diff.removed.length === 0 ? (
                        <p className="muted small-text">没有删除段落。</p>
                      ) : (
                        <>
                          {(expanded.removed ? diff.removed : diff.removed.slice(0, PAGE)).map((item) => (
                            <DiffText key={item.content_group_id} item={item} />
                          ))}
                          {diff.removed.length > PAGE && (
                            <button className="ghost small" onClick={() => toggle("removed")}>
                              {expanded.removed ? "收起" : `显示更多（共 ${diff.summary.removed} 段）`}
                            </button>
                          )}
                        </>
                      )}
                    </section>

                    <section className="family-diff-section">
                      <div className="family-diff-head">
                        <h4>近似修改</h4>
                        <span className="muted small-text">两侧段落高度相似但有差异</span>
                      </div>
                      {diff.modified.length === 0 ? (
                        <p className="muted small-text">没有可配对的修改段落。</p>
                      ) : (
                        <>
                          {(expanded.modified ? diff.modified : diff.modified.slice(0, PAGE)).map((change) => (
                            <div
                              key={`${change.left.content_group_id}:${change.right.content_group_id}`}
                              className="family-modified"
                            >
                              <div className="family-modified-head">
                                相似度 {Math.round(change.similarity * 100)}%
                              </div>
                              <DiffText label="基准版本" item={change.left} />
                              <DiffText label="对比版本" item={change.right} />
                            </div>
                          ))}
                          {diff.modified.length > PAGE && (
                            <button className="ghost small" onClick={() => toggle("modified")}>
                              {expanded.modified ? "收起" : `显示更多（共 ${diff.summary.modified} 段）`}
                            </button>
                          )}
                        </>
                      )}
                    </section>

                    <section className="family-diff-section">
                      <div className="family-diff-head">
                        <h4>共享段落</h4>
                        <span className="muted small-text">
                          {diff.truncated.shared ? "接口只返回部分共享段落" : "重复内容只展示一份"}
                        </span>
                      </div>
                      {diff.shared.length === 0 ? (
                        <p className="muted small-text">两个版本没有完全相同的段落。</p>
                      ) : (
                        <>
                          {(expanded.shared ? diff.shared : diff.shared.slice(0, PAGE)).map((item) => (
                            <DiffText key={item.content_group_id} item={item} />
                          ))}
                          {diff.shared.length > PAGE && (
                            <button className="ghost small" onClick={() => toggle("shared")}>
                              {expanded.shared ? "收起" : `显示更多（共 ${diff.summary.shared} 段）`}
                            </button>
                          )}
                        </>
                      )}
                    </section>
                  </div>
                )}
              </>
            )}
          </section>

          <section className="panel family-panel">
            <div className="inventory-panel-head">
              <h3>内容段落与来源</h3>
              <span className="muted small-text">{groups.length} 个内容组</span>
            </div>
            {groups.length === 0 ? (
              <p className="muted small-text">这个家族还没有可用的段落分组。</p>
            ) : (
              <>
                <ul className="family-group-list">
                  {visibleGroups.map((group) => {
                    const names = [...new Set(
                      group.sources.map((source) => source.name || source.path || "未知文件"),
                    )];
                    return (
                      <li key={group.content_group_id} className="family-group">
                        <p className="family-group-text">{group.text}</p>
                        <div className="family-sources">
                          {names.length} 个来源：{names.join("、")}
                        </div>
                      </li>
                    );
                  })}
                </ul>
                {groups.length > PAGE && (
                  <button
                    className="ghost small"
                    onClick={() => setShowAllGroups((current) => !current)}
                  >
                    {showAllGroups ? "收起" : `显示更多（共 ${groups.length} 组）`}
                  </button>
                )}
              </>
            )}
          </section>
        </>
      )}
    </div>
  );
}
