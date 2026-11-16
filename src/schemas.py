"""送るもの・返すものの形。設計書 5章。

形が違うものは、FastAPI がここで止める（app.py で 400 にして返す）。
"""

from typing import Literal

from pydantic import BaseModel, Field, field_validator

# 質問の長さの上限（仮：2,000字。要件 F-43）
QUESTION_MAX_CHARS = 2000
# 直前のやりとりは最大3往復＝6件（要件 F-14）
HISTORY_MAX_ITEMS = 6
# 直前のやりとり1件の長さの上限（仮）
HISTORY_TEXT_MAX_CHARS = 4000

Screen = Literal["home", "lecture", "question", "result", "glossary"]


class ChatContext(BaseModel):
    """いまの画面。どれも任意（組み込み先から呼ぶときに無くても動くように）。"""

    course: str | None = Field(default=None, max_length=64)
    set_id: str | None = Field(default=None, max_length=64)
    screen: Screen | None = None
    question_id: str | None = Field(default=None, max_length=64)
    # 講義の画面で見ている資料のページ（1から）。「このページが分からない」に答えるために使う
    page: int | None = Field(default=None, ge=1, le=2000)


class HistoryItem(BaseModel):
    role: Literal["user", "assistant"]
    text: str = Field(min_length=1, max_length=HISTORY_TEXT_MAX_CHARS)


class ChatRequest(BaseModel):
    """POST /api/chat で送るもの。"""

    question: str
    # ブラウザ内に保存したランダムなID。回数を数えるためだけに使い、質問の記録には保存しない
    browser_id: str = Field(pattern=r"^[A-Za-z0-9_-]{8,64}$")
    context: ChatContext = Field(default_factory=ChatContext)
    history: list[HistoryItem] = Field(default_factory=list, max_length=HISTORY_MAX_ITEMS)

    @field_validator("question")
    @classmethod
    def question_length(cls, value: str) -> str:
        value = value.strip()
        if not value:
            raise ValueError("質問が空です")
        if len(value) > QUESTION_MAX_CHARS:
            raise ValueError("質問が長すぎます")
        return value


class FeedbackRequest(BaseModel):
    """POST /api/chat/feedback で送るもの。"""

    log_id: str = Field(pattern=r"^[0-9a-f-]{36}$")
    helpful: bool
