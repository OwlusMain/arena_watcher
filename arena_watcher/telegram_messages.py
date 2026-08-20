from __future__ import annotations

import html
import re
from dataclasses import dataclass


TELEGRAM_TEXT_LIMIT = 4096

_HTML_TOKEN_RE = re.compile(
    r"(<(?:/?[A-Za-z][^<>]*|![^<>]*)>|&(?:#[0-9]+|#x[0-9A-Fa-f]+|[A-Za-z][A-Za-z0-9]+);)"
)
_TAG_RE = re.compile(r"<\s*(/?)\s*([A-Za-z][A-Za-z0-9]*)[^>]*>")
_VOID_TAGS = {"br"}


@dataclass(frozen=True, slots=True)
class _Token:
    raw: str
    text: str
    opening_tag: tuple[str, str] | None = None
    closing_tag: str | None = None


def telegram_text_length(value: str) -> int:
    """Return Telegram's UTF-16 text length for an already parsed string."""

    return len(value.encode("utf-16-le")) // 2


def split_text_message(text: str, limit: int = TELEGRAM_TEXT_LIMIT) -> list[str]:
    """Split plain Telegram text without changing or reordering its contents."""

    tokens = [_Token(raw=character, text=character) for character in text]
    return _split_tokens(tokens, limit, html_mode=False)


def split_html_message(text: str, limit: int = TELEGRAM_TEXT_LIMIT) -> list[str]:
    """Split Telegram HTML while keeping every resulting fragment valid HTML."""

    tokens = _tokenize_html(text)
    return _split_tokens(tokens, limit, html_mode=True)


def _tokenize_html(value: str) -> list[_Token]:
    tokens: list[_Token] = []
    for part in _HTML_TOKEN_RE.split(value):
        if not part:
            continue

        if part.startswith("<"):
            match = _TAG_RE.fullmatch(part)
            if not match:
                tokens.extend(_Token(raw=character, text=character) for character in part)
                continue

            closing, raw_name = match.groups()
            name = raw_name.lower()
            if closing:
                tokens.append(_Token(raw=part, text="", closing_tag=name))
            elif name in _VOID_TAGS or part.rstrip().endswith("/>"):
                tokens.append(_Token(raw=part, text="\n" if name == "br" else ""))
            else:
                tokens.append(_Token(raw=part, text="", opening_tag=(name, part)))
            continue

        if part.startswith("&"):
            tokens.append(_Token(raw=part, text=html.unescape(part)))
            continue

        tokens.extend(_Token(raw=character, text=character) for character in part)
    return tokens


def _split_tokens(tokens: list[_Token], limit: int, html_mode: bool) -> list[str]:
    if limit <= 0:
        raise ValueError("Telegram message limit must be positive.")
    if not tokens:
        return []

    stacks = _tag_stacks(tokens) if html_mode else [()] * (len(tokens) + 1)
    total_length = sum(telegram_text_length(token.text) for token in tokens)
    if total_length <= limit:
        return ["".join(token.raw for token in tokens)]

    chunks: list[str] = []
    start = 0
    while start < len(tokens):
        end = start
        used = 0
        last_newline: int | None = None
        last_space: int | None = None

        while end < len(tokens):
            token = tokens[end]
            token_length = telegram_text_length(token.text)
            if used + token_length > limit:
                break
            used += token_length
            end += 1
            if token.text.endswith("\n"):
                last_newline = end
            elif token.text.isspace():
                last_space = end

        if end == len(tokens):
            split_at = end
        elif last_newline is not None and last_newline > start:
            split_at = last_newline
        elif last_space is not None and last_space > start:
            split_at = last_space
        elif end > start:
            split_at = end
        else:
            # A single Unicode code point may occupy more units than a custom test limit.
            split_at = start + 1

        raw_chunk = "".join(token.raw for token in tokens[start:split_at])
        if html_mode:
            prefix = "".join(raw_tag for _, raw_tag in stacks[start])
            suffix = "".join(f"</{name}>" for name, _ in reversed(stacks[split_at]))
            raw_chunk = prefix + raw_chunk + suffix
        chunks.append(raw_chunk)
        start = split_at

    return chunks


def _tag_stacks(tokens: list[_Token]) -> list[tuple[tuple[str, str], ...]]:
    stack: list[tuple[str, str]] = []
    stacks: list[tuple[tuple[str, str], ...]] = []
    for token in tokens:
        stacks.append(tuple(stack))
        if token.opening_tag:
            stack.append(token.opening_tag)
        elif token.closing_tag:
            for index in range(len(stack) - 1, -1, -1):
                if stack[index][0] == token.closing_tag:
                    del stack[index:]
                    break
    stacks.append(tuple(stack))
    return stacks
