"""質問から答えまでの流れ（chat.py）のテスト。AIは偽物。"""

import asyncio

from answer import (
    COMMUNITY_TEXT,
    GENERAL_HEAD,
    GENERAL_LABEL,
    GENERAL_MARK,
    GENERAL_TAIL,
    NO_MATERIAL_MARK,
    NOT_IN_MATERIAL_TEXT,
    OUT_OF_SCOPE_TEXT,
    SUPPLEMENT_MARK,
)
from chat import Outcome, respond, split_tail
from fakes import FakeLlm, broken, busy, sample_content
from schemas import ChatContext, HistoryItem

CONTENT = sample_content()


def run(llm, question="黒い画面って何", context=None, history=None):
    outcome = Outcome()
    ticks = iter(range(0, 100000, 1))  # 呼ぶたびに1秒進む時計

    async def collect():
        return [
            e
            async for e in respond(
                llm, CONTENT, question, context or ChatContext(), history or [], "log-1", outcome, lambda: next(ticks)
            )
        ]

    return asyncio.run(collect()), outcome


def text_of(events):
    return "".join(data["text"] for kind, data in events if kind == "delta")


def kinds(events):
    return [kind for kind, _ in events]


# ---- 答えられたとき ----


def test_answer_arrives_in_order_with_sources():
    events, outcome = run(FakeLlm())
    assert kinds(events) == ["status", "status", "delta", "delta", "done"]
    assert events[0][1] == {"state": "searching"} and events[1][1] == {"state": "answering"}
    assert text_of(events) == "文字で命令を打つ画面です。\n出典：10月期 第1回 p.35"
    done = events[-1][1]
    assert done["result"] == "answered" and done["log_id"] == "log-1"
    assert done["sources"] == [
        {
            "id": "lecture-1-qa-1",
            "label": "10月期 第1回",
            "title": "解説：操作の基礎（ターミナル）",
            "kind": "qa",
            "pages": [19],
        }
    ]


def test_outcome_has_what_the_log_needs():
    _, outcome = run(FakeLlm())
    assert outcome.result == "answered"
    assert outcome.chunk_ids == ["lecture-1-qa-1"]
    assert (outcome.usage.input_tokens, outcome.usage.output_tokens) == (5800, 140)  # 2回の呼び出しの合計
    assert outcome.first_ms is not None and outcome.total_ms >= outcome.first_ms


def test_ai_gets_the_chosen_material_terms_screen_and_history():
    llm = FakeLlm()
    run(
        llm,
        question="ターミナルって何",
        context=ChatContext(set_id="lecture-1"),
        history=[HistoryItem(role="user", text="CLIって何")],
    )
    sent = llm.sent("answer")
    assert "黒い画面のこと。（p.35）" in sent  # 選ばれたかたまりの本文
    assert "毎回読むルールはCLAUDE.md" not in sent  # 選ばれなかったかたまりは渡さない
    assert "・ターミナル：文字で命令を打つ画面。" in sent  # 文字の一致で拾った用語
    assert "いま開いている回：10月期 第1回" in sent
    assert "受講生：CLIって何" in sent
    assert "<question>\nターミナルって何\n</question>" in sent


def test_writer_gets_the_question_being_solved_without_index_number():
    llm = FakeLlm()
    run(
        llm,
        question="この問題の解説をやさしく",
        context=ChatContext(screen="question", question_id="2026-10-lecture1-009"),
    )
    assert "いま解いている問題が入っている項目：2番" in llm.sent("select")  # 資料選びには、目次の番号で伝える
    sent = llm.sent("answer")
    assert "いま解いている問題：「ターミナルとは」" in sent  # 回答には、問題の文で伝える
    assert "入っている項目" not in sent


def test_writer_gets_the_page_being_viewed():
    llm = FakeLlm()
    run(llm, question="このページがわからない", context=ChatContext(set_id="lecture-1", screen="lecture", page=19))
    assert "いま見ている資料のページ：p.19（このページを扱っている項目：1番、2番）" in llm.sent("select")
    sent = llm.sent("answer")
    assert "いま見ている資料のページ：p.19" in sent and "扱っている項目" not in sent


def test_glossary_alone_is_enough_to_answer():
    events, outcome = run(FakeLlm(selection={"related": True, "numbers": []}), question="APIって何")
    assert outcome.result == "answered" and outcome.chunk_ids == []
    assert events[-1][1]["sources"] == []


# ---- 答えないとき ----


def test_unrelated_question_gets_fixed_guide_without_calling_the_writer():
    llm = FakeLlm(selection={"related": False, "numbers": []})
    events, outcome = run(llm, question="今日の天気は")
    assert kinds(events) == ["status", "delta", "done"]
    assert text_of(events) == OUT_OF_SCOPE_TEXT
    assert outcome.result == "out_of_scope"
    assert [k for k, *_ in llm.calls] == ["select"]  # 答えを書く呼び出しはしない


def test_nothing_found_still_asks_the_writer_who_may_say_no_material():
    # 読む資料が見つからなくても、答えを書くAIを呼ぶ（一般的な説明ができるかを判断させるため）
    llm = FakeLlm(selection={"related": True, "numbers": []}, pieces=[NO_MATERIAL_MARK])
    events, outcome = run(llm, question="講義の録画はいつまで見られますか")
    assert text_of(events) == NOT_IN_MATERIAL_TEXT
    assert outcome.result == "not_in_material" and outcome.chunk_ids == []
    assert [k for k, *_ in llm.calls] == ["select", "answer"]
    assert "<materials>\n（ありません）\n</materials>" in llm.sent("answer")


def test_community_question_gets_the_guide_to_ask_in_the_group():
    # 「.AI」のコミュニティについての質問は、答えを書くAIを呼ばずに、決まった案内文を返す
    llm = FakeLlm(selection={"related": True, "community": True, "numbers": [2]})
    events, outcome = run(llm, question=".AIのコミュニティにはどうやって入れますか")
    assert text_of(events) == COMMUNITY_TEXT and "グループで直接質問" in COMMUNITY_TEXT
    assert outcome.result == "community" and outcome.chunk_ids == []
    assert events[-1][1]["result"] == "community" and events[-1][1]["sources"] == []
    assert [k for k, *_ in llm.calls] == ["select"]


def test_community_flag_wins_even_if_marked_unrelated():
    events, outcome = run(FakeLlm(selection={"related": False, "community": True, "numbers": []}))
    assert text_of(events) == COMMUNITY_TEXT and outcome.result == "community"


# ---- 資料にないが、一般的な説明をするとき ----


def test_general_answer_gets_the_label_first_and_the_guide_last():
    llm = FakeLlm(
        selection={"related": True, "numbers": []},
        pieces=[GENERAL_MARK, "\n", "Docker は、", "アプリを箱に入れて動かす道具です。"],
    )
    events, outcome = run(llm, question="Docker って何")
    assert text_of(events) == GENERAL_HEAD + "\n\nDocker は、アプリを箱に入れて動かす道具です。\n\n" + GENERAL_TAIL
    assert GENERAL_MARK not in text_of(events)  # 印は見せない
    assert outcome.result == "general"
    assert events[-1][1]["result"] == "general" and events[-1][1]["sources"] == []  # 出典は付けない


def test_general_mark_split_across_pieces_is_still_found():
    for pieces in (
        ["【資料", "外】本文です。"],
        ["\n【", "資料外", "】\n\n", "本文", "です。"],
        ["【資料外】本文です。"],
    ):
        events, outcome = run(FakeLlm(pieces=pieces))
        assert text_of(events) == GENERAL_HEAD + "\n\n本文です。\n\n" + GENERAL_TAIL, pieces
        assert outcome.result == "general"


def test_general_answer_can_follow_chosen_material():
    # 資料は選ばれたが、読んでみたら答えがなかったとき。出典は付けず、選ばれたかたまりは記録に残す
    events, outcome = run(FakeLlm(pieces=[GENERAL_MARK + "\n一般的な説明です。"]))
    assert outcome.result == "general" and outcome.chunk_ids == ["lecture-1-qa-1"]
    assert events[-1][1]["sources"] == []


def test_general_mark_without_any_body_is_treated_as_no_material():
    for pieces in ([GENERAL_MARK], [GENERAL_MARK, "\n", "  "]):
        events, outcome = run(FakeLlm(pieces=pieces))
        assert text_of(events) == NOT_IN_MATERIAL_TEXT, pieces
        assert outcome.result == "not_in_material"


def test_mark_from_the_writer_is_replaced_and_never_shown():
    for pieces in ([NO_MATERIAL_MARK], ["【資料", "なし】"], ["\n", "【", "資料なし】", "余計な文"]):
        events, outcome = run(FakeLlm(pieces=pieces))
        assert text_of(events) == NOT_IN_MATERIAL_TEXT, pieces
        assert outcome.result == "not_in_material"
        assert events[-1][1]["sources"] == []


def test_answer_that_only_looks_like_the_mark_at_first_is_shown_whole():
    events, outcome = run(FakeLlm(pieces=["【", "資料", "の場所】は第1回です。"]))
    assert text_of(events) == "【資料の場所】は第1回です。"
    assert outcome.result == "answered"


def test_very_short_answer_is_still_shown():
    events, outcome = run(FakeLlm(pieces=["【"]))
    assert text_of(events) == "【" and outcome.result == "answered"


# ---- 資料が質問の一部にしか答えていないとき（補足の係を呼ぶ） ----

MAIN = "Vercel は、URL ができる置き場です。ほかの置き場は、資料には書かれていません。\n出典：ツール4 p.2"
ADDED = "ほかには Firebase などがあります。"


def test_end_mark_calls_the_supplement_writer_and_adds_a_labeled_part():
    llm = FakeLlm(
        pieces=[
            "Vercel は、URL ができる置き場です。",
            "ほかの置き場は、資料には書かれていません。",
            "\n出典：ツール4 p.2",
            "\n\n" + SUPPLEMENT_MARK,
        ]
    )
    events, outcome = run(llm, question="ほかのデプロイ先は？")
    assert text_of(events) == MAIN + "\n\n" + GENERAL_LABEL + "\n\n" + ADDED + "\n\n" + GENERAL_TAIL
    assert SUPPLEMENT_MARK not in text_of(events)  # 印は見せない
    assert outcome.result == "answered"  # 資料からの答えがあるので、出典は付く
    assert [s["id"] for s in events[-1][1]["sources"]] == ["lecture-1-qa-1"]
    assert [k for k, *_ in llm.calls] == ["select", "answer", "supplement"]
    sent = llm.sent("supplement")
    assert "<question>\nほかのデプロイ先は？\n</question>" in sent
    assert "<answered>\n" + MAIN + "\n</answered>" in sent  # すでに見せた答えを渡す（印は渡さない）
    assert "<materials>" not in sent  # 資料の本文は渡さない
    # 使った量は、3回の呼び出しの合計
    assert (outcome.usage.input_tokens, outcome.usage.output_tokens) == (6100, 220)


def test_end_mark_is_found_however_it_is_split():
    for pieces in (
        [MAIN + "\n" + SUPPLEMENT_MARK],
        [MAIN, "\n", "【", "資料外", "あり】", "\n"],
        [MAIN + "\n【資料外あ", "り】  "],
    ):
        events, outcome = run(FakeLlm(pieces=pieces))
        assert text_of(events) == MAIN + "\n\n" + GENERAL_LABEL + "\n\n" + ADDED + "\n\n" + GENERAL_TAIL, pieces


def test_end_mark_is_ignored_when_the_answer_does_not_say_something_is_missing():
    # 資料だけで答えられたのに、AIが印を書いてしまったとき。印は見せず、補足も呼ばない
    llm = FakeLlm(pieces=["ターミナルは、文字で命令を打つ画面です。\n出典：ツール1 p.6", "\n" + SUPPLEMENT_MARK])
    events, outcome = run(llm)
    assert text_of(events) == "ターミナルは、文字で命令を打つ画面です。\n出典：ツール1 p.6"
    assert [k for k, *_ in llm.calls] == ["select", "answer"] and outcome.result == "answered"


def test_no_end_mark_means_no_supplement_call():
    llm = FakeLlm()
    events, outcome = run(llm)
    assert [k for k, *_ in llm.calls] == ["select", "answer"]
    assert GENERAL_LABEL not in text_of(events)


def test_brackets_inside_the_answer_are_shown_as_they_are():
    # 「【」が出てきても、最後の印でなければ、そのまま見せる
    events, outcome = run(FakeLlm(pieces=["手順は", "【注意】", "を読んでから。", "【資料", "の場所】は第1回です。"]))
    assert text_of(events) == "手順は【注意】を読んでから。【資料の場所】は第1回です。"
    assert outcome.result == "answered"


def test_supplement_writer_may_decline_and_then_nothing_is_added():
    llm = FakeLlm(pieces=[MAIN, "\n" + SUPPLEMENT_MARK], supplement_pieces=["【資料", "なし】"])
    events, outcome = run(llm)
    assert text_of(events) == MAIN and outcome.result == "answered"
    assert kinds(events)[-1] == "done"


def test_supplement_failure_keeps_the_answer_from_the_material():
    # 資料からの答えは出せているので、補足が失敗しても、失敗の知らせにはしない
    llm = FakeLlm(pieces=[MAIN, "\n" + SUPPLEMENT_MARK], supplement_pieces=[], supplement_error=busy())
    events, outcome = run(llm)
    assert text_of(events) == MAIN and outcome.result == "answered"
    assert kinds(events)[-1] == "done" and "error" not in kinds(events)
    assert outcome.error_reason == "supplement_rate_limit"


def test_only_the_end_mark_becomes_a_general_answer():
    # 資料からの答えを1文字も書かずに、補足だけを頼んだとき。答えの全部が一般的な説明になる
    events, outcome = run(FakeLlm(pieces=[SUPPLEMENT_MARK]))
    assert text_of(events) == GENERAL_HEAD + "\n\n" + ADDED + "\n\n" + GENERAL_TAIL
    assert outcome.result == "general" and events[-1][1]["sources"] == []


def test_split_tail_holds_back_only_what_could_be_the_end_mark():
    assert split_tail("答えです。") == ("答えです。", "")
    assert split_tail("答えです。\n\n") == ("答えです。", "\n\n")  # 終わりの空白は持っておく
    assert split_tail("答えです。\n【資料外") == ("答えです。", "\n【資料外")  # 印の書きかけ
    assert split_tail("答えです。\n" + SUPPLEMENT_MARK + "\n") == ("答えです。", "\n" + SUPPLEMENT_MARK + "\n")
    assert split_tail("【資料の場所】は") == ("【資料の場所】は", "")  # 印ではない


# ---- うまくいかないとき ----


def test_failure_while_choosing_gives_error_event():
    events, outcome = run(FakeLlm(select_error=broken()))
    assert kinds(events) == ["status", "error"]
    assert events[-1][1]["error"] == "unavailable"
    assert outcome.result == "error" and outcome.error_reason == "connection"


def test_busy_gives_busy_message():
    events, _ = run(FakeLlm(select_error=busy()))
    assert events[-1][1] == {"error": "busy", "message": "いま混み合っています。時間をおいて試してください"}


def test_failure_in_the_middle_of_the_answer_ends_with_error_not_done():
    events, outcome = run(FakeLlm(answer_error=broken()))
    assert kinds(events)[-1] == "error" and "done" not in kinds(events)
    assert outcome.result == "error"


def test_empty_answer_is_an_error():
    events, outcome = run(FakeLlm(pieces=[]))
    assert kinds(events)[-1] == "error" and outcome.error_reason == "empty_answer"
