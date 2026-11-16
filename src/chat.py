"""質問から答えまでの流れ。設計書 3章の③〜⑤をつなぐ。

FastAPI にもデータベースにも触らない。知らせ（status / delta / done / error）を順に返すだけ。
app.py がそれを画面に届け、記録する。評価用のスクリプトも、同じ流れを直接呼ぶ。
"""

from collections.abc import AsyncIterator, Callable
from dataclasses import dataclass, field

import answer as answer_settings
import retrieve
from answer import (
    ANSWER_MAX_TOKENS,
    COMMUNITY_TEXT,
    GENERAL_HEAD,
    GENERAL_LABEL,
    GENERAL_MARK,
    GENERAL_TAIL,
    NO_MATERIAL_MARK,
    NOT_IN_MATERIAL_TEXT,
    OUT_OF_SCOPE_TEXT,
    SUPPLEMENT_MARK,
    SUPPLEMENT_MAX_TOKENS,
    SUPPLEMENT_NEEDS,
    build_answer_prompt,
    build_supplement_prompt,
)
from content import Chunk, Content
from guard import ERRORS
from llm import LlmError, Usage
from schemas import ChatContext, HistoryItem


@dataclass
class Outcome:
    """1問の結果。流れが終わったあと、記録に使う。"""

    result: str = "error"  # answered / general / not_in_material / community / out_of_scope / error
    chunk_ids: list[str] = field(default_factory=list)
    usage: Usage = field(default_factory=Usage)
    first_ms: int | None = None  # 最初の文字までの時間
    total_ms: int | None = None  # 全部出るまでの時間
    error_reason: str | None = None  # うまくいかなかったときの短い理由（記録はしない。ログ用）


def split_tail(text: str) -> tuple[str, str]:
    """資料からの答えを、（いま画面に流してよい部分, まだ持っておく部分）に分ける。

    答えのいちばん最後に、補足を頼む印（SUPPLEMENT_MARK）が来ることがある。印は画面に見せないので、
    終わりの部分が「印の書きかけ」に見える間は、流さずに持っておく。終わりの空白も、一緒に持っておく
    （印の前の改行を、先に流してしまわないため）。
    """
    start = text.rfind(SUPPLEMENT_MARK[0])
    if start != -1 and SUPPLEMENT_MARK.startswith(text[start:].rstrip()):
        keep_from = len(text[:start].rstrip())
    else:
        keep_from = len(text.rstrip())
    return text[:keep_from], text[keep_from:]


def source_of(chunk: Chunk) -> dict:
    """画面に出典として出すための、かたまりの情報。"""
    return {
        "id": chunk.id,
        "label": chunk.label,
        "title": chunk.title,
        "kind": chunk.kind,
        "pages": list(chunk.pages),
    }


async def respond(
    llm,
    content: Content,
    question: str,
    context: ChatContext,
    history: list[HistoryItem],
    log_id: str,
    outcome: Outcome,
    clock: Callable[[], float],
) -> AsyncIterator[tuple[str, dict]]:
    """知らせを（種類, 中身）の組で、順に返す。結果は outcome に書き込む。

    question と history は、伏せ字にしたあとのものを渡すこと。
    """
    started = clock()

    def elapsed_ms() -> int:
        return int((clock() - started) * 1000)

    def delta(text: str) -> tuple[str, dict]:
        if outcome.first_ms is None:
            outcome.first_ms = elapsed_ms()
        return "delta", {"text": text}

    def done(sources: list[dict]) -> tuple[str, dict]:
        outcome.total_ms = elapsed_ms()
        return "done", {"result": outcome.result, "sources": sources, "log_id": log_id}

    try:
        # ③ 用語を拾う（AIを使わない）
        terms = retrieve.find_terms(question, content.glossary)

        # ④ 資料を選ぶ
        yield "status", {"state": "searching"}
        selection = await retrieve.select(llm, content, question, context, history)
        outcome.usage.add(selection.usage)

        if selection.community:
            # 「.AI」のコミュニティについての質問。AIに答えを書かせず、「グループで直接聞いてください」と案内する
            outcome.result = "community"
            yield delta(COMMUNITY_TEXT)
            yield done([])
            return

        if not selection.related:
            # クエストと関係ない質問。AIに答えを書かせず、決まった案内文を返す
            outcome.result = "out_of_scope"
            yield delta(OUT_OF_SCOPE_TEXT)
            yield done([])
            return

        outcome.chunk_ids = [chunk.id for chunk in selection.chunks]

        # ⑤ 答えを書く。
        # 読む資料が1つも見つからなかったときも、AIを呼ぶ
        # （講義に関係する質問なら、断り書きを付けて一般的な説明をするため。判断の記録 D-27）
        yield "status", {"state": "answering"}
        system, user = build_answer_prompt(
            question,
            selection.chunks,
            terms,
            retrieve.describe_context_for_answer(content, context),
            retrieve.describe_history(history),
        )
        history_text = retrieve.describe_history(history)
        # AIは、答えの最初か最後に印を書くことがある。印は画面に見せない。
        #   最初の【資料なし】… 答えられない。決まった文に置き換える
        #   最初の【資料外】… 資料にないので、一般的な説明をする。断り書きに置き換えてから、続きを流す
        #   最後の【資料外あり】… 資料は質問の一部にしか答えていない。このあと、補足の係を呼ぶ
        # 最初の印かどうかは数文字が届くまで分からないので、それまでは画面に流さずにためておく
        marks = (NO_MATERIAL_MARK, GENERAL_MARK)
        held = ""
        mode = None  # None（まだ分からない）/ "material" / "general" / "none"
        wrote_anything = False
        tail = ""  # 資料からの答えのうち、まだ流していない終わりの部分（最後の印を見分けるため）
        from_material = ""  # 資料からの答えとして、画面に流した文字（補足の係に渡す）
        async for piece in llm.stream_text(
            system, user, ANSWER_MAX_TOKENS, answer_settings.ANSWER_EFFORT, outcome.usage
        ):
            if mode == "none":
                continue  # 決まった文に置き換えるので、続きは使わない
            if mode == "general" and wrote_anything:
                yield delta(piece)
                continue
            if mode == "material":
                ready, tail = split_tail(tail + piece)
                if ready:
                    from_material += ready
                    yield delta(ready)
                continue
            held += piece
            head = held.lstrip()
            if mode is None:
                if any(len(head) < len(mark) and mark.startswith(head) for mark in marks):
                    continue  # まだ印かどうか分からない
                if head.startswith(NO_MATERIAL_MARK):
                    mode = "none"
                    continue
                if head.startswith(GENERAL_MARK):
                    mode = "general"
                    held = head[len(GENERAL_MARK) :]
                else:
                    mode = "material"
                    ready, tail = split_tail(held)
                    if ready:
                        from_material += ready
                        yield delta(ready)
                    continue
            # 一般的な説明：印のあとの本文が届き始めたら、断り書きを先に出してから流す
            body = held.lstrip()
            if body:
                wrote_anything = True
                yield delta(GENERAL_HEAD + "\n\n" + body)
        if mode is None and held.strip():
            # 短い答えのまま終わった（最初の印ではない）
            mode = "material"
            tail = held

        # 資料からの答えの、持っておいた終わりの部分を片付ける
        wants_supplement = False
        if mode == "material":
            if tail.strip() == SUPPLEMENT_MARK:
                # 印があっても、資料からの答えが「資料には書かれていません」と言っていなければ、補足は呼ばない
                # （資料だけで答えられた質問に、余計な補足を付けないため）。答えが1文字もないときは、呼ぶ
                wants_supplement = not from_material.strip() or any(w in from_material for w in SUPPLEMENT_NEEDS)
            elif tail.strip():
                rest = tail.rstrip()
                from_material += rest
                yield delta(rest)

        # 補足：資料が答えていない残りを、別の係に、一般的な説明として書かせる（判断の記録 D-27 の追記）
        supplemented = False
        if wants_supplement:
            # 資料からの答えがあるときは、その後ろに断り書きを足す。ないときは、答えの全部が一般的な説明になる
            label = "\n\n" + GENERAL_LABEL if from_material.strip() else GENERAL_HEAD
            s_system, s_user = build_supplement_prompt(question, from_material, history_text)
            s_held = ""
            s_none = False
            try:
                async for piece in llm.stream_text(
                    s_system, s_user, SUPPLEMENT_MAX_TOKENS, answer_settings.ANSWER_EFFORT, outcome.usage
                ):
                    if s_none:
                        continue
                    if supplemented:
                        yield delta(piece)
                        continue
                    s_held += piece
                    head = s_held.lstrip()
                    if len(head) < len(NO_MATERIAL_MARK) and NO_MATERIAL_MARK.startswith(head):
                        continue  # まだ印かどうか分からない
                    if head.startswith(NO_MATERIAL_MARK):
                        s_none = True  # 補足できることがない。何も足さない
                    elif head:
                        supplemented = True
                        yield delta(label + "\n\n" + head)
                if not supplemented and not s_none and s_held.strip():
                    supplemented = True
                    yield delta(label + "\n\n" + s_held.strip())
                if supplemented:
                    yield delta("\n\n" + GENERAL_TAIL)
            except LlmError as e:
                # 資料からの答えは出せているので、失敗の知らせにはしない。理由だけ残す
                outcome.error_reason = f"supplement_{e.reason}"

        if mode == "general" and wrote_anything:
            outcome.result = "general"
            yield delta("\n\n" + GENERAL_TAIL)
            yield done([])  # 資料にもとづく答えではないので、出典は付けない
        elif mode == "material" and from_material.strip():
            outcome.result = "answered"
            yield done([source_of(chunk) for chunk in selection.chunks])
        elif mode == "material" and supplemented:
            # 資料からの答えがなく、補足だけになった。一般的な説明として扱う
            outcome.result = "general"
            yield done([])
        elif mode in ("none", "general", "material"):
            # 印だけで本文がなかったときも、「資料にありません」として扱う
            outcome.result = "not_in_material"
            yield delta(NOT_IN_MATERIAL_TEXT)
            yield done([])
        else:
            raise LlmError("unavailable", "empty_answer")

    except LlmError as e:
        outcome.result = "error"
        outcome.error_reason = e.reason
        outcome.total_ms = elapsed_ms()
        yield "error", {"error": e.kind, "message": ERRORS[e.kind][1]}
