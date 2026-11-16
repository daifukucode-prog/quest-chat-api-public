"""資料を選ぶ部分（retrieve.py）のテスト：用語の拾い方、AIに渡す文、番号の戻し方。"""

import asyncio

import retrieve
from content import parse_content
from fakes import FakeLlm, sample_content
from schemas import ChatContext, HistoryItem

CONTENT = sample_content()


def terms(question):
    return [t.term for t in retrieve.find_terms(question, CONTENT.glossary)]


# ---- 用語を拾う ----


def test_finds_terms_written_in_the_question():
    assert terms("ターミナルって何ですか") == ["ターミナル"]


def test_longer_term_comes_first():
    assert terms("APIキーはどこに貼りますか") == ["APIキー", "API"]


def test_ascii_term_ignores_case_but_not_inside_other_words():
    assert terms("apiって何") == ["API"]
    assert terms("CAPITAL と RAPID の違い") == []


def test_no_terms_when_nothing_matches():
    assert terms("黒い画面ってどこから開くの") == []


def test_terms_are_limited(monkeypatch):
    monkeypatch.setattr(retrieve, "MAX_TERMS", 1)
    assert terms("APIキーとターミナル") == ["ターミナル"] or len(terms("APIキーとターミナル")) == 1


# ---- AIに渡す文 ----


def test_select_prompt_puts_index_in_system_and_question_in_user():
    system, user = retrieve.build_select_prompt(CONTENT, "黒い画面って何", ChatContext(), [])
    assert CONTENT.index_text() in system
    assert "黒い画面って何" not in system  # 指示文は毎回同じ（覚えさせるため）
    assert "<question>\n黒い画面って何\n</question>" in user


def test_select_prompt_is_the_same_for_different_questions():
    a, _ = retrieve.build_select_prompt(CONTENT, "質問A", ChatContext(set_id="lecture-1"), [])
    b, _ = retrieve.build_select_prompt(CONTENT, "質問B", ChatContext(), [HistoryItem(role="user", text="x")])
    assert a == b


def test_context_is_described_in_words():
    context = ChatContext(set_id="lecture-1", screen="question", question_id="2026-10-lecture1-009")
    text = retrieve.describe_context(CONTENT, context)
    assert "いま開いている回：10月期 第1回" in text
    assert "問題を解いている画面" in text
    assert "いま解いている問題が入っている項目：2番" in text


def test_unknown_context_is_left_out():
    assert retrieve.describe_context(CONTENT, ChatContext()) == "（分かりません）"
    text = retrieve.describe_context(CONTENT, ChatContext(set_id="no-such-set", question_id="no-such-q"))
    assert text == "（分かりません）"


def test_answer_gets_the_question_text_not_the_index_number():
    # 回答のAIは目次を見ていない。通し番号を渡すと、答えに「2番」と書いてしまう
    context = ChatContext(set_id="lecture-1", screen="question", question_id="2026-10-lecture1-009")
    text = retrieve.describe_context_for_answer(CONTENT, context)
    assert "いま開いている回：10月期 第1回" in text
    assert "問題を解いている画面" in text
    assert "いま解いている問題：「ターミナルとは」" in text
    assert "番" not in text


def test_answer_context_leaves_out_unknown_question():
    assert retrieve.describe_context_for_answer(CONTENT, ChatContext()) == "（分かりません）"
    assert retrieve.describe_context_for_answer(CONTENT, ChatContext(question_id="no-such-q")) == "（分かりません）"


def test_question_text_picks_the_right_one_in_a_chunk():
    def content_with(text, ids):
        return parse_content(
            {
                "version": "v",
                "chunks": [
                    {
                        "id": "c1",
                        "course": "2026-10",
                        "set_id": "lecture-1",
                        "label": "10月期 第1回",
                        "kind": "qa",
                        "title": "解説",
                        "text": text,
                        "question_ids": ids,
                    }
                ],
            }
        )

    two = content_with("問：一つ目の問題\n答：あ\n解説：い\n\n問：二つ目の問題\n答：う\n解説：え", ["q-1", "q-2"])
    assert two.question_text("q-1") == "一つ目の問題"
    assert two.question_text("q-2") == "二つ目の問題"
    assert two.question_text("q-9") is None
    # 問題の数と本文の区切りの数が合わないときは、取り違えないよう None にする
    broken = content_with("問：一つ目の問題\n答：あ\n解説：い", ["q-1", "q-2"])
    assert broken.question_text("q-2") is None


def test_page_being_viewed_is_told_with_the_items_that_cover_it():
    # 資料選びには、そのページを扱っている項目の番号も伝える（「このページが分からない」のとき、そこから選べるように）
    text = retrieve.describe_context(CONTENT, ChatContext(set_id="lecture-1", screen="lecture", page=19))
    assert "いま見ている資料のページ：p.19（このページを扱っている項目：1番、2番）" in text
    # どのかたまりも扱っていないページは、ページだけ伝える
    assert "いま見ている資料のページ：p.99" in retrieve.describe_context(
        CONTENT, ChatContext(set_id="lecture-1", page=99)
    )
    assert "扱っている項目" not in retrieve.describe_context(CONTENT, ChatContext(set_id="lecture-1", page=99))
    # 回が分からないときは、ページだけでは意味がないので書かない
    assert retrieve.describe_context(CONTENT, ChatContext(page=19)) == "（分かりません）"
    assert retrieve.describe_context(CONTENT, ChatContext(set_id="no-such-set", page=19)) == "（分かりません）"


def test_numbers_of_page_looks_only_at_that_lecture():
    # テスト用の資料は、どのかたまりも19ページを扱っている
    assert CONTENT.numbers_of_page("lecture-1", 19) == [1, 2]
    assert CONTENT.numbers_of_page("lecture-2", 19) == [3]
    assert CONTENT.numbers_of_page("lecture-1", 35) == []


def test_history_is_described_with_who_said_it():
    history = [HistoryItem(role="user", text="CLIって何"), HistoryItem(role="assistant", text="文字で打つ画面です")]
    assert retrieve.describe_history(history) == "受講生：CLIって何\nチャット：文字で打つ画面です"
    assert retrieve.describe_history([]) == "（ありません）"


# ---- 番号をかたまりに戻す ----


def test_numbers_become_chunks_in_the_order_given():
    assert [c.id for c in retrieve.to_chunks(CONTENT, [3, 1])] == ["lecture-2-summary", "lecture-1-summary"]


def test_bad_numbers_are_dropped():
    assert [c.id for c in retrieve.to_chunks(CONTENT, [0, 99, -1, "2", 2.0, None, 2, 2])] == ["lecture-1-qa-1"]


def test_chunks_are_limited_to_four(monkeypatch):
    monkeypatch.setattr(retrieve, "MAX_CHUNKS", 2)
    assert len(retrieve.to_chunks(CONTENT, [1, 2, 3])) == 2


# ---- 選ぶ ----


def select(llm, question="黒い画面って何"):
    return asyncio.run(retrieve.select(llm, CONTENT, question, ChatContext(), []))


def test_select_returns_chunks_chosen_by_the_ai():
    result = select(FakeLlm(selection={"related": True, "numbers": [2, 1]}))
    assert result.related is True
    assert [c.id for c in result.chunks] == ["lecture-1-qa-1", "lecture-1-summary"]
    assert result.usage.input_tokens == 5000


def test_unrelated_question_gets_no_chunks_even_if_numbers_are_given():
    result = select(FakeLlm(selection={"related": False, "numbers": [1]}))
    assert result.related is False and result.chunks == []


def test_related_question_may_have_no_chunks():
    result = select(FakeLlm(selection={"related": True, "numbers": []}))
    assert result.related is True and result.chunks == []
