import { useCallback, useEffect, useState } from "react";
import { api, formatTime } from "../api";
import type { Project, ProjectAsset, ProjectSource, Source } from "../api";
import { pickDirectory } from "../tauri";
import Icon from "../Icon";
import ExternalView from "./ExternalView";

const MEMBERSHIP_LABEL: Record<string, string> = {
  primary: "主资料",
  linked: "关联资料",
  excluded: "已排除",
};

const SCOPE_LABEL: Record<string, string> = {
  project_first: "先项目主资料",
  expanded: "主资料 + 关联",
  global: "全局",
};

export default function ProjectsView() {
  const [projects, setProjects] = useState<Project[]>([]);
  const [activeId, setActiveId] = useState<string | null>(null);
  const [detail, setDetail] = useState<Project | null>(null);
  const [assets, setAssets] = useState<ProjectAsset[]>([]);
  const [membership, setMembership] = useState<"primary" | "linked" | "excluded">("primary");
  const [knowledge, setKnowledge] = useState<{
    summaries: { title: string; summary: string; path?: string | null }[];
    entities: { name: string; type: string; description: string }[];
    questions: { question: string; answer: string }[];
  } | null>(null);
  const [loading, setLoading] = useState(true);
  const [error, setError] = useState<string | null>(null);
  const [note, setNote] = useState<string | null>(null);

  const [showCreate, setShowCreate] = useState(false);
  const [name, setName] = useState("");
  const [description, setDescription] = useState("");
  const [folder, setFolder] = useState("");
  const [creating, setCreating] = useState(false);

  const [pathInput, setPathInput] = useState("");
  const [globalSources, setGlobalSources] = useState<Source[]>([]);

  const loadGlobalSources = useCallback(async () => {
    try {
      const data = await api.sources();
      setGlobalSources(data.items);
    } catch (err) {
      setError(err instanceof Error ? err.message : String(err));
    }
  }, []);

  const loadProjects = useCallback(async () => {
    try {
      const data = await api.projects();
      setProjects(data.items);
      setActiveId((current) => current ?? data.items[0]?.project_id ?? null);
    } catch (err) {
      setError(err instanceof Error ? err.message : String(err));
    } finally {
      setLoading(false);
    }
  }, []);

  const loadDetail = useCallback(async (projectId: string) => {
    try {
      const [project, listing, knowledgeData] = await Promise.all([
        api.project(projectId),
        api.projectAssets(projectId, { membership, limit: 200 }),
        api.projectKnowledge(projectId),
      ]);
      setDetail(project);
      setAssets(listing.items);
      setKnowledge({
        summaries: knowledgeData.summaries,
        entities: knowledgeData.entities,
        questions: knowledgeData.questions,
      });
    } catch (err) {
      setError(err instanceof Error ? err.message : String(err));
    }
  }, [membership]);

  useEffect(() => {
    void loadProjects();
    void loadGlobalSources();
  }, [loadProjects, loadGlobalSources]);

  useEffect(() => {
    if (activeId) void loadDetail(activeId);
  }, [activeId, loadDetail]);

  const chooseFolder = async () => {
    const picked = await pickDirectory();
    if (picked) setFolder(picked);
  };

  const createProject = async () => {
    if (!name.trim()) return;
    setCreating(true);
    setError(null);
    try {
      const result = await api.createProject({
        name: name.trim(),
        description: description.trim(),
        folders: folder.trim() ? [folder.trim()] : [],
      });
      setName("");
      setDescription("");
      setFolder("");
      setShowCreate(false);
      setNote(result.sources.some((item) => item.error)
        ? `项目已创建，但文件夹关联失败：${result.sources.find((item) => item.error)?.error}`
        : "项目已创建，文件夹会在后台扫描并自动关联");
      await loadProjects();
      setActiveId(result.project.project_id);
    } catch (err) {
      setError(err instanceof Error ? err.message : String(err));
    } finally {
      setCreating(false);
    }
  };

  const addFolder = async () => {
    if (!activeId) return;
    const picked = pathInput.trim() || (await pickDirectory());
    if (!picked) return;
    try {
      await api.addProjectSource(activeId, { root: picked });
      setPathInput("");
      setNote("文件夹已关联，已有文件会复用现有索引");
      await loadProjects();
      await loadDetail(activeId);
    } catch (err) {
      setError(err instanceof Error ? err.message : String(err));
    }
  };

  const removeSource = async (source: ProjectSource) => {
    if (!activeId) return;
    try {
      await api.removeProjectSource(activeId, source.source_id);
      setNote("已移除该项目对该文件夹的引用；原始文件与全局索引未受影响");
      await loadProjects();
      await loadDetail(activeId);
    } catch (err) {
      setError(err instanceof Error ? err.message : String(err));
    }
  };

  const addLinked = async () => {
    if (!activeId || !pathInput.trim()) return;
    try {
      const result = await api.addProjectAssets(activeId, {
        paths: [pathInput.trim()],
        membership_type: "linked",
      });
      setPathInput("");
      setNote(result.missing?.length
        ? "该路径还没有进入全局资产目录，请先在数据源里扫描"
        : "已作为关联资料加入项目");
      await loadDetail(activeId);
    } catch (err) {
      setError(err instanceof Error ? err.message : String(err));
    }
  };

  const changeMembership = async (asset: ProjectAsset, next: string) => {
    if (!activeId) return;
    try {
      await api.addProjectAssets(activeId, {
        asset_ids: [asset.asset_id],
        membership_type: next,
      });
      setNote(next === "excluded"
        ? "已在本项目排除；其他项目与全局检索不受影响"
        : `已改为${MEMBERSHIP_LABEL[next] ?? next}`);
      await loadDetail(activeId);
      await loadProjects();
    } catch (err) {
      setError(err instanceof Error ? err.message : String(err));
    }
  };

  const pullSource = async (source: Source) => {
    if (!activeId) return;
    try {
      await api.addProjectSource(activeId, { source_id: source.source_id });
      setNote(`已把「${source.label}」拉取到项目，已有索引直接复用`);
      await loadProjects();
      await loadDetail(activeId);
    } catch (err) {
      setError(err instanceof Error ? err.message : String(err));
    }
  };

  const removeProject = async () => {
    if (!detail || detail.kind === "global") return;
    const confirmed = window.confirm(
      `删除项目「${detail.name}」？\n\n只会移除项目关系，不会删除文件、Chunk 或向量。`,
    );
    if (!confirmed) return;
    try {
      await api.deleteProject(detail.project_id);
      setDetail(null);
      setAssets([]);
      setKnowledge(null);
      setActiveId(null);
      await loadProjects();
      setNote("项目已删除，原始文件与全局索引未受影响");
    } catch (err) {
      setError(err instanceof Error ? err.message : String(err));
    }
  };

  const updatePolicy = async (payload: Record<string, string>) => {
    if (!activeId) return;
    try {
      await api.updateProject(activeId, payload);
      await loadDetail(activeId);
      await loadProjects();
      setNote("项目设置已更新");
    } catch (err) {
      setError(err instanceof Error ? err.message : String(err));
    }
  };

  const stats = detail?.stats;
  const isGlobal = detail?.kind === "global";
  const projectSourceIds = new Set((detail?.sources ?? []).map((source) => source.source_id));
  const pullableSources = globalSources.filter(
    (source) => !projectSourceIds.has(source.source_id) && source.state === "active",
  );

  return (
    <section className="view-section projects-view">
      <div className="inventory-head">
        <div>
          <div className="inventory-kicker">项目知识空间</div>
          <h2>项目</h2>
          <p className="sub">
            数据页录入的文件夹自动属于全局项目；子项目按需拉取资料。文件留在原位置，
            同一份内容只解析一次，可被多个项目共享。每个项目可单独创建只读外部接口。
          </p>
        </div>
        <button className="primary small icon-label" onClick={() => setShowCreate((value) => !value)}>
          <Icon name="plus" size={14} />
          {showCreate ? "收起" : "新建项目"}
        </button>
      </div>

      {error && <div className="alert error">{error}</div>}
      {note && <div className="alert ok">{note}</div>}

      {showCreate && (
        <section className="panel">
          <div className="panel-head"><h3>新建项目</h3></div>
          <div className="project-form">
            <label>
              项目名称
              <input value={name} onChange={(event) => setName(event.target.value)} placeholder="例如：黄斑蓝子鱼研究" />
            </label>
            <label>
              描述（可选）
              <input value={description} onChange={(event) => setDescription(event.target.value)} placeholder="这个项目在做什么" />
            </label>
            <label>
              文件夹（可选，可稍后添加）
              <span className="inline-field">
                <input value={folder} onChange={(event) => setFolder(event.target.value)} placeholder="D:\Research\Siganus" />
                <button className="ghost small" onClick={() => void chooseFolder()}>选择</button>
              </span>
            </label>
            <div className="project-form-actions">
              <button className="primary small" disabled={creating || !name.trim()} onClick={() => void createProject()}>
                {creating ? "创建中…" : "创建项目"}
              </button>
            </div>
          </div>
        </section>
      )}

      <div className="project-layout">
        <aside className="panel project-list">
          <div className="panel-head">
            <h3>全部项目</h3>
            <span className="muted small-text">{projects.length} 个</span>
          </div>
          {loading ? (
            <p className="muted small-text">正在读取…</p>
          ) : projects.length === 0 ? (
            <p className="muted small-text">还没有项目。不创建项目也可以继续使用全局文件库。</p>
          ) : (
            <ul>
              {projects.map((project) => (
                <li key={project.project_id}>
                  <button
                    className={`project-item${project.project_id === activeId ? " active" : ""}`}
                    onClick={() => setActiveId(project.project_id)}
                  >
                    <span className="project-item-name">
                      {project.name}
                      {project.kind === "global" && <span className="chip subtle">全局</span>}
                    </span>
                    <span className="project-item-meta">
                      {project.primary_count ?? 0} 主 · {project.linked_count ?? 0} 关联
                    </span>
                  </button>
                </li>
              ))}
            </ul>
          )}
        </aside>

        <div className="project-detail">
          {!detail ? (
            <section className="panel">
              <p className="muted">选择或创建一个项目。</p>
            </section>
          ) : (
            <>
              <section className="panel">
                <div className="panel-head">
                  <div>
                    <h3>
                      {detail.name}
                      {isGlobal && <span className="chip subtle">全局</span>}
                    </h3>
                    <p className="muted small-text">{detail.description || "暂无描述"}</p>
                  </div>
                  <div className="project-source-actions">
                    <span className="chip subtle">
                      更新于 {formatTime(detail.updated_at)}
                    </span>
                    {!isGlobal && (
                      <button className="ghost small danger" onClick={() => void removeProject()}>
                        删除项目
                      </button>
                    )}
                  </div>
                </div>
                {stats && (
                  <div className="project-stats">
                    <div><span>文件</span><strong>{stats.usable}</strong></div>
                    <div><span>主资料</span><strong>{stats.primary}</strong></div>
                    <div><span>关联</span><strong>{stats.linked}</strong></div>
                    <div><span>已排除</span><strong>{stats.excluded}</strong></div>
                    <div><span>已解析</span><strong>{stats.parsed}</strong></div>
                    <div><span>已向量化</span><strong>{stats.embedded}</strong></div>
                    <div><span>待处理</span><strong>{stats.pending}</strong></div>
                    <div><span>失败</span><strong>{stats.failed}</strong></div>
                  </div>
                )}
                <div className="project-policies">
                  {isGlobal ? (
                    <div className="project-policy-static">
                      <span>检索范围</span>
                      <strong>全部资料（自动包含数据页所有文件夹）</strong>
                    </div>
                  ) : (
                    <label>
                      默认检索策略
                      <select
                        value={detail.retrieval_policy}
                        onChange={(event) => void updatePolicy({ retrieval_policy: event.target.value })}
                      >
                        {Object.entries(SCOPE_LABEL).map(([value, label]) => (
                          <option key={value} value={value}>{label}</option>
                        ))}
                      </select>
                    </label>
                  )}
                  <label>
                    外部 Agent
                    <select
                      value={detail.privacy_policy}
                      onChange={(event) => void updatePolicy({ privacy_policy: event.target.value })}
                    >
                      <option value="external_allowed">允许通过外部接口读取</option>
                      <option value="local_only">仅本地使用</option>
                    </select>
                  </label>
                </div>
              </section>

              <section className="panel">
                <div className="panel-head">
                  <h3>{isGlobal ? "全局文件夹" : "项目文件夹"}</h3>
                  <span className="muted small-text">{detail.sources?.length ?? 0} 个</span>
                </div>
                {isGlobal ? (
                  <p className="muted small-text">
                    数据页录入的文件夹会自动属于全局项目。这里只读展示；增加或撤销授权请到「数据」页。
                  </p>
                ) : (
                  <div className="inline-field">
                    <input
                      value={pathInput}
                      onChange={(event) => setPathInput(event.target.value)}
                      placeholder="文件夹路径，或点「添加文件夹」选择"
                    />
                    <button className="ghost small" onClick={() => void addFolder()}>添加文件夹</button>
                  </div>
                )}
                <ul className="project-sources">
                  {(detail.sources ?? []).map((source) => (
                    <li key={source.source_id}>
                      <div>
                        <strong>{source.label}</strong>
                        <span className="muted small-text">{source.root_uri}</span>
                        {source.path_prefix && <span className="chip subtle">子目录 {source.path_prefix}</span>}
                      </div>
                      <div className="project-source-actions">
                        <span className="chip subtle">{source.available} 个文件</span>
                        {!isGlobal && (
                          <button className="ghost small" onClick={() => void removeSource(source)}>移除</button>
                        )}
                      </div>
                    </li>
                  ))}
                  {(detail.sources ?? []).length === 0 && (
                    <li className="muted small-text">
                      {isGlobal ? "数据页还没有授权文件夹。" : "还没有项目文件夹，可以从下方全局项目拉取。"}
                    </li>
                  )}
                </ul>
                {!isGlobal && pullableSources.length > 0 && (
                  <div className="project-pull">
                    <div className="muted small-text">从全局项目拉取（默认作为主资料）</div>
                    <ul className="project-sources">
                      {pullableSources.map((source) => (
                        <li key={source.source_id}>
                          <div>
                            <strong>{source.label}</strong>
                            <span className="muted small-text">{source.root_uri}</span>
                          </div>
                          <div className="project-source-actions">
                            <span className="chip subtle">{source.stats.available} 个文件</span>
                            <button className="ghost small" onClick={() => void pullSource(source)}>
                              拉取到项目
                            </button>
                          </div>
                        </li>
                      ))}
                    </ul>
                  </div>
                )}
              </section>

              <section className="panel">
                <div className="panel-head">
                  <h3>{isGlobal ? "全部资料" : "项目资料"}</h3>
                  {isGlobal ? (
                    <span className="muted small-text">只读：项目关系在子项目中调整</span>
                  ) : (
                    <div className="segmented small">
                      {(["primary", "linked", "excluded"] as const).map((key) => (
                        <button
                          key={key}
                          className={membership === key ? "active" : ""}
                          onClick={() => setMembership(key)}
                        >
                          {MEMBERSHIP_LABEL[key]}
                        </button>
                      ))}
                    </div>
                  )}
                </div>
                {!isGlobal && (
                  <div className="inline-field">
                    <input
                      value={pathInput}
                      onChange={(event) => setPathInput(event.target.value)}
                      placeholder="把全局文件路径加入为关联资料"
                    />
                    <button className="ghost small" onClick={() => void addLinked()}>加入关联</button>
                  </div>
                )}
                <ul className="project-assets">
                  {assets.map((asset) => (
                    <li key={asset.asset_id}>
                      <div className="project-asset-main">
                        <span className="project-asset-name">{asset.name}</span>
                        {!isGlobal && (
                          <span className="chip subtle">{MEMBERSHIP_LABEL[asset.membership_type]}</span>
                        )}
                        {asset.parse_state && <span className="chip subtle">{asset.parse_state}</span>}
                        <div className="project-asset-path">{asset.path}</div>
                      </div>
                      {!isGlobal && (
                        <div className="project-asset-actions">
                          {asset.membership_type !== "primary" && (
                            <button className="ghost small" onClick={() => void changeMembership(asset, "primary")}>设为主资料</button>
                          )}
                          {asset.membership_type !== "linked" && (
                            <button className="ghost small" onClick={() => void changeMembership(asset, "linked")}>设为关联</button>
                          )}
                          {asset.membership_type !== "excluded" && (
                            <button className="ghost small" onClick={() => void changeMembership(asset, "excluded")}>排除</button>
                          )}
                        </div>
                      )}
                    </li>
                  ))}
                  {assets.length === 0 && (
                    <li className="muted small-text">这一分类下还没有资料。</li>
                  )}
                </ul>
              </section>

              <section className="panel">
                <div className="panel-head">
                  <h3>项目知识</h3>
                  <span className="muted small-text">全部来自可追溯的原始文件</span>
                </div>
                {!knowledge || (
                  knowledge.summaries.length + knowledge.entities.length + knowledge.questions.length === 0
                ) ? (
                  <p className="muted small-text">
                    还没有生成摘要 / 实体 / FAQ。配置生成模型后，项目资料会按全局任务队列生成。
                  </p>
                ) : (
                  <div className="project-knowledge">
                    {knowledge.summaries.slice(0, 5).map((summary) => (
                      <div key={summary.title} className="knowledge-item">
                        <strong>{summary.title}</strong>
                        <p>{summary.summary}</p>
                        {summary.path && <span className="muted small-text">{summary.path}</span>}
                      </div>
                    ))}
                    {knowledge.questions.slice(0, 5).map((question) => (
                      <div key={question.question} className="knowledge-item">
                        <strong>{question.question}</strong>
                        <p>{question.answer}</p>
                      </div>
                    ))}
                  </div>
                )}
              </section>

              <ExternalView projectId={detail.project_id} projectName={detail.name} />
            </>
          )}
        </div>
      </div>
    </section>
  );
}
