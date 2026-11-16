"""守りの部分：断り方、呼び出し元の確認、伏せ字、回数制限。設計書 5.4・7章。

AIは使わない。
"""

import hashlib
import re
from datetime import UTC, datetime, timedelta, timezone

import store

# 1つのブラウザの1日の上限（本人が決定。10問から引き上げた。判断の記録 D-16・D-13 の追記）
BROWSER_DAILY_LIMIT = 15
# 全体の1日の上限（150問から引き上げた。数は仮。料金を測った結果から決めた。判断の記録 D-13 の追記）
GLOBAL_DAILY_LIMIT = 300

JST = timezone(timedelta(hours=9))


# ---- 断り方（設計書 5.4） ----

# 種類 → （状態コード, 画面に出す文言）
ERRORS = {
    "invalid_input": (400, "質問が長すぎます。短くして送ってください"),
    "forbidden_origin": (403, "このサイトからは使えません"),
    "browser_limit": (429, "今日の質問回数の上限に達しました。明日また使えます"),
    "global_limit": (429, "いま混み合っています。時間をおいて試してください"),
    # Claude API の回数上限に当たったとき（答えを流している途中の知らせとして返す）
    "busy": (429, "いま混み合っています。時間をおいて試してください"),
    "maintenance": (503, "いまメンテナンス中です"),
    "unavailable": (503, "いまは答えられません。時間をおいて試してください"),
    "not_found": (404, "見つかりませんでした"),
}


class ApiError(Exception):
    """断るときに投げる。app.py が状態コードと文言に変えて返す。"""

    def __init__(self, kind: str) -> None:
        super().__init__(kind)
        self.kind = kind
        self.status, self.message = ERRORS[kind]


# ---- 呼び出し元の確認 ----


def allowed_origin(origin: str | None, allowed: str | None) -> str | None:
    """呼び出し元が許可したサイトなら、そのURLを返す。違えば None。

    allowed は ALLOWED_ORIGINS の値で、URLをカンマで区切って並べたもの。
    ブラウザ以外からは偽れるので、これだけに頼らず、回数制限と上限を主な守りにする。
    """
    if not origin or not allowed:
        return None
    sites = {site.strip().rstrip("/") for site in allowed.split(",") if site.strip()}
    return origin if origin.rstrip("/") in sites else None


# ---- 伏せ字 ----

KEY_MARK = "[伏せ字:キー]"
MAIL_MARK = "[伏せ字:メール]"
NAME_MARK = "[伏せ字:名前]"

_OUTSIDE = r"(?<![A-Za-z0-9_-])"  # 前が英数字でない
_END = r"(?![A-Za-z0-9_-])"  # 後ろが英数字でない

# 1. 「API_KEY=xxxx」「password: xxxx」のように、名前で秘密だと分かるもの。値だけを伏せる
_NAMED_SECRET = re.compile(
    r"(?i)((?:[A-Z0-9_]*(?:API[_-]?KEY|TOKEN|SECRET|PASSWORD|PASSWD)[A-Z0-9_]*|パスワード|APIキー)"
    r"\s*[=:：]\s*['\"]?)([^\s'\"]{6,})"
)
# 2. 形で分かるキー（Claude・OpenAI・GitHub・AWS・Google・Slack・JWT）
_KNOWN_KEY = re.compile(
    _OUTSIDE + r"(?:sk-[A-Za-z0-9_-]{20,}"
    r"|gh[pousr]_[A-Za-z0-9]{20,}"
    r"|github_pat_[A-Za-z0-9_]{20,}"
    r"|AKIA[0-9A-Z]{16}"
    r"|AIza[0-9A-Za-z_-]{30,}"
    r"|xox[abprs]-[A-Za-z0-9-]{10,}"
    r"|eyJ[A-Za-z0-9_-]{10,}\.[A-Za-z0-9_-]{10,}\.[A-Za-z0-9_-]{10,})"
)
# 3. 英字と数字が混ざった32文字以上の続き（種類は分からないが、キーらしいもの）
_LONG_RANDOM = re.compile(_OUTSIDE + r"(?=[A-Za-z0-9_-]*[0-9])(?=[A-Za-z0-9_-]*[A-Za-z])[A-Za-z0-9_-]{32,}" + _END)
_EMAIL = re.compile(r"[A-Za-z0-9._%+-]+@[A-Za-z0-9-]+(?:\.[A-Za-z0-9-]+)+")
# /Users/名前/ ・ /home/名前/ ・ C:\Users\名前\ の名前部分
_HOME_UNIX = re.compile(r"(/(?:Users|home)/)([^/\s]+)")
_HOME_WINDOWS = re.compile(r"([A-Za-z]:\\Users\\)([^\\\s]+)")


def redact(text: str) -> str:
    """APIキーらしき文字、メールアドレス、PCのユーザー名を伏せ字にする。

    AIに送る前と、記録する前に通す（要件 F-24）。
    """
    text = _NAMED_SECRET.sub(lambda m: m.group(1) + KEY_MARK, text)
    text = _KNOWN_KEY.sub(KEY_MARK, text)
    text = _EMAIL.sub(MAIL_MARK, text)
    text = _HOME_UNIX.sub(lambda m: m.group(1) + NAME_MARK, text)
    text = _HOME_WINDOWS.sub(lambda m: m.group(1) + NAME_MARK, text)
    text = _LONG_RANDOM.sub(KEY_MARK, text)
    return text


# ---- 回数制限 ----


def jst_day(now: float) -> str:
    """日本時間の日付（例：2026-11-16）。回数は日本時間の1日で数える。"""
    return datetime.fromtimestamp(now, JST).strftime("%Y-%m-%d")


def utc_iso(now: float) -> str:
    """UTCの日時（例：2026-11-16T03:04:05Z）。記録に使う。"""
    return datetime.fromtimestamp(now, UTC).strftime("%Y-%m-%dT%H:%M:%SZ")


def browser_hash(browser_id: str, day: str) -> str:
    """ブラウザのIDを、元に戻せない形に変える。

    日付を混ぜるので、同じブラウザでも日が変わると別の値になる
    （日をまたいで同じブラウザを追えない）。
    """
    return hashlib.sha256(f"{day}:{browser_id}".encode()).hexdigest()


async def count_question(db: store.Database, browser_id: str, now: float) -> None:
    """質問1つを数える。上限を超えていたら ApiError を投げる。

    先に足してから比べる（「読んでから書く」にすると、同時の質問で数がずれるため）。
    """
    day = jst_day(now)
    used = await store.add_browser_usage(db, day, browser_hash(browser_id, day))
    if used > BROWSER_DAILY_LIMIT:
        raise ApiError("browser_limit")
    total = await store.add_daily_usage(db, day)
    if total == 1:
        # その日の最初の質問のときに、前日以前のブラウザごとの回数を消す
        await store.delete_old_browser_usage(db, day)
    if total > GLOBAL_DAILY_LIMIT:
        raise ApiError("global_limit")
