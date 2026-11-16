"""守りの部分（guard.py）と、送るものの形（schemas.py）のテスト。"""

import pytest
from pydantic import ValidationError

from guard import KEY_MARK, MAIL_MARK, NAME_MARK, allowed_origin, browser_hash, jst_day, redact, utc_iso
from schemas import QUESTION_MAX_CHARS, ChatRequest

BROWSER = "b1946ac9-2f0c-4c7e-9d1a-000000000001"


# ---- 伏せ字 ----


@pytest.mark.parametrize(
    "secret",
    [
        "sk-ant-api03-AbCdEfGhIjKlMnOpQrStUvWxYz0123456789",
        "sk-proj-AbCdEfGhIjKlMnOpQrStUvWx",
        "ghp_AbCdEfGhIjKlMnOpQrStUvWxYz012345",
        "github_pat_11ABCDEFG0abcdefghijkl_mnopqrstuv",
        "AKIAIOSFODNN7EXAMPLE",
        "AIzaSyA-abcdefghijklmnopqrstuvwxyz01234",
        "xoxb-123456789012-abcdefghij",
        "eyJhbGciOiJIUzI1NiJ9.eyJzdWIiOiIxMjM0NTY3ODkwIn0.dozjgNryP4J3jVmNHl0w5N",
        "9f86d081884c7d659a2feaa0c55ad015a3bf4f1b2b0b822cd15d6c15b0f00a08",
    ],
)
def test_redacts_things_that_look_like_keys(secret):
    out = redact(f"エラーが出ました {secret} どうすればいいですか")
    assert secret not in out
    assert KEY_MARK in out
    assert out.startswith("エラーが出ました ") and out.endswith(" どうすればいいですか")


@pytest.mark.parametrize(
    "line, hidden",
    [
        ("ANTHROPIC_API_KEY=abc123xyz", "abc123xyz"),
        ('api_key: "short-but-secret"', "short-but-secret"),
        ("password: hunter2hunter", "hunter2hunter"),
        ("パスワード：himitsu1234", "himitsu1234"),
        ("export GITHUB_TOKEN='tok_abcdef'", "tok_abcdef"),
    ],
)
def test_redacts_value_when_the_name_says_it_is_secret(line, hidden):
    out = redact(line)
    assert hidden not in out
    assert KEY_MARK in out
    assert out.split("=")[0].split(":")[0].split("：")[0] in line  # 名前のほうは残す


def test_redacts_email():
    out = redact("taro.yamada+test@example.co.jp にメールが届きません")
    assert out == f"{MAIL_MARK} にメールが届きません"


@pytest.mark.parametrize(
    "text, expected",
    [
        ("/Users/taro/projects/app が見つからない", f"/Users/{NAME_MARK}/projects/app が見つからない"),
        ("cd /home/hanako/work", f"cd /home/{NAME_MARK}/work"),
        (r"C:\Users\Taro\Desktop\memo.txt", rf"C:\Users\{NAME_MARK}\Desktop\memo.txt"),
    ],
)
def test_redacts_user_name_in_paths(text, expected):
    assert redact(text) == expected


@pytest.mark.parametrize(
    "text",
    [
        "ターミナルって何ですか？どこから開けばいいですか",
        "CLAUDE.md と Skills はどう使い分けますか",
        "zsh: command not found: claude と出ました",
        "Error: Cannot find module 'next' at Function.Module._resolveFilename (node:internal/modules/cjs/loader:1039:15)",
        'git commit -m "first" したら fatal: not a git repository と言われた',
        "10月期 第2回 p.19 の手順3が分かりません",
        "https://quest-app.example.com/ を開いています",
        "npm ERR! code ENOENT npm ERR! syscall open",
    ],
)
def test_ordinary_questions_are_not_changed(text):
    assert redact(text) == text


def test_redacts_several_things_in_one_question():
    out = redact(
        "/Users/taro/app で ANTHROPIC_API_KEY=sk-ant-api03-AbCdEfGhIjKlMnOpQrSt を使い、taro@example.com に送る"
    )
    assert "taro" not in out and "sk-ant" not in out and "example.com" not in out
    assert out.count(KEY_MARK) == 1 and MAIL_MARK in out and NAME_MARK in out


# ---- 呼び出し元 ----

ALLOWED = "https://quest-app.example.com, http://localhost:4173"


@pytest.mark.parametrize("origin", ["https://quest-app.example.com", "http://localhost:4173"])
def test_allowed_origin_passes(origin):
    assert allowed_origin(origin, ALLOWED) == origin


@pytest.mark.parametrize(
    "origin",
    [
        None,
        "",
        "https://evil.example",
        "https://quest-app.example.com.evil.example",
        "http://quest-app.example.com",
        "http://localhost:9999",
    ],
)
def test_other_origin_is_refused(origin):
    assert allowed_origin(origin, ALLOWED) is None


def test_nothing_is_allowed_when_setting_is_empty():
    assert allowed_origin("http://localhost:4173", None) is None
    assert allowed_origin("http://localhost:4173", "") is None


# ---- 日付とハッシュ ----


def test_day_is_counted_in_japan_time():
    # 2026-11-15 15:00 UTC は、日本では 11-16 の 0:00
    assert jst_day(1794754800 - 1) == "2026-11-15"
    assert jst_day(1794754800) == "2026-11-16"
    assert utc_iso(1794754800) == "2026-11-15T15:00:00Z"


def test_browser_hash_hides_the_id_and_changes_every_day():
    a = browser_hash(BROWSER, "2026-11-16")
    assert BROWSER not in a and len(a) == 64
    assert a == browser_hash(BROWSER, "2026-11-16")
    assert a != browser_hash(BROWSER, "2026-11-17")
    assert a != browser_hash(BROWSER + "x", "2026-11-16")


# ---- 送るものの形 ----


def test_question_is_trimmed_and_limited():
    assert ChatRequest(question="  ターミナルとは  ", browser_id=BROWSER).question == "ターミナルとは"
    ChatRequest(question="あ" * QUESTION_MAX_CHARS, browser_id=BROWSER)
    for bad in ["", "   ", "あ" * (QUESTION_MAX_CHARS + 1)]:
        with pytest.raises(ValidationError):
            ChatRequest(question=bad, browser_id=BROWSER)


def test_context_is_optional_but_screen_must_be_known():
    assert ChatRequest(question="q", browser_id=BROWSER).context.screen is None
    ChatRequest(question="q", browser_id=BROWSER, context={"screen": "question", "set_id": "lecture-1"})
    with pytest.raises(ValidationError):
        ChatRequest(question="q", browser_id=BROWSER, context={"screen": "admin"})


def test_history_is_at_most_three_round_trips():
    item = {"role": "user", "text": "CLIって何ですか"}
    ChatRequest(question="q", browser_id=BROWSER, history=[item] * 6)
    with pytest.raises(ValidationError):
        ChatRequest(question="q", browser_id=BROWSER, history=[item] * 7)
    with pytest.raises(ValidationError):
        ChatRequest(question="q", browser_id=BROWSER, history=[{"role": "system", "text": "x"}])


@pytest.mark.parametrize("bad", ["", "short", "has space in it", "x" * 65, "<script>alert(1)</script>"])
def test_browser_id_must_look_like_an_id(bad):
    with pytest.raises(ValidationError):
        ChatRequest(question="q", browser_id=bad)
