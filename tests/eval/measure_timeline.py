"""動いている API に質問を送り、時間の内訳を測る。判断の記録 D-25 の追記（遅さの調査）。

API は答えの前に「探しています」「答えを書いています」の知らせを送る。
それぞれが届いた時刻を記録すると、API を作り直さずに、外から内訳が分かる。

  処理に入るまで … 送ってから「探しています」が届くまで（Worker の起動・資料の用意・回数の記録）
  資料選び       … 「探しています」から「答えを書いています」まで（Claude API の1回目）
  書き始め       … 「答えを書いています」から最初の文字まで（Claude API の2回目）

本物の Claude API が呼ばれるので料金がかかり（1問 約0.04〜0.14円の見込み）、その日の回数にも数えられる。

使い方:
  .tools/bin/uv run python tests/eval/measure_timeline.py [--url API のURL] [--origin 呼び出し元]
                                                           [--waits 0,0,0,120,0,0]

--waits は、質問ごとの「送る前に待つ秒数」。長く待つと、Worker が眠ったあとの1問目を測れる。
"""

import argparse
import json
import statistics
import time
import uuid

import httpx2

QUESTIONS = [
    "黒い画面って何ですか",
    "コミットとプッシュの違いは何ですか",
    "APIキーはどこに保存すればいいですか",
    "ブランチって何のために使うのですか",
    "間違えて鍵を GitHub に上げてしまいました。どうすればいいですか",
]


def ask(url: str, origin: str, question: str, browser_id: str) -> dict:
    """1問送り、知らせが届いた時刻（ミリ秒）を返す。"""
    arrived: dict[str, float] = {}
    result = None
    started = time.perf_counter()
    # 毎回つなぎ直す（受講生のブラウザが、久しぶりに質問する場面に近づけるため）
    with httpx2.Client(timeout=60) as client:
        body = {"question": question, "browser_id": browser_id}
        with client.stream("POST", url, headers={"Origin": origin}, json=body) as response:
            if response.status_code != 200:
                response.read()
                return {"result": f"status_{response.status_code}"}
            event = None
            for line in response.iter_lines():
                now = time.perf_counter() - started
                if line.startswith("event:"):
                    event = line[6:].strip()
                elif line.startswith("data:"):
                    data = json.loads(line[5:])
                    if event == "status":
                        arrived.setdefault(data["state"], now)
                    elif event == "delta":
                        arrived.setdefault("first", now)
                    elif event in ("done", "error"):
                        arrived["end"] = now
                        result = data.get("result") or data.get("error")
    ms = {name: int(seconds * 1000) for name, seconds in arrived.items()}
    answering = ms.get("answering")
    return {
        "result": result,
        "enter": ms.get("searching"),
        "select": answering - ms["searching"] if answering is not None else None,
        "write": ms["first"] - answering if answering is not None and "first" in ms else None,
        "first": ms.get("first"),
        "end": ms.get("end"),
    }


def middle(rows: list[dict], name: str) -> str:
    values = [row[name] for row in rows if row.get(name) is not None]
    if not values:
        return "—"
    return f"真ん中 {int(statistics.median(values))}ms（{min(values)}〜{max(values)}）"


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("--url", default="https://quest-chat-api.example.workers.dev/api/chat")
    parser.add_argument("--origin", default="https://quest-app.example.com")
    parser.add_argument("--waits", default="0,0,0,0,0,0")
    args = parser.parse_args()
    waits = [int(x) for x in args.waits.split(",")]
    # 1ブラウザ1日10問の上限に当たらないよう、10問ごとにIDを変える
    browser_ids = [f"measure-{uuid.uuid4().hex[:12]}" for _ in range(len(waits) // 10 + 1)]

    rows = []
    for n, wait in enumerate(waits):
        if wait:
            time.sleep(wait)
        row = ask(args.url, args.origin, QUESTIONS[n % len(QUESTIONS)], browser_ids[n // 10])
        rows.append(row)
        print(
            f"{n + 1:>2} 待ち{wait:>4}秒  処理に入るまで {row.get('enter')}ms  資料選び {row.get('select')}ms  "
            f"書き始め {row.get('write')}ms  最初の文字 {row.get('first')}ms  全部 {row.get('end')}ms  {row['result']}",
            flush=True,
        )

    firsts = [row["first"] for row in rows if row.get("first") is not None]
    print()
    print(f"処理に入るまで: {middle(rows, 'enter')}")
    print(f"資料選び:       {middle(rows, 'select')}")
    print(f"書き始め:       {middle(rows, 'write')}")
    print(f"最初の文字まで: {middle(rows, 'first')}  3秒以内 {sum(f <= 3000 for f in firsts)}/{len(firsts)}")


if __name__ == "__main__":
    main()
