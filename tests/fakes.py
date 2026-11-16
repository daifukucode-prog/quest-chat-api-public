"""テストで使う偽物。AIも通信も使わずに、流れを確かめるためのもの。"""

import json

from answer import SUPPLEMENT_RULES
from content import ContentStore, parse_content
from llm import LlmError, Usage


def sample_content():
    def chunk(id, set_id, label, kind, title, text, **more):
        return {
            "id": id,
            "course": "2026-10",
            "set_id": set_id,
            "label": label,
            "kind": kind,
            "title": title,
            "pages": [19],
            "text": text,
            **more,
        }

    return parse_content(
        {
            "version": "v-test",
            "chunks": [
                chunk(
                    "lecture-1-summary",
                    "lecture-1",
                    "10月期 第1回",
                    "summary",
                    "要点：最初の自動運転",
                    "・入り口はGUI・CLI・IDEの3つ。（p.35）",
                ),
                chunk(
                    "lecture-2-summary",
                    "lecture-2",
                    "10月期 第2回",
                    "summary",
                    "要点：AIが働く仕組み",
                    "・毎回読むルールはCLAUDE.md。（p.19）",
                ),
                chunk(
                    "lecture-1-qa-1",
                    "lecture-1",
                    "10月期 第1回",
                    "qa",
                    "解説：操作の基礎（ターミナル）",
                    "問：ターミナルとは\n答：文字で命令を打つ画面\n解説：黒い画面のこと。（p.35）",
                    question_ids=["2026-10-lecture1-009"],
                ),
            ],
            "glossary": [
                {"term": "ターミナル", "reading": "たーみなる", "description": "文字で命令を打つ画面。"},
                {"term": "API", "reading": "えーぴーあい", "description": "ソフト同士がやりとりする窓口。"},
                {"term": "APIキー", "reading": "えーぴーあいきー", "description": "窓口を使うための自分専用の鍵。"},
            ],
        }
    )


def loaded_store() -> ContentStore:
    """資料を読み込み済みの入れ物（取りに行かない）。"""
    store = ContentStore()
    store.content = sample_content()
    store.fetched_at = 1e12
    return store


class FakeLlm:
    """Claude の代わり。決めておいた答えを返し、渡されたものを覚えておく。"""

    def __init__(
        self,
        selection=None,
        pieces=None,
        select_error=None,
        answer_error=None,
        supplement_pieces=None,
        supplement_error=None,
    ):
        self.selection = {"related": True, "numbers": [2]} if selection is None else selection
        self.pieces = ["文字で命令を打つ画面です。", "\n出典：10月期 第1回 p.35"] if pieces is None else pieces
        self.select_error = select_error
        self.answer_error = answer_error
        # 補足の係（資料が答えていない残りを書く、2回目の呼び出し）が返すもの
        self.supplement_pieces = (
            ["ほかには Firebase などがあります。"] if supplement_pieces is None else supplement_pieces
        )
        self.supplement_error = supplement_error
        self.calls = []  # (種類, 指示文, 質問の側)
        self.efforts = []  # 呼び出しごとの「考える量」

    async def ask_json(self, system, user, schema, max_tokens, effort):
        self.calls.append(("select", system, user))
        self.efforts.append(effort)
        if self.select_error:
            raise self.select_error
        return json.loads(json.dumps(self.selection)), Usage(5000, 20, 0)

    async def stream_text(self, system, user, max_tokens, effort, usage):
        # 指示文で、答えを書く係か、補足を書く係かを見分ける
        supplement = system == SUPPLEMENT_RULES
        self.calls.append(("supplement" if supplement else "answer", system, user))
        self.efforts.append(effort)
        for piece in self.supplement_pieces if supplement else self.pieces:
            yield piece
        usage.add(Usage(300, 80, 0) if supplement else Usage(800, 120, 0))
        error = self.supplement_error if supplement else self.answer_error
        if error:
            raise error

    def sent(self, kind):
        """その種類の呼び出しで、AIに渡した文（指示文＋質問の側）。"""
        return "\n".join(system + "\n" + user for k, system, user in self.calls if k == kind)


def busy():
    return LlmError("busy", "rate_limit")


def broken():
    return LlmError("unavailable", "connection")
