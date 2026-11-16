"""記録と回数（store.py）と、回数制限（guard.count_question）のテスト。手元の SQLite を使う。"""

import asyncio
import contextlib
import json

import pytest

import guard
import store
from guard import ApiError

DAY = "2026-11-16"
NOON = 1794798000  # 2026-11-16 12:00（日本時間）
ONE_DAY = 86400


@pytest.fixture
def db(tmp_path):
    database = store.SqliteDatabase(str(tmp_path / "test.sqlite3"))
    yield database
    database.close()


def run(coro):
    return asyncio.run(coro)


def rows(db, sql, params=()):
    return [dict(r) for r in db._conn.execute(sql, params).fetchall()]


# ---- 回数 ----


def test_daily_usage_counts_up_from_one(db):
    assert [run(store.add_daily_usage(db, DAY)) for _ in range(3)] == [1, 2, 3]
    assert run(store.add_daily_usage(db, "2026-11-17")) == 1  # 日が変わると1から


def test_browser_usage_is_counted_per_browser(db):
    assert run(store.add_browser_usage(db, DAY, "aaa")) == 1
    assert run(store.add_browser_usage(db, DAY, "aaa")) == 2
    assert run(store.add_browser_usage(db, DAY, "bbb")) == 1


def test_counting_does_not_lose_any_when_many_come_at_once(db):
    async def many():
        await asyncio.gather(*[store.add_daily_usage(db, DAY) for _ in range(40)])
        return await store.add_daily_usage(db, DAY)

    assert run(many()) == 41


def test_old_browser_usage_is_deleted(db):
    run(store.add_browser_usage(db, "2026-11-15", "old"))
    run(store.add_browser_usage(db, DAY, "today"))
    assert run(store.delete_old_browser_usage(db, DAY)) == 1
    assert [r["browser_hash"] for r in rows(db, "SELECT browser_hash FROM browser_usage")] == ["today"]


# ---- 回数制限 ----


def test_browser_is_refused_after_the_daily_limit(db):
    for _ in range(guard.BROWSER_DAILY_LIMIT):
        run(guard.count_question(db, "browser-aaaa", NOON))
    with pytest.raises(ApiError) as e:
        run(guard.count_question(db, "browser-aaaa", NOON))
    assert e.value.kind == "browser_limit" and e.value.status == 429
    run(guard.count_question(db, "browser-bbbb", NOON))  # 別のブラウザは使える


def test_refused_question_does_not_use_up_the_global_count(db):
    for _ in range(guard.BROWSER_DAILY_LIMIT + 5):
        with contextlib.suppress(ApiError):  # 上限を超えたぶんは断られる。それでよい
            run(guard.count_question(db, "browser-aaaa", NOON))
    assert rows(db, "SELECT n FROM daily_usage")[0]["n"] == guard.BROWSER_DAILY_LIMIT


def test_browser_can_ask_again_the_next_day(db):
    for _ in range(guard.BROWSER_DAILY_LIMIT):
        run(guard.count_question(db, "browser-aaaa", NOON))
    run(guard.count_question(db, "browser-aaaa", NOON + ONE_DAY))
    # 次の日の最初の質問で、前の日の行は消える
    assert [r["day"] for r in rows(db, "SELECT day FROM browser_usage")] == ["2026-11-17"]


def test_everyone_is_refused_after_the_global_limit(db, monkeypatch):
    monkeypatch.setattr(guard, "GLOBAL_DAILY_LIMIT", 3)
    for i in range(3):
        run(guard.count_question(db, f"browser-{i:04d}", NOON))
    with pytest.raises(ApiError) as e:
        run(guard.count_question(db, "browser-9999", NOON))
    assert e.value.kind == "global_limit" and e.value.status == 429


def test_browser_id_itself_is_never_stored(db):
    run(guard.count_question(db, "browser-secret-id", NOON))
    stored = rows(db, "SELECT * FROM browser_usage")
    assert "browser-secret-id" not in json.dumps(stored)
    assert stored[0]["browser_hash"] == guard.browser_hash("browser-secret-id", DAY)


# ---- 質問の記録 ----


def test_log_is_saved_and_read_back_in_japanese(db):
    run(
        store.save_log(
            db,
            id="id-1",
            created_at="2026-11-16T03:00:00Z",
            question="黒い画面ってどこから開くの",
            set_id="lecture-1",
            screen="question",
            chunk_ids=["lecture-1-qa-2", "lecture-1-summary"],
            result="answered",
            latency_first_ms=1200,
            input_tokens=3000,
            content_version="v1",
        )
    )
    row = rows(db, "SELECT * FROM chat_logs")[0]
    assert row["question"] == "黒い画面ってどこから開くの"
    assert json.loads(row["chunk_ids"]) == ["lecture-1-qa-2", "lecture-1-summary"]
    assert row["course"] is None and row["helpful"] is None and row["output_tokens"] is None
    assert row["latency_first_ms"] == 1200


def test_log_table_has_no_column_for_who_asked(db):
    columns = {r["name"] for r in rows(db, "PRAGMA table_info(chat_logs)")}
    assert columns == set(store.LOG_COLUMNS) | {"helpful"}
    assert not columns & {"browser_id", "browser_hash", "ip", "user", "name", "email", "answer", "history"}


def test_log_refuses_unknown_columns(db):
    with pytest.raises(ValueError):
        run(store.save_log(db, id="id-1", created_at="x", question="q", result="error", browser_id="b"))


def test_question_text_cannot_change_the_sql(db):
    nasty = "'); DROP TABLE chat_logs; --"
    run(store.save_log(db, id="id-1", created_at="x", question=nasty, result="error"))
    assert rows(db, "SELECT question FROM chat_logs")[0]["question"] == nasty


def test_helpful_is_recorded_only_for_existing_logs(db):
    run(store.save_log(db, id="id-1", created_at="x", question="q", result="answered"))
    assert run(store.set_helpful(db, "id-1", True)) is True
    assert rows(db, "SELECT helpful FROM chat_logs")[0]["helpful"] == 1
    assert run(store.set_helpful(db, "id-1", False)) is True
    assert rows(db, "SELECT helpful FROM chat_logs")[0]["helpful"] == 0
    assert run(store.set_helpful(db, "no-such-id", True)) is False


# ---- 本番（D1）用のつなぎ ----


class FakeD1:
    """D1 の代わり。pywrangler dev で見た実際の返り方をまねる。

    行は dict の仲間（workers.rpc.JsDict）で返り、行がなければ空の値が返る。
    手元の SQLite のテストだけでは、この違いに気づけなかった（判断の記録 D-23）。
    """

    class JsDict(dict):
        pass

    class Meta:
        changes = 1

    class Result:
        pass

    def __init__(self, row):
        self.row, self.calls = row, []

    def prepare(self, sql):
        self.calls.append(["prepare", sql])
        return self

    def bind(self, *params):
        self.calls.append(["bind", params])
        return self

    async def first(self):
        return self.JsDict(self.row) if self.row is not None else None

    async def run(self):
        result = self.Result()
        result.meta = self.Meta()
        return result


def test_d1_row_becomes_a_plain_dict():
    d1 = FakeD1({"n": 3})
    row = run(store.D1Database(d1).first("SELECT n FROM daily_usage WHERE day = ?", (DAY,)))
    assert row == {"n": 3} and type(row) is dict
    assert d1.calls == [["prepare", "SELECT n FROM daily_usage WHERE day = ?"], ["bind", (DAY,)]]


def test_d1_missing_row_is_none():
    assert run(store.D1Database(FakeD1(None)).first("SELECT 1")) is None


def test_d1_run_returns_changed_rows():
    assert run(store.D1Database(FakeD1(None)).run("DELETE FROM browser_usage WHERE day < ?", (DAY,))) == 1


def test_counting_works_through_d1():
    assert run(store.add_daily_usage(store.D1Database(FakeD1({"n": 7})), DAY)) == 7


def test_count_fails_loudly_when_no_row_comes_back():
    # 数が返らないときに 0 として進めると、上限を超えても通してしまう。はっきり失敗させる
    assert store.count_of({"n": 3}) == 3
    with pytest.raises(RuntimeError):
        store.count_of(None)
