"""出典の点検（tests/eval/source_check.py）のテスト。"""

from source_check import short_names, source_names_ok

# 資料の名前の全部（いまの資料まとめファイルと同じ17個）
LABELS = [
    *[f"10月期 第{n}回" for n in range(0, 9)],
    *[f"11月期 第{n}回" for n in range(1, 4)],
    *[f"ツール解説 ツール{n}" for n in range(1, 6)],
]


def test_full_name_passes():
    # 表の1番：正式な名前が書いてある
    text = "コミットは、変更を手元に記録することです。\n\n出典：ツール解説 ツール3 p.10"
    assert source_names_ok(LABELS, ["ツール解説 ツール3"], text) is True


def test_empty_source_line_fails():
    # 表の2番：名前が1つもない
    text = "コミットは、変更を手元に記録することです。\n\n出典："
    assert source_names_ok(LABELS, ["ツール解説 ツール3"], text) is False


def test_short_name_passes_when_it_is_unique():
    # 表の3番：省略形。「ツール3」は、全部の資料の中で1つしかない
    text = "コミットは、変更を手元に記録することです。\n\n出典：ツール3 p.10"
    assert source_names_ok(LABELS, ["ツール解説 ツール3"], text) is True


def test_short_name_followed_by_a_digit_is_another_name():
    # 表の4番：「ツール3」のあとに数字が続くので、別の名前（ツール30）
    text = "コミットは、変更を手元に記録することです。\n\n出典：ツール30 p.10"
    assert source_names_ok(LABELS, ["ツール解説 ツール3"], text) is False


def test_short_name_fails_when_it_is_not_unique():
    # 表の5番：「第1回」は10月期にも11月期にもあり、どちらか分からない
    text = "入り口は3つあります。\n\n出典：第1回 p.35"
    assert source_names_ok(LABELS, ["10月期 第1回"], text) is False


def test_same_number_in_another_term_fails():
    # 表の6番：期が違う。選んでいない資料の名前
    text = "Webhook は、合図で動く仕組みです。\n\n出典：10月期 第1回 p.5"
    assert source_names_ok(LABELS, ["11月期 第1回"], text) is False


def test_full_name_followed_by_a_digit_is_another_name():
    # 表の7番：「第1回」と「第10回」を取り違えない
    text = "入り口は3つあります。\n\n出典：10月期 第10回 p.3"
    assert source_names_ok(LABELS, ["10月期 第1回"], text) is False


def test_one_of_two_chosen_is_enough():
    # 表の8番：選んだ資料のうち、1つ書いてあればよい
    text = "鍵は作り直します。\n\n出典：11月期 第2回 p.24"
    assert source_names_ok(LABELS, ["ツール解説 ツール3", "11月期 第2回"], text) is True


def test_two_source_lines_both_chosen_pass():
    # 表の9番：出典の行が2つあっても、どちらも選んだ資料
    text = "鍵は作り直します。\n\n出典：ツール解説 ツール3 p.17\n出典：11月期 第2回 p.29"
    assert source_names_ok(LABELS, ["ツール解説 ツール3", "11月期 第2回"], text) is True


def test_name_not_chosen_mixed_in_fails():
    # 表の10番：選んでいない資料が混ざっている
    text = "鍵は作り直します。\n\n出典：ツール解説 ツール3 p.10、11月期 第2回 p.24"
    assert source_names_ok(LABELS, ["ツール解説 ツール3"], text) is False


def test_short_name_not_chosen_mixed_in_fails():
    # 表の11番：選んでいない資料を、省略形で書いている
    text = "鍵は作り直します。\n\n出典：11月期 第2回 p.24、ツール3 p.10"
    assert source_names_ok(LABELS, ["11月期 第2回"], text) is False


def test_title_and_glossary_are_not_looked_at():
    # 表の12番：題や「用語ノート」は、今回は見ない（分かっている限界）
    text = "コミットは、変更を手元に記録することです。\n\n出典：ツール3 解説（コミットとプッシュ）p.10、用語ノート"
    assert source_names_ok(LABELS, ["ツール解説 ツール3"], text) is True


def test_unknown_words_only_fails():
    # 表の13番：選んだ資料の名前が1つもない
    text = "コミットは、変更を手元に記録することです。\n\n出典：あいうえお"
    assert source_names_ok(LABELS, ["ツール解説 ツール3"], text) is False


def test_no_source_line_fails():
    # 表の14番：「出典」で始まる行がない（本文の中に「出典」という文字があるだけ）
    text = "コミットは、変更を手元に記録することです。出典は資料にあります。"
    assert source_names_ok(LABELS, ["ツール解説 ツール3"], text) is False


def test_full_tool_name_followed_by_a_digit_is_another_name():
    # 表の15番：「ツール解説 ツール3」のあとに数字が続くので、別の名前（ツール30）
    text = "コミットは、変更を手元に記録することです。\n\n出典：ツール解説 ツール30 p.10"
    assert source_names_ok(LABELS, ["ツール解説 ツール3"], text) is False


def test_short_names_keeps_only_unique_ones():
    # 省略形の一覧：1つしかないものだけが入る。「第1回」は10月期にも11月期にもあるので入らない
    shorts = short_names(LABELS)
    assert shorts["ツール解説 ツール3"] == "ツール3"
    assert shorts["10月期 第0回"] == "第0回"
    assert "10月期 第1回" not in shorts
    assert "11月期 第1回" not in shorts


def test_same_number_in_another_term_is_not_mixed_up():
    # 「11月期 第1回」と書いてあるとき、「10月期 第1回」が混ざっているとは数えない
    text = "Webhook は、合図で動く仕組みです。\n\n出典：11月期 第1回 p.5"
    assert source_names_ok(LABELS, ["11月期 第1回"], text) is True


def test_nothing_chosen_fails():
    # 選んだ資料がないのに出典が書いてあるのは、おかしい
    text = "コミットは、変更を手元に記録することです。\n\n出典：ツール解説 ツール3 p.10"
    assert source_names_ok(LABELS, [], text) is False
