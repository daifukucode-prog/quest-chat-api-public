"""評価で使う、出典の点検。答えの出典の行に、選んだ資料の名前が正しく書かれているかを見る。

合格にする条件は、次の3つを全部満たすこと。

1. 「出典」で始まる行がある
2. その行に、選んだ資料の名前が1つ以上書かれている
3. その行に、選んでいない資料の名前が書かれていない

名前は、正式な名前（「ツール解説 ツール3」）と、省略形（「ツール3」）の両方を見る。
省略形は、資料の全部の中で1つしかないものだけを認める。「第1回」は10月期にも11月期にもあり、
どちらか分からないので、名前として数えない。

見ていないもの（分かっている限界）：ページ番号、題、「用語ノート」という言葉。
"""

import re

# 名前を見つけたあと、その場所を埋めておく文字（同じ場所を、別の名前として数え直さないため）
FILLER = "■"


def name_pattern(name: str) -> str:
    """name を探すための正規表現。すぐ後ろに数字が続くものは、別の名前として数えない。

    例：「ツール3」を探すとき、「ツール30」は別の名前なので、見つからないことにする。
    """
    return re.escape(name) + r"(?!\d)"


def is_written(name: str, joined: str) -> bool:
    """joined の中に name が書かれていれば True。"""
    return re.search(name_pattern(name), joined) is not None


def short_names(all_labels: list[str]) -> dict[str, str]:
    """省略形の一覧を作る。正式な名前 → 省略形（空白で区切った、最後の部分）。

    省略形が、資料の全部の中で1つしかないものだけを入れる。
    例：「ツール解説 ツール3」→「ツール3」は入る。「10月期 第1回」→「第1回」は、11月期にもあるので入らない。
    """
    labels = sorted(set(all_labels))
    tails = [label.split(" ")[-1] for label in labels]
    shorts = {}
    for label, tail in zip(labels, tails, strict=True):
        if tail != label and tails.count(tail) == 1:
            shorts[label] = tail
    return shorts


def names_of(label: str, shorts: dict[str, str]) -> list[str]:
    """1つの資料を指す名前の全部。正式な名前と、あれば省略形。長い名前を先にする。"""
    names = [label]
    if label in shorts:
        names.append(shorts[label])
    return names


def source_names_ok(all_labels: list[str], chosen: list[str], text: str) -> bool:
    """出典の行に、選んだ資料の名前が正しく書かれていれば True。

    all_labels … 資料の名前の全部（例：["10月期 第1回", "ツール解説 ツール3", …]）
    chosen     … 資料選びで選ばれた資料の名前
    text       … AIが書いた答えの全文
    """
    # 1. 「出典」で始まる行を集める
    source_lines = []
    for line in text.split("\n"):
        if line.strip().startswith("出典"):
            source_lines.append(line)

    if not source_lines:
        return False

    joined = "\n".join(source_lines)
    shorts = short_names(all_labels)

    # 2. 選んだ資料の名前が、1つ以上書かれているか。見つけた場所は埋めておく
    found = 0
    for label in chosen:
        for name in names_of(label, shorts):
            if is_written(name, joined):
                found += 1
                joined = re.sub(name_pattern(name), FILLER, joined)

    if found == 0:
        return False

    # 3. 残りに、選んでいない資料の名前が書かれていないか
    for label in set(all_labels) - set(chosen):
        for name in names_of(label, shorts):
            if is_written(name, joined):
                return False

    return True
