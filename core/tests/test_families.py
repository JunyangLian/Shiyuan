from __future__ import annotations

from pathlib import Path

import pytest

from shiyuan import db, families, indexer, retrieval, scanner, search, store


@pytest.fixture()
def conn(tmp_path):
    connection = db.connect(tmp_path / "families.db")
    db.init_db(connection)
    yield connection
    connection.close()


def _seed_versions(conn, tmp_path):
    root = tmp_path / "资料"
    root.mkdir()
    shared = (
        "广东省城市科技创新评价研究。"
        + "本文以广东省二十一个地级市为样本，构建综合评价指标体系。" * 30
    )
    (root / "论文初稿.md").write_text(
        shared + "\n初稿内容，样本量为四十二。\n仅初稿保留的理论解释：样本选择存在局限。",
        encoding="utf-8")
    (root / "论文修改版.md").write_text(
        shared + "\n修改版增加了稳健性检验。", encoding="utf-8")
    (root / "论文终稿.md").write_text(
        shared + "\n终稿增加了稳健性检验与政策建议。", encoding="utf-8")
    (root / "论文初稿_备份.md").write_text(
        shared + "\n初稿内容，样本量为四十二。\n仅初稿保留的理论解释：样本选择存在局限。",
        encoding="utf-8")
    (root / "另一篇论文.md").write_text(
        "海洋微生物基因表达与氮循环，和城市创新评价无关。", encoding="utf-8")
    source = store.add_source(conn, root)
    scanner.scan_source(conn, source["source_id"])
    indexer.index_pending(conn, source["source_id"])
    return root, source["source_id"]


def test_builds_family_without_recommendation(conn, tmp_path):
    _seed_versions(conn, tmp_path)

    result = families.rebuild(conn)
    assert result["families"] == 1
    assert result["members"] >= 3

    family = conn.execute("SELECT * FROM document_families").fetchone()
    assert family["recommended_asset_id"] is None
    rows = conn.execute(
        "SELECT m.is_recommended, m.version_label, l.path"
        " FROM document_family_members m"
        " LEFT JOIN asset_locations l ON l.asset_id = m.asset_id"
    ).fetchall()
    names = {Path(row["path"]).name: row for row in rows if row["path"]}
    assert "论文终稿.md" in names
    assert "论文修改版.md" in names
    assert "论文初稿.md" in names or "论文初稿_备份.md" in names
    assert "另一篇论文.md" not in names
    assert all(row["is_recommended"] == 0 for row in rows)
    assert names["论文终稿.md"]["version_label"] == "终稿"

    groups = families.content_groups(conn, family["family_id"])
    shared = [group for group in groups if "综合评价指标体系" in group["text"]]
    assert shared
    assert any(
        len({source["asset_id"] for source in group["sources"]}) >= 2
        for group in shared
    )
    unique = [group for group in groups if "样本选择存在局限" in group["text"]]
    assert unique
    unique_names = {source["name"] for source in unique[0]["sources"]}
    assert unique_names & {"论文初稿.md", "论文初稿_备份.md"}
    assert "论文终稿.md" not in unique_names


def test_generic_tool_names_are_not_merged(conn, tmp_path):
    root = tmp_path / "图片"
    root.mkdir()
    for index, name in enumerate(
        ("Snipaste_2025-01-01_10-00-00.png", "Snipaste_2025-02-02_11-00-00.png")
    ):
        (root / name).write_bytes(b"\x89PNG\r\n\x1a\n" + b"0" * (16 + index))
    source = store.add_source(conn, root)
    scanner.scan_source(conn, source["source_id"])

    families.rebuild(conn)
    assert conn.execute("SELECT COUNT(*) FROM document_families").fetchone()[0] == 0


def test_non_document_extensions_are_ignored(conn, tmp_path):
    root = tmp_path / "代码"
    (root / "一").mkdir(parents=True)
    (root / "二").mkdir(parents=True)
    (root / "一" / "shared.css").write_text("body { color: red; }", encoding="utf-8")
    (root / "二" / "shared.css").write_text("body { color: red; }", encoding="utf-8")
    source = store.add_source(conn, root)
    scanner.scan_source(conn, source["source_id"])

    families.rebuild(conn)
    assert conn.execute("SELECT COUNT(*) FROM document_families").fetchone()[0] == 0


def test_search_aggregates_family_versions(conn, tmp_path):
    _seed_versions(conn, tmp_path)
    families.rebuild(conn)

    result = search.search(conn, "城市科技创新")
    family_items = [
        item for item in result["items"] if (item.get("family_size") or 1) > 1
    ]
    assert len(family_items) == 1
    item = family_items[0]
    assert item["family_size"] >= 3
    assert len(item["family_versions"]) >= 3
    assert not item.get("is_recommended")
    names = {version["name"] for version in item["family_versions"]}
    assert "论文终稿.md" in names
    assert "论文修改版.md" in names


def test_shared_content_merges_sources(conn, tmp_path):
    _seed_versions(conn, tmp_path)
    families.rebuild(conn)

    hits = retrieval.find_chunks(conn, "综合评价指标体系")
    shared = [
        hit for hit in hits
        if "综合评价指标体系" in hit.text and len(hit.sources) >= 2
    ]
    assert shared, "共享段落应合并为一条并保留多个来源"
    names = {source["name"] for source in shared[0].sources}
    assert "论文终稿.md" in names
    assert "论文修改版.md" in names


def test_unique_first_draft_content_is_retrievable(conn, tmp_path):
    _seed_versions(conn, tmp_path)
    families.rebuild(conn)

    hits = retrieval.find_chunks(conn, "样本选择存在局限")
    unique = [hit for hit in hits if "样本选择存在局限" in hit.text]
    assert unique, "仅初稿存在的段落必须仍然可检索"
    sources = {source["name"] for hit in unique for source in hit.sources}
    assert "论文初稿.md" in sources

    result = search.search(conn, "样本选择存在局限")
    assert result["items"]
    assert any(
        "论文初稿.md" in {
            version["name"] for version in item.get("family_versions", [])
        }
        for item in result["items"]
    )


def test_diff_reports_shared_and_modified(conn, tmp_path):
    _seed_versions(conn, tmp_path)
    families.rebuild(conn)

    family = conn.execute("SELECT family_id FROM document_families").fetchone()
    members = families.family_members(conn, family["family_id"])
    by_name = {member["name"]: member["asset_id"] for member in members}
    result = families.diff_versions(
        conn, family["family_id"],
        by_name["论文初稿.md"], by_name["论文终稿.md"],
    )

    assert result["summary"]["shared"] >= 1
    left_texts = " ".join(item["text"] for item in result["removed"])
    left_texts += " ".join(item["left"]["text"] for item in result["modified"])
    assert "样本选择存在局限" in left_texts
    right_texts = " ".join(item["text"] for item in result["added"])
    right_texts += " ".join(item["right"]["text"] for item in result["modified"])
    assert "政策建议" in right_texts


def test_diff_separates_added_and_removed(conn, tmp_path):
    root = tmp_path / "短稿"
    root.mkdir()
    (root / "城市创新研究初稿.md").write_text(
        "研究背景与问题提出。\n仅初稿保留的理论解释：样本选择存在局限。",
        encoding="utf-8")
    (root / "城市创新研究终稿.md").write_text(
        "研究背景与问题提出。\n终稿新增的政策建议：加强跨区域协同。",
        encoding="utf-8")
    source = store.add_source(conn, root)
    scanner.scan_source(conn, source["source_id"])
    indexer.index_pending(conn, source["source_id"])
    families.rebuild(conn)

    family = conn.execute("SELECT family_id FROM document_families").fetchone()
    assert family is not None
    members = families.family_members(conn, family["family_id"])
    by_name = {member["name"]: member["asset_id"] for member in members}
    result = families.diff_versions(
        conn, family["family_id"],
        by_name["城市创新研究初稿.md"], by_name["城市创新研究终稿.md"],
    )

    assert any("样本选择存在局限" in item["text"] for item in result["removed"])
    assert any("政策建议" in item["text"] for item in result["added"])


def test_diff_pairs_near_identical_paragraph_as_modified():
    base = "本文使用双向固定效应模型进行回归分析，并控制城市固定效应与年份固定效应。" * 20
    groups = {
        "a": {"content_group_id": "a", "order": 1,
              "text": base + "样本区间为二〇一五年至二〇二三年。", "assets": {}},
        "b": {"content_group_id": "b", "order": 2,
              "text": base + "样本区间为二〇一〇年至二〇二四年。", "assets": {}},
        "c": {"content_group_id": "c", "order": 3,
              "text": "完全不同的内容，讨论海洋微生物与氮循环之间的关系。" * 10,
              "assets": {}},
    }

    modified, removed, added = families._pair_modified(groups, ["a", "c"], ["b"])
    assert [left for _, left, _ in modified] == ["a"]
    assert removed == ["c"]
    assert added == []


def test_diff_rejects_asset_outside_family(conn, tmp_path):
    _seed_versions(conn, tmp_path)
    families.rebuild(conn)
    family = conn.execute("SELECT family_id FROM document_families").fetchone()

    with pytest.raises(ValueError):
        families.diff_versions(
            conn, family["family_id"], "asset_missing", "asset_missing_2",
        )


def test_family_id_is_stable_across_rebuilds(conn, tmp_path):
    _seed_versions(conn, tmp_path)
    families.rebuild(conn)
    first = [row[0] for row in conn.execute("SELECT family_id FROM document_families")]

    families.rebuild(conn)
    second = [row[0] for row in conn.execute("SELECT family_id FROM document_families")]

    assert first == second
