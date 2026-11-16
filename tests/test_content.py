"""資料の読み込み（content.py）のテスト。AIも通信も使わない。"""

import asyncio

import pytest
from fastapi.testclient import TestClient

import content as content_module
from app import app
from content import REFRESH_SEC, ContentError, ContentStore, parse_content


def chunk(id, label, title, **more):
    return {
        "id": id,
        "course": "2026-10",
        "set_id": "lecture-1",
        "label": label,
        "kind": "summary",
        "title": title,
        "pages": [1],
        "text": f"{title}の本文",
        **more,
    }


def sample():
    # 元の並びは「要点を全部 → 解説を全部」なので、同じ回が離れて出てくる
    return {
        "version": "v1",
        "chunks": [
            chunk("l1-summary", "10月期 第1回", "要点：A"),
            chunk("l2-summary", "10月期 第2回", "要点：B"),
            chunk("l1-qa-1", "10月期 第1回", "解説：C"),
            chunk("l2-qa-1", "10月期 第2回", "解説：D"),
        ],
        "glossary": [{"term": "定期実行", "reading": "ていきじっこう", "description": "決めた時刻に始める仕組み"}],
    }


# ---- 形の確認 ----


def test_parse_reads_chunks_and_glossary():
    c = parse_content(sample())
    assert c.version == "v1"
    assert len(c.chunks) == 4
    assert c.chunks[0].pages == (1,)
    assert c.glossary[0].term == "定期実行"


def test_parse_rejects_wrong_shape():
    with pytest.raises(ContentError):
        parse_content({"version": "v1"})  # かたまりがない
    with pytest.raises(ContentError):
        parse_content({"version": "v1", "chunks": [{"id": "x"}]})  # 必要な項目がない
    with pytest.raises(ContentError):
        parse_content("文字だけ")


def test_parse_rejects_empty_chunks():
    with pytest.raises(ContentError):
        parse_content({"version": "v1", "chunks": []})


def test_parse_rejects_duplicate_ids():
    data = sample()
    data["chunks"][1]["id"] = "l1-summary"
    with pytest.raises(ContentError):
        parse_content(data)


# ---- 目次 ----


def test_index_groups_by_lecture_and_numbers_from_one():
    c = parse_content(sample())
    assert c.index_text() == "\n".join(
        [
            "## 10月期 第1回",
            "1 要点：A",
            "2 解説：C",
            "## 10月期 第2回",
            "3 要点：B",
            "4 解説：D",
        ]
    )


def test_number_goes_back_to_the_same_chunk_as_the_index():
    c = parse_content(sample())
    assert c.chunk_by_number(2).id == "l1-qa-1"
    assert c.chunk_by_number(3).id == "l2-summary"


def test_unknown_number_gives_none():
    c = parse_content(sample())
    assert c.chunk_by_number(0) is None
    assert c.chunk_by_number(5) is None
    assert c.chunk_by_number(-1) is None


# ---- 持っておく・取り直す ----


def fake_fetch(monkeypatch, results):
    """取りに行く部分を偽物に差し替える。results を順に返し、例外なら投げる。"""
    calls = []

    async def fetch(url):
        calls.append(url)
        r = results.pop(0)
        if isinstance(r, Exception):
            raise r
        return r

    monkeypatch.setattr(content_module, "fetch_content", fetch)
    return calls


def test_store_fetches_once_then_reuses(monkeypatch):
    calls = fake_fetch(monkeypatch, [parse_content(sample())])
    s = ContentStore()
    a = asyncio.run(s.get("http://x/c.json", now=1000))
    b = asyncio.run(s.get("http://x/c.json", now=1000 + REFRESH_SEC - 1))
    assert a is b
    assert len(calls) == 1


def test_store_refetches_after_interval(monkeypatch):
    new = sample() | {"version": "v2"}
    calls = fake_fetch(monkeypatch, [parse_content(sample()), parse_content(new)])
    s = ContentStore()
    asyncio.run(s.get("http://x/c.json", now=1000))
    c = asyncio.run(s.get("http://x/c.json", now=1000 + REFRESH_SEC))
    assert c.version == "v2"
    assert len(calls) == 2


def test_store_keeps_old_content_when_refetch_fails(monkeypatch):
    fake_fetch(monkeypatch, [parse_content(sample()), ContentError("取れない")])
    s = ContentStore()
    asyncio.run(s.get("http://x/c.json", now=1000))
    c = asyncio.run(s.get("http://x/c.json", now=1000 + REFRESH_SEC))
    assert c.version == "v1"


def test_store_raises_when_nothing_loaded(monkeypatch):
    fake_fetch(monkeypatch, [ContentError("取れない")])
    with pytest.raises(ContentError):
        asyncio.run(ContentStore().get("http://x/c.json", now=1000))


def test_store_raises_without_url():
    with pytest.raises(ContentError):
        asyncio.run(ContentStore().get(None, now=1000))


# ---- /api/health ----


def test_health_reports_version_when_loaded(monkeypatch):
    monkeypatch.setattr("app.content_store", ContentStore())
    fake_fetch(monkeypatch, [parse_content(sample())])
    monkeypatch.setenv("CONTENT_URL", "http://x/c.json")
    r = TestClient(app).get("/api/health")
    assert r.status_code == 200
    assert r.json() == {"ok": True, "content_loaded": True, "content_version": "v1", "chunks": 4, "glossary": 1}


def test_health_still_answers_when_content_missing(monkeypatch):
    monkeypatch.setattr("app.content_store", ContentStore())
    monkeypatch.delenv("CONTENT_URL", raising=False)
    r = TestClient(app).get("/api/health")
    assert r.status_code == 200
    assert r.json() == {"ok": True, "content_loaded": False, "content_version": None}
