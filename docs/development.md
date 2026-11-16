# 開発の手引き（動かし方・確かめ方・公開のしかた）

README から移した、作業用の手順。全体の説明は [README](../README.md) を参照。

## 手元で動かす

Python 3.12 と Node.js（22.18 以上）が要る。`uv` はフォルダの中に入れて使う（最初の1回だけ）。

```bash
python3 -m venv .tools && .tools/bin/pip install uv
```

```bash
.tools/bin/uv sync
```

### 1. 資料まとめファイルを作る

クエストアプリのデータを読んで、`build/chat-content.json` を作る（アプリは書き換えない）。

```bash
node tools/build-content.mjs <クエストアプリのフォルダ>
```

### 2. 資料を配る

API は資料を URL から取りに行くので、手元では `build/` を小さなサーバーで配る。別のターミナルで動かしたままにする。

```bash
python3 -m http.server 4174 --directory build --bind 127.0.0.1
```

### 3. API を動かす

手元の設定は `.dev.vars` に書く（Cloudflare Workers での `.env` にあたるファイル。git には入らない）。最初に `.dev.vars.example` を `.dev.vars` という名前で写しておく。

Uvicorn で動かす（ふだんの開発）。`--env-file` で同じ `.dev.vars` を読ませる：

```bash
.tools/bin/uv run uvicorn app:app --app-dir src --reload --port 8000 --env-file .dev.vars
```

本番と同じ実行の仕組みで動かす（Cloudflare Workers の手元版）。`.dev.vars` は自動で読まれる：

```bash
PATH="$PWD/.tools/bin:$PATH" .tools/bin/uv run pywrangler dev
```

どちらも `/api/health` を開いて、`"content_loaded": true` と資料の版が返れば動いている。

### 4. データベース

- Uvicorn で動かすとき：何もしなくてよい。`build/dev.sqlite3` に SQLite のファイルが自動で作られる。
- `pywrangler dev` で動かすとき：最初の1回だけ、手元の D1 に表を作る。

```bash
npx wrangler d1 migrations apply quest-chat-api --local
```

### 5. 質問を送ってみる

許可したサイトからの呼び出しだけを受け付けるので、`Origin` を付けて送る。`.dev.vars` に `ANTHROPIC_API_KEY` があれば、答えが少しずつ届く（`-N` は、届いた順に表示するための指定）。

```bash
curl -N -X POST http://127.0.0.1:8787/api/chat -H "Origin: http://localhost:4173" -H "Content-Type: application/json" -d '{"question":"ターミナルって何ですか","browser_id":"test-browser-0001"}'
```


## テスト

```bash
.tools/bin/uv run pytest
```

AIは偽物に差し替えてあるので、キーも料金も要らない。

## 答えの質を確かめる（評価用の質問集）

本物の Claude API を呼ぶ（30問で数円の見込み）。結果の表が画面に出て、答えの全文つきの報告が `build/eval-report.md` にできる。

```bash
.tools/bin/uv run python tests/eval/run_eval.py
```

指示文やモデルの設定を変えたら流し直し、前より悪くなっていないかを見る。質問は `tests/eval/questions.yaml` にある。

答えた質問については、「資料にない文が入っていないか」を別の呼び出しでAIに採点させる（`--no-judge` で省ける）。採点も間違えるので、指摘された文は報告を読んで確かめる。考える量を変えて比べるときは `--answer-effort medium --out eval-medium.md` のように指定する。

## 速さの内訳を測る

動いている API に質問を送り、「処理に入るまで」「資料選び」「書き始め」に分けて時間を出す（本物の Claude API が呼ばれ、その日の回数にも数えられる）。何も指定しないと、公開した API に6問送る。

```bash
.tools/bin/uv run python tests/eval/measure_timeline.py --waits 0,0,0,120,0,0
```

`--waits` は、質問ごとの「送る前に待つ秒数」。Claude API の速さは時間帯で揺れるので、変更の前後を比べるときは、同じ時間帯に続けて測る。

## Cloudflare に公開する

公開は、本人の了承を取ってから行う。先に、このフォルダが正しいアカウント（`開発用のアカウント`）に結び付いているかを確かめる。

```bash
npx wrangler@4.162.0 whoami
```

```bash
PATH="$PWD/.tools/bin:$PATH" .tools/bin/uv run pywrangler deploy
```

公開したら、`https://quest-chat-api.example.workers.dev/api/health` を開いて資料の版が返ることと、実際に質問して答えが届くことを確かめる（`tests/eval/measure_timeline.py`）。手元では出ないエラー（メモリや計算の時間の上限）があるので、公開した状態で確かめるまで「動く」とは言わない。

## 書き方と型を確かめる

GitHub に送ると、下の4つが自動で流れる（`.github/workflows/ci.yml`）。送る前に手元で流すと、失敗に早く気づける。

```bash
.tools/bin/uv run ruff check .
```

```bash
.tools/bin/uv run ruff format --check .
```

```bash
.tools/bin/uv run mypy
```

```bash
.tools/bin/uv run pytest
```

- `ruff check`：まずい書き方（使っていない読み込み、間違いやすい書き方など）を見つける。`--fix` を付けると、直せるものは直す。
- `ruff format`：改行や空白の見た目をそろえる。`--check` を外すと、その場で書き直す。
- `mypy`：型が合っているかを確かめる（`src/` だけ）。
- 設定は `pyproject.toml` の `[tool.ruff]` と `[tool.mypy]` にある。
