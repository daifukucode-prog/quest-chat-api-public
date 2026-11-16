"""資料まとめファイル（chat-content.json）の読み込みと保持。設計書 4章。

やることは3つ。
1. ファイルを取りに行き、形を確かめる
2. 取ったものを API の中に持っておき、一定時間ごとに取り直す
3. AIに渡す「目次」を組み立て、AIが答えた通し番号からかたまりに戻す
"""

import httpx2
from pydantic import BaseModel, ConfigDict, ValidationError

# 取り直す間隔（仮：10分。設計書 4.3）
REFRESH_SEC = 600
# 取りに行って待つ長さの上限
FETCH_TIMEOUT_SEC = 10


class ContentError(Exception):
    """資料を読み込めなかった。"""


class Chunk(BaseModel):
    """かたまり：資料を話題ごとに分けた単位。AIにはこの単位で渡す。"""

    model_config = ConfigDict(frozen=True)

    id: str
    course: str
    set_id: str
    label: str  # 例：10月期 第1回
    kind: str  # summary（要点）／assignment（課題）／qa（解説）
    title: str
    pages: tuple[int, ...] = ()
    text: str
    # 解説のかたまりに入っている問題のID（「いま解いている問題」から、かたまりを探すのに使う）
    question_ids: tuple[str, ...] = ()


class Term(BaseModel):
    """用語ノートの1件。"""

    model_config = ConfigDict(frozen=True)

    term: str
    reading: str = ""
    description: str


class Content(BaseModel):
    """資料まとめファイルの中身。読み込んだあとは書き換えない。"""

    model_config = ConfigDict(frozen=True)

    version: str
    chunks: tuple[Chunk, ...]
    glossary: tuple[Term, ...] = ()

    def ordered_chunks(self) -> list[Chunk]:
        """目次に並べる順のかたまり。同じ回のものを続けて並べる。

        通し番号はこの順に 1 から振る。
        """
        first_seen: dict[str, int] = {}
        for chunk in self.chunks:
            first_seen.setdefault(chunk.label, len(first_seen))
        # sorted は同じ回の中の元の順番を保つ
        return sorted(self.chunks, key=lambda c: first_seen[c.label])

    def index_text(self) -> str:
        """AIに渡す目次。

        回ごとに見出しを1回だけ書き、その下に「通し番号 題」を並べる。
        id と回の名前を毎行に書くより短くなる（設計書 4.2）。
        """
        lines: list[str] = []
        last_label = None
        for number, chunk in enumerate(self.ordered_chunks(), start=1):
            if chunk.label != last_label:
                lines.append(f"## {chunk.label}")
                last_label = chunk.label
            lines.append(f"{number} {chunk.title}")
        return "\n".join(lines)

    def number_of_question(self, question_id: str) -> int | None:
        """その問題が入っているかたまりの、目次の通し番号。なければ None。"""
        for number, chunk in enumerate(self.ordered_chunks(), start=1):
            if question_id in chunk.question_ids:
                return number
        return None

    def numbers_of_page(self, set_id: str, page: int) -> list[int]:
        """その回の資料の、そのページを扱っているかたまりの、目次の通し番号（複数ありうる）。"""
        return [
            number
            for number, chunk in enumerate(self.ordered_chunks(), start=1)
            if chunk.set_id == set_id and page in chunk.pages
        ]

    def question_text(self, question_id: str) -> str | None:
        """その問題の文（解説のかたまりの中の「問：」の行）。分からなければ None。

        解説のかたまりの本文は、問題ごとに空行で区切られ、question_ids と同じ順に並んでいる
        （tools/build-content.mjs）。数が合わないときは、取り違えないよう None を返す。
        """
        for chunk in self.chunks:
            if question_id not in chunk.question_ids:
                continue
            blocks = chunk.text.split("\n\n")
            if len(blocks) != len(chunk.question_ids):
                return None
            first_line = blocks[chunk.question_ids.index(question_id)].split("\n")[0]
            return first_line[2:].strip() if first_line.startswith("問：") else None
        return None

    def label_of_set(self, set_id: str) -> str | None:
        """回のID（例：lecture-1）から、人が読む呼び名（例：10月期 第1回）。"""
        for chunk in self.chunks:
            if chunk.set_id == set_id:
                return chunk.label
        return None

    def chunk_by_number(self, number: int) -> Chunk | None:
        """目次の通し番号からかたまりに戻す。ない番号なら None。"""
        ordered = self.ordered_chunks()
        if 1 <= number <= len(ordered):
            return ordered[number - 1]
        return None


def parse_content(data: object) -> Content:
    """読み込んだ JSON の形を確かめて Content にする。"""
    try:
        content = Content.model_validate(data)
    except ValidationError as e:
        raise ContentError(f"資料まとめファイルの形が違います: {e.error_count()}か所") from e
    if not content.chunks:
        raise ContentError("資料まとめファイルにかたまりが1つもありません")
    ids = [c.id for c in content.chunks]
    if len(set(ids)) != len(ids):
        raise ContentError("資料まとめファイルの中で、かたまりの id が重なっています")
    return content


async def fetch_content(url: str) -> Content:
    """資料まとめファイルを取りに行く。"""
    try:
        async with httpx2.AsyncClient(timeout=FETCH_TIMEOUT_SEC) as client:
            response = await client.get(url)
            response.raise_for_status()
            data = response.json()
    except (httpx2.HTTPError, ValueError) as e:
        raise ContentError(f"資料まとめファイルを取れませんでした: {type(e).__name__}") from e
    return parse_content(data)


class ContentStore:
    """読み込んだ資料を持っておく入れ物。

    資料は全員に同じものを返すので、複数の質問で共有してよい
    （利用者ごとの値は、ここには置かない）。
    """

    def __init__(self) -> None:
        self.content: Content | None = None
        self.fetched_at: float = 0.0

    async def get(self, url: str | None, now: float) -> Content:
        """資料を返す。持っていないか、古くなっていたら取り直す。

        now は「いまの時刻」。質問を受けてから取った値を渡す
        （Workers では、関数の外で取った時刻が 0 になるため）。
        """
        if self.content is not None and now - self.fetched_at < REFRESH_SEC:
            return self.content
        if not url:
            raise ContentError("CONTENT_URL が設定されていません")
        try:
            self.content = await fetch_content(url)
        except ContentError:
            # 取り直しに失敗しても、前に取れたものがあればそれを使い続ける
            if self.content is None:
                raise
        self.fetched_at = now
        return self.content


# Worker の中で1つだけ持つ
store = ContentStore()
