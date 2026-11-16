-- 表を作る。設計書 6章。
-- 本番（D1）と手元（SQLite）で同じ SQL を使う。

-- 質問の記録。誰の質問かは持たない（名前・メール・IPアドレス・ブラウザのIDを入れない）
CREATE TABLE IF NOT EXISTS chat_logs (
  id               TEXT PRIMARY KEY,  -- ランダムなID。利用者とは結びつかない
  created_at       TEXT NOT NULL,     -- 日時（UTC）
  question         TEXT NOT NULL,     -- 伏せ字にしたあとの質問文
  course           TEXT,              -- 開いていた期
  set_id           TEXT,              -- 開いていた回
  screen           TEXT,              -- 開いていた画面の種類
  chunk_ids        TEXT NOT NULL DEFAULT '[]',  -- 選ばれたかたまりのID（JSONの配列）
  result           TEXT NOT NULL,     -- answered / not_in_material / out_of_scope / error
  helpful          INTEGER,           -- 1＝役に立った、0＝立たなかった、空＝未回答
  latency_first_ms INTEGER,           -- 最初の文字までの時間
  latency_total_ms INTEGER,           -- 全部出るまでの時間
  input_tokens     INTEGER,           -- AIに渡した量
  output_tokens    INTEGER,           -- AIが書いた量
  content_version  TEXT               -- そのとき使った資料の版
);

CREATE INDEX IF NOT EXISTS chat_logs_created_at ON chat_logs (created_at);

-- 全体の1日の質問数
CREATE TABLE IF NOT EXISTS daily_usage (
  day TEXT PRIMARY KEY,               -- 日付（日本時間）
  n   INTEGER NOT NULL DEFAULT 0
);

-- ブラウザごとの1日の質問数。質問の記録とは結びつけない。前日以前の行は消す
CREATE TABLE IF NOT EXISTS browser_usage (
  day          TEXT NOT NULL,         -- 日付（日本時間）
  browser_hash TEXT NOT NULL,         -- ブラウザのIDを元に戻せない形に変えたもの
  n            INTEGER NOT NULL DEFAULT 0,
  PRIMARY KEY (day, browser_hash)
);
