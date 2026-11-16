"""記録と回数の保存。設計書 6章。

本番は Cloudflare D1、手元は SQLite のファイル。どちらも中身は SQLite なので、SQL は同じ。
違うのは呼び出し方だけで、その違いを下の2つのクラスに閉じ込める。

SQL には値を直接つなげず、必ず ? に別で渡す（不正な命令を混ぜられないようにするため）。
"""

import json
from pathlib import Path
from typing import Protocol

MIGRATIONS_DIR = Path(__file__).resolve().parent.parent / "migrations"


class Database(Protocol):
    """データベースにできること。2つだけ。"""

    async def run(self, sql: str, params: tuple = ()) -> int:
        """書き込む。変わった行の数を返す。"""
        ...

    async def first(self, sql: str, params: tuple = ()) -> dict | None:
        """最初の1行を読む。なければ None。"""
        ...


class SqliteDatabase:
    """手元の開発とテスト用。SQLite のファイルを使う。"""

    def __init__(self, path: str) -> None:
        # 本番（Workers）では使わない部品なので、使うときにだけ読み込む
        import sqlite3

        if path != ":memory:":
            Path(path).parent.mkdir(parents=True, exist_ok=True)
        self._conn = sqlite3.connect(path, isolation_level=None)  # 1文ごとに確定する
        self._conn.row_factory = sqlite3.Row
        # 手元では、表がなければここで作る（本番の D1 は wrangler で作る）
        for file in sorted(MIGRATIONS_DIR.glob("*.sql")):
            self._conn.executescript(file.read_text(encoding="utf-8"))

    async def run(self, sql: str, params: tuple = ()) -> int:
        return self._conn.execute(sql, params).rowcount

    async def first(self, sql: str, params: tuple = ()) -> dict | None:
        row = self._conn.execute(sql, params).fetchone()
        return dict(row) if row is not None else None

    def close(self) -> None:
        self._conn.close()


class D1Database:
    """本番用。Workers の設定にある D1（env.DB）を使う。"""

    def __init__(self, d1) -> None:
        self._d1 = d1

    async def run(self, sql: str, params: tuple = ()) -> int:
        result = await self._d1.prepare(sql).bind(*params).run()
        return int(result.meta.changes)

    async def first(self, sql: str, params: tuple = ()) -> dict | None:
        row = await self._d1.prepare(sql).bind(*params).first()
        # 行があれば dict の仲間（workers.rpc.JsDict）、なければ空の値が返る。
        # ふつうの dict に直して、手元の SQLite と同じ形にそろえる
        if not row:
            return None
        return dict(row)


# ---- 回数 ----


def count_of(row: dict | None) -> int:
    """「足したあとの数」を返す SQL の結果から、数を取り出す。

    RETURNING を付けているので、行は必ず返るはず。返らなかったら、数え方が壊れているので、
    0 として先へ進めず、はっきり失敗させる（0 にすると、上限を超えても通してしまう）。
    """
    if row is None:
        raise RuntimeError("回数を足した結果が返りませんでした")
    return int(row["n"])


async def add_daily_usage(db: Database, day: str) -> int:
    """全体の1日の質問数に1足し、足したあとの数を返す。

    「いまの数を読む → 1足して書く」の2手に分けると、同時に質問が来たときに数がずれる。
    SQL の1文で足す。
    """
    row = await db.first(
        "INSERT INTO daily_usage (day, n) VALUES (?, 1) ON CONFLICT (day) DO UPDATE SET n = n + 1 RETURNING n",
        (day,),
    )
    return count_of(row)


async def add_browser_usage(db: Database, day: str, browser_hash: str) -> int:
    """ブラウザごとの1日の質問数に1足し、足したあとの数を返す。"""
    row = await db.first(
        "INSERT INTO browser_usage (day, browser_hash, n) VALUES (?, ?, 1) "
        "ON CONFLICT (day, browser_hash) DO UPDATE SET n = n + 1 RETURNING n",
        (day, browser_hash),
    )
    return count_of(row)


async def delete_old_browser_usage(db: Database, today: str) -> int:
    """前日以前のブラウザごとの回数を消す。"""
    return await db.run("DELETE FROM browser_usage WHERE day < ?", (today,))


# ---- 質問の記録 ----

# chat_logs に入れてよい列。ここにない名前は保存しない
LOG_COLUMNS = (
    "id",
    "created_at",
    "question",
    "course",
    "set_id",
    "screen",
    "chunk_ids",
    "result",
    "latency_first_ms",
    "latency_total_ms",
    "input_tokens",
    "output_tokens",
    "content_version",
)


async def save_log(db: Database, **values) -> None:
    """質問の記録を1行足す。

    値が空（None）の列は書かない（表の側で空になる）。
    列の名前は上の一覧にあるものだけを使い、利用者が送った文字は列の名前に使わない。
    """
    unknown = set(values) - set(LOG_COLUMNS)
    if unknown:
        raise ValueError(f"chat_logs にない列です: {sorted(unknown)}")
    if "chunk_ids" in values:
        values["chunk_ids"] = json.dumps(list(values["chunk_ids"]), ensure_ascii=False)
    filled = {name: value for name, value in values.items() if value is not None}
    columns = ", ".join(filled)
    marks = ", ".join("?" for _ in filled)
    await db.run(f"INSERT INTO chat_logs ({columns}) VALUES ({marks})", tuple(filled.values()))


async def set_helpful(db: Database, log_id: str, helpful: bool) -> bool:
    """「役に立った／立たなかった」を記録する。その記録がなければ False。"""
    changed = await db.run("UPDATE chat_logs SET helpful = ? WHERE id = ?", (1 if helpful else 0, log_id))
    return changed > 0
