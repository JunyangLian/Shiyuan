import { act, fireEvent, render, screen, waitFor, within } from "@testing-library/react";
import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";

import styles from "./styles.css?raw";

const mocks = vi.hoisted(() => ({
  health: vi.fn(),
  stats: vi.fn(),
  search: vi.fn(),
  sources: vi.fn(),
  addSource: vi.fn(),
  scan: vi.fn(),
  ocr: vi.fn(),
  setState: vi.fn(),
  removeSource: vi.fn(),
  chatSessions: vi.fn(),
  chatSession: vi.fn(),
  createChatSession: vi.fn(),
  ask: vi.fn(),
  renameChatSession: vi.fn(),
  deleteChatSession: vi.fn(),
  answerSettings: vi.fn(),
  saveAnswerSettings: vi.fn(),
  embeddingSettings: vi.fn(),
  saveEmbeddingSettings: vi.fn(),
  querySettings: vi.fn(),
  saveQuerySettings: vi.fn(),
  ocrSettings: vi.fn(),
  saveOcrSettings: vi.fn(),
  events: vi.fn(),
  jobs: vi.fn(),
  maintenance: vi.fn(),
  retryJob: vi.fn(),
}));

vi.mock("./api", () => ({
  api: mocks,
  formatTime: (value: string | null) => value ?? "—",
  formatSize: (value: number) => `${value} B`,
}));

vi.mock("./tauri", () => ({
  openFile: vi.fn(),
  pickDirectory: vi.fn(),
}));

import App from "./App";

const STATS = {
  files: { available: 4, missing: 0, offline: 0, stale: 1 },
  by_ext: [{ ext: "md", count: 4 }],
  sources: [
    {
      source_id: "src_1",
      label: "资料",
      root_uri: "D:\\资料",
      state: "active",
      health: "ok",
      last_scan_at: "2026-11-22T10:00:00+08:00",
      available: 4,
      stale: 1,
    },
  ],
  coverage: {
    directory_level: "L0",
    directory_indexed: 4,
    content_level: "L1",
    content_indexed: 2,
    content_pending: 0,
    content_empty: 0,
    content_failed: 0,
    content_unsupported: 0,
    low_quality: 0,
    ocr_indexed: 0,
    ocr_empty: 0,
    ocr_failed: 0,
    ocr_pending: 0,
    mixed_pdf: 0,
    ocr_policy: "auto",
    ocr_available: true,
    ocr_engine: "auto",
    ocr_languages: ["zh-Hans-CN"],
    ocr_message: null,
  },
  maintenance: {
    jobs: {},
    chunks: 10,
    artifacts: 4,
    db_bytes: 2048,
    running: {
      running: false,
      kind: null,
      source_id: null,
      started_at: null,
      message: null,
    },
    budget: { date: "2026-11-22", l1_used: 1, l1_limit: 500, l2_used: 0, l2_limit: 200 },
    vectors: 0,
    embedding: {
      provider: "none",
      base_url: "",
      model: "",
      allow_cloud: false,
      top_k: 6,
      has_key: false,
    },
  },
};

const EMPTY_SEARCH = {
  query: "",
  total: 0,
  items: [],
  index_level: "L0",
  content_hits: 0,
  truncated: false,
  rewritten: null,
  vector_used: false,
  vector_model: null,
  vector_error: null,
};

beforeEach(() => {
  window.localStorage.clear();
  mocks.health.mockResolvedValue({ status: "ok", version: "0.1.0", db: "test.db" });
  mocks.stats.mockResolvedValue(STATS);
  mocks.search.mockResolvedValue(EMPTY_SEARCH);
  mocks.sources.mockResolvedValue({ items: [] });
  mocks.chatSessions.mockResolvedValue({ items: [] });
  mocks.answerSettings.mockResolvedValue({
    provider: "extractive",
    base_url: "",
    model: "",
    allow_cloud: false,
    top_k: 6,
    has_key: false,
  });
  mocks.embeddingSettings.mockResolvedValue({
    provider: "none",
    base_url: "",
    model: "",
    allow_cloud: false,
    top_k: 6,
    has_key: false,
  });
  mocks.querySettings.mockResolvedValue({ custom: [], builtin_groups: 3 });
  mocks.ocrSettings.mockResolvedValue({ policy: "auto", engines: [] });
  mocks.events.mockResolvedValue({ items: [] });
  mocks.jobs.mockResolvedValue({ items: [] });
  vi.stubGlobal("requestAnimationFrame", (callback: FrameRequestCallback) => {
    callback(0);
    return 1;
  });
  vi.stubGlobal("cancelAnimationFrame", () => undefined);
});

afterEach(() => {
  vi.unstubAllGlobals();
});

async function settle() {
  await act(async () => {
    await Promise.resolve();
    await Promise.resolve();
    await Promise.resolve();
  });
}

async function renderApp() {
  const utils = render(<App />);
  await settle();
  return utils;
}

describe("页面整合", () => {
  it("主导航只保留两个入口", async () => {
    const { container } = await renderApp();
    const labels = [...container.querySelectorAll(".nav .nav-label")].map(
      (element) => element.textContent,
    );
    expect(labels).toEqual(["检索与对话", "数据源与维护"]);
  });

  it("搜索与对话在同一页切换，且两个视图保持挂载", async () => {
    await renderApp();
    const searchPanel = screen.getByTestId("workspace-search");
    const chatPanel = screen.getByTestId("workspace-chat");
    const searchInput = await within(searchPanel).findByPlaceholderText(/BLAST/);
    expect(searchPanel.hidden).toBe(false);
    expect(chatPanel.hidden).toBe(true);

    fireEvent.click(screen.getByRole("tab", { name: "知识对话" }));
    expect(screen.getByRole("tab", { name: "知识对话" }).getAttribute("aria-selected"))
      .toBe("true");
    expect(searchPanel.hidden).toBe(true);
    expect(chatPanel.hidden).toBe(false);
    expect(window.localStorage.getItem("shiyuan.workspace.tab")).toBe("chat");
    expect(await screen.findByText("还没有对话")).toBeTruthy();

    fireEvent.click(screen.getByRole("tab", { name: "找文件" }));
    expect(screen.getByTestId("workspace-search").hidden).toBe(false);
    // 引用相同说明视图没有被卸载重建，查询状态得以保留。
    expect(within(screen.getByTestId("workspace-search")).getByPlaceholderText(/BLAST/))
      .toBe(searchInput);
  });

  it("数据源与维护在同一页切换", async () => {
    await renderApp();
    fireEvent.click(screen.getByRole("button", { name: /数据源与维护/ }));
    await settle();

    expect(await screen.findByRole("tab", { name: "数据源" })).toBeTruthy();
    expect(screen.getByTestId("library-sources").hidden).toBe(false);
    expect(screen.getByTestId("library-health").hidden).toBe(true);
    expect(await screen.findByText("添加文件夹")).toBeTruthy();

    fireEvent.click(screen.getByRole("tab", { name: "维护" }));
    expect(screen.getByTestId("library-sources").hidden).toBe(true);
    expect(screen.getByTestId("library-health").hidden).toBe(false);
    expect(await screen.findByText("内容覆盖（L1）")).toBeTruthy();
    expect(window.localStorage.getItem("shiyuan.library.tab")).toBe("health");

    fireEvent.click(screen.getByRole("tab", { name: "数据源" }));
    expect(screen.getByTestId("library-sources").hidden).toBe(false);
  });
});

describe("固定对话输入框", () => {
  it("输入框与发送按钮位于消息列表之后的 composer 中", async () => {
    const { container } = await renderApp();
    fireEvent.click(screen.getByRole("tab", { name: "知识对话" }));

    const composer = await waitFor(() => {
      const element = container.querySelector(".chat-composer");
      if (!element) throw new Error("composer 未渲染");
      return element as HTMLElement;
    });
    const messages = container.querySelector(".chat-messages");
    expect(messages).toBeTruthy();
    expect(
      messages!.compareDocumentPosition(composer) & Node.DOCUMENT_POSITION_FOLLOWING,
    ).toBeTruthy();
    expect(within(composer).getByPlaceholderText(/项目A 的最终样本量/)).toBeTruthy();
    expect(within(composer).getByRole("button", { name: "提问" })).toBeTruthy();
  });

  it("样式表把 composer 固定在视口底部", () => {
    const block = styles.match(/\.chat-composer\s*\{([^}]*)\}/)?.[1] ?? "";
    expect(block).toContain("position: sticky;");
    expect(block).toContain("bottom: 0;");
  });

  it("切回对话标签时滚动到最新消息", async () => {
    const { container } = await renderApp();
    const main = container.querySelector(".main") as HTMLElement;
    const setScrollTop = vi.fn();
    Object.defineProperty(main, "scrollTop", {
      configurable: true,
      get: () => 0,
      set: setScrollTop,
    });
    Object.defineProperty(main, "scrollHeight", { configurable: true, value: 2400 });

    fireEvent.click(screen.getByRole("tab", { name: "知识对话" }));
    await waitFor(() => expect(setScrollTop).toHaveBeenCalledWith(2400));

    setScrollTop.mockClear();
    fireEvent.click(screen.getByRole("tab", { name: "找文件" }));
    fireEvent.click(screen.getByRole("tab", { name: "知识对话" }));
    await waitFor(() => expect(setScrollTop).toHaveBeenCalledWith(2400));
  });
});
