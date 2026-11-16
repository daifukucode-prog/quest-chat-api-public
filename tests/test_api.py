"""APIの決まり（app.py）のテスト。AIは偽物に差し替える。"""

import json
import sqlite3

import pytest
from fastapi.testclient import TestClient

import guard
from app import app
from fakes import FakeLlm, broken, loaded_store

SITE = "https://quest-app.example.com"
BROWSER = "b1946ac9-2f0c-4c7e-9d1a-000000000001"


@pytest.fixture
def db_path(tmp_path, monkeypatch):
    path = tmp_path / "api.sqlite3"
    monkeypatch.setenv("SQLITE_PATH", str(path))
    monkeypatch.setenv("ALLOWED_ORIGINS", f"{SITE},http://localhost:4173")
    monkeypatch.delenv("CHAT_ENABLED", raising=False)
    monkeypatch.setenv("ANTHROPIC_API_KEY", "test-key")
    monkeypatch.setattr("app.content_store", loaded_store())
    return path


@pytest.fixture
def llm(monkeypatch):
    fake = FakeLlm()
    monkeypatch.setattr("app.make_llm", lambda api_key: fake)
    return fake


@pytest.fixture
def client(db_path, llm):
    return TestClient(app)


def events_of(response):
    """届いた知らせを（種類, 中身）の組に直す。"""
    out = []
    for block in response.text.strip().split("\n\n"):
        lines = dict(line.split(": ", 1) for line in block.split("\n"))
        out.append((lines["event"], json.loads(lines["data"])))
    return out


def ask(client, question="ターミナルって何ですか", origin=SITE, browser=BROWSER, **more):
    headers = {"Origin": origin} if origin else {}
    return client.post("/api/chat", json={"question": question, "browser_id": browser, **more}, headers=headers)


def logs(db_path):
    if not db_path.exists():
        return []
    conn = sqlite3.connect(db_path)
    conn.row_factory = sqlite3.Row
    try:
        return [dict(r) for r in conn.execute("SELECT * FROM chat_logs ORDER BY created_at")]
    except sqlite3.OperationalError:
        return []
    finally:
        conn.close()


# ---- 呼び出し元 ----


@pytest.mark.parametrize("origin", [None, "https://evil.example"])
def test_other_sites_are_refused_and_nothing_is_recorded(client, db_path, origin):
    r = ask(client, origin=origin)
    assert r.status_code == 403
    assert r.json()["error"] == "forbidden_origin"
    assert "access-control-allow-origin" not in r.headers
    assert logs(db_path) == []


def test_other_sites_are_refused_before_the_input_is_checked(client):
    assert ask(client, question="", origin="https://evil.example").status_code == 403


def test_preflight_from_allowed_site(client):
    r = client.options("/api/chat", headers={"Origin": SITE, "Access-Control-Request-Method": "POST"})
    assert r.status_code == 204
    assert r.headers["access-control-allow-origin"] == SITE
    assert client.options("/api/chat", headers={"Origin": "https://evil.example"}).status_code == 403


# ---- 入力の確認 ----


@pytest.mark.parametrize(
    "body",
    [
        {"question": "", "browser_id": BROWSER},
        {"question": "あ" * 2001, "browser_id": BROWSER},
        {"question": "q"},
        {"question": "q", "browser_id": "bad id"},
        {"question": "q", "browser_id": BROWSER, "context": {"screen": "admin"}},
        {"question": "q", "browser_id": BROWSER, "history": [{"role": "user", "text": "x"}] * 7},
    ],
)
def test_wrong_input_gets_400_with_cors_and_is_not_counted(client, db_path, body):
    r = client.post("/api/chat", json=body, headers={"Origin": SITE})
    assert r.status_code == 400
    assert r.json() == {"error": "invalid_input", "message": guard.ERRORS["invalid_input"][1]}
    assert r.headers["access-control-allow-origin"] == SITE  # 画面が理由を読めるように
    assert logs(db_path) == []


def test_error_reply_does_not_echo_what_was_sent(client):
    r = client.post("/api/chat", json={"question": "q", "browser_id": "sk-ant-secret key"}, headers={"Origin": SITE})
    assert "sk-ant" not in r.text


def test_broken_json_gets_400(client):
    r = client.post("/api/chat", content=b"{not json", headers={"Origin": SITE, "Content-Type": "application/json"})
    assert r.status_code == 400


# ---- 答えが順に届く ----


def test_answer_is_streamed_as_events(client, llm):
    r = ask(client)
    assert r.status_code == 200
    assert r.headers["content-type"].startswith("text/event-stream")
    assert r.headers["access-control-allow-origin"] == SITE
    events = events_of(r)
    assert [kind for kind, _ in events] == ["status", "status", "delta", "delta", "done"]
    assert "".join(d["text"] for k, d in events if k == "delta").startswith("文字で命令を打つ画面です。")
    done = events[-1][1]
    assert done["result"] == "answered" and done["sources"][0]["label"] == "10月期 第1回"


def test_question_is_recorded_redacted_and_without_who_asked(client, db_path, llm):
    r = ask(
        client,
        question="/Users/taro/app で ANTHROPIC_API_KEY=sk-ant-api03-AbCdEfGhIjKlMnOpQrSt と出ました",
        context={"course": "2026-10", "set_id": "lecture-1", "screen": "question"},
        history=[
            {"role": "user", "text": "taro@example.com です。CLIって何ですか"},
            {"role": "assistant", "text": "文字で命令を打つ画面です"},
        ],
    )
    log_id = events_of(r)[-1][1]["log_id"]

    (row,) = logs(db_path)
    assert row["id"] == log_id
    assert row["question"] == f"/Users/{guard.NAME_MARK}/app で ANTHROPIC_API_KEY={guard.KEY_MARK} と出ました"
    assert (row["course"], row["set_id"], row["screen"]) == ("2026-10", "lecture-1", "question")
    assert row["result"] == "answered" and row["helpful"] is None
    assert json.loads(row["chunk_ids"]) == ["lecture-1-qa-1"]
    assert (row["input_tokens"], row["output_tokens"]) == (5800, 140)
    assert row["content_version"] == "v-test"
    assert row["latency_first_ms"] is not None and row["latency_total_ms"] is not None
    everything = " ".join(str(v) for v in row.values())
    assert BROWSER not in everything  # ブラウザのIDを記録しない
    assert "CLIって何ですか" not in everything  # 直前のやりとりを記録しない
    assert "文字で命令を打つ画面です。" not in everything  # 答えの本文を記録しない

    # AIにも、伏せ字にしたあとのものだけを渡す
    sent = llm.sent("select") + llm.sent("answer")
    assert "sk-ant" not in sent and "taro" not in sent and BROWSER not in sent
    assert guard.KEY_MARK in sent and guard.MAIL_MARK in sent


def test_out_of_scope_not_in_material_and_general_are_recorded(client, db_path, llm):
    llm.selection = {"related": False, "numbers": []}
    ask(client, question="今日の天気は")
    llm.selection = {"related": True, "numbers": []}
    llm.pieces = ["【資料なし】"]
    ask(client, question="講義の録画はいつまで見られますか", browser="another-browser-0002")
    llm.pieces = ["【資料外】\n", "一般的な説明です。"]
    ask(client, question="Docker って何ですか", browser="another-browser-0003")
    assert sorted(row["result"] for row in logs(db_path)) == ["general", "not_in_material", "out_of_scope"]


def test_page_must_be_a_positive_number(client):
    assert ask(client, context={"set_id": "lecture-1", "page": 3}).status_code == 200
    assert ask(client, context={"set_id": "lecture-1", "page": 0}).status_code == 400
    assert ask(client, context={"set_id": "lecture-1", "page": "abc"}).status_code == 400


def test_ai_failure_is_an_error_event_and_recorded_as_error(client, db_path, llm):
    llm.select_error = broken()
    r = ask(client)
    assert r.status_code == 200  # 流し始めたあとなので、状態コードではなく知らせで伝える
    assert events_of(r)[-1][0] == "error"
    assert logs(db_path)[0]["result"] == "error"


def test_missing_key_or_content_is_503_and_not_counted(client, db_path, monkeypatch):
    monkeypatch.delenv("ANTHROPIC_API_KEY")
    r = ask(client)
    assert r.status_code == 503 and r.json()["error"] == "unavailable"
    monkeypatch.setenv("ANTHROPIC_API_KEY", "test-key")
    from content import ContentStore

    monkeypatch.setattr("app.content_store", ContentStore())
    monkeypatch.delenv("CONTENT_URL", raising=False)
    assert ask(client).status_code == 503
    assert logs(db_path) == []
    # 上の2回は数えられていないので、資料が戻れば10問ぜんぶ使える
    monkeypatch.setattr("app.content_store", loaded_store())
    for _ in range(guard.BROWSER_DAILY_LIMIT):
        assert ask(client).status_code == 200


# ---- 回数制限 ----


def test_eleventh_question_from_one_browser_is_refused(client, db_path):
    for _ in range(guard.BROWSER_DAILY_LIMIT):
        assert ask(client).status_code == 200
    r = ask(client)
    assert r.status_code == 429
    assert r.json() == {"error": "browser_limit", "message": guard.ERRORS["browser_limit"][1]}
    assert len(logs(db_path)) == guard.BROWSER_DAILY_LIMIT
    assert ask(client, browser="another-browser-0002").status_code == 200


def test_global_limit_refuses_everyone(client, monkeypatch):
    monkeypatch.setattr(guard, "GLOBAL_DAILY_LIMIT", 2)
    assert ask(client, browser="browser-0001").status_code == 200
    assert ask(client, browser="browser-0002").status_code == 200
    r = ask(client, browser="browser-0003")
    assert r.status_code == 429 and r.json()["error"] == "global_limit"


def test_emergency_stop(client, db_path, llm, monkeypatch):
    monkeypatch.setenv("CHAT_ENABLED", "false")
    r = ask(client)
    assert r.status_code == 503 and r.json()["error"] == "maintenance"
    assert logs(db_path) == []  # 止めている間は数えも記録もしない
    assert llm.calls == []  # AIも呼ばない
    assert client.get("/api/health").status_code == 200


# ---- 評価 ----


def test_feedback_is_recorded(client, db_path):
    ask(client)
    log_id = logs(db_path)[0]["id"]
    r = client.post("/api/chat/feedback", json={"log_id": log_id, "helpful": False}, headers={"Origin": SITE})
    assert r.status_code == 200 and r.json() == {"ok": True}
    assert logs(db_path)[0]["helpful"] == 0


def test_feedback_for_unknown_log_is_404_and_wrong_shape_is_400(client):
    headers = {"Origin": SITE}
    unknown = "00000000-0000-4000-8000-000000000000"
    assert (
        client.post("/api/chat/feedback", json={"log_id": unknown, "helpful": True}, headers=headers).status_code == 404
    )
    assert client.post("/api/chat/feedback", json={"log_id": "x", "helpful": True}, headers=headers).status_code == 400
    assert client.post("/api/chat/feedback", json={"log_id": unknown, "helpful": True}).status_code == 403
