from __future__ import annotations

import logging
import re
from dataclasses import dataclass
from typing import Iterable, List, Set
from urllib.parse import urljoin

import requests

from .arena_client import ModelEntry

logger = logging.getLogger(__name__)


class DesignArenaFetchError(RuntimeError):
    """Raised when the DesignArena bundle cannot be fetched or parsed."""


@dataclass(frozen=True, slots=True)
class DesignArenaClientConfig:
    base_url: str = "https://www.designarena.ai/"


class DesignArenaClient:
    def __init__(self, config: DesignArenaClientConfig | None = None) -> None:
        self._config = config or DesignArenaClientConfig()
        # Match script src values for .js files (with optional query strings), case-insensitive.
        self._script_src_regex = re.compile(r'src=["\']([^"\']+\.js[^"\']*)["\']', re.IGNORECASE)
        self._session = requests.Session()
        self._session.headers.update(
            {
                "User-Agent": "Mozilla/5.0 (X11; Linux x86_64) AppleWebKit/537.36 "
                "(KHTML, like Gecko) Chrome/123.0.0.0 Safari/537.36",
            }
        )

    def fetch_models(self) -> List[ModelEntry]:
        _, text = self._fetch_bundle_with_mapping()
        model_block = self._find_largest_model_block(text)
        if not model_block:
            raise DesignArenaFetchError("No models found in the DesignArena bundle.")

        matches = self._extract_model_entries(model_block)
        if not matches:
            raise DesignArenaFetchError("No models found in the DesignArena bundle.")

        entries: list[ModelEntry] = []
        for identifier, display_name in matches:
            entries.append(ModelEntry(identifier=identifier, name=display_name, raw={"id": identifier, "name": display_name}))
        return entries

    def _fetch_bundle_with_mapping(self) -> tuple[str, str]:
        """
        Find and fetch the JS bundle that contains the model mapping by scanning all candidate
        script URLs from the homepage and Next.js build manifest.
        """
        try:
            response = self._session.get(self._config.base_url, timeout=30)
        except Exception as exc:  # pragma: no cover - network failure
            raise DesignArenaFetchError(f"Failed to reach {self._config.base_url}: {exc}") from exc

        if response.status_code < 200 or response.status_code >= 300:
            raise DesignArenaFetchError(
                f"DesignArena responded with status {response.status_code} for {response.url}."
            )

        html = response.text or ""
        candidates: Set[str] = set(self._extract_script_urls(html))

        manifest_url = self._extract_manifest_url(html)
        if manifest_url:
            manifest_text = self._fetch_text(urljoin(self._config.base_url, manifest_url))
            candidates.update(self._extract_script_urls(manifest_text))

        if not candidates:
            # Retry with a fresh one-off request (matches the manual repro) in case session headers/cookies
            # influenced the response content.
            try:
                fallback_html = requests.get(self._config.base_url, timeout=30).text
                candidates.update(self._extract_script_urls(fallback_html))
            except Exception:
                pass

        tried: list[str] = []
        if not candidates:
            raise DesignArenaFetchError("No script candidates found in DesignArena HTML.")

        for path in candidates:
            url = urljoin(self._config.base_url, path)
            try:
                text = self._fetch_text(url)
            except DesignArenaFetchError:
                tried.append(url)
                continue
            if (
                "open_source:!" in text
            ):
                mapping_pos = text.find("let n=")
                brace_pos = text.find("{", mapping_pos) if mapping_pos != -1 else -1
                if mapping_pos != -1 and brace_pos != -1:
                    return url, text
            tried.append(url)

        raise DesignArenaFetchError(
            "Could not locate DesignArena model bundle after checking scripts. Tried: "
            + ", ".join(tried[:5])
            + ("..." if len(tried) > 5 else "")
        )

    def _fetch_text(self, url: str) -> str:
        try:
            response = self._session.get(url, timeout=30)
        except Exception as exc:  # pragma: no cover - network failure
            raise DesignArenaFetchError(f"Failed to reach {url}: {exc}") from exc

        if response.status_code < 200 or response.status_code >= 300:
            raise DesignArenaFetchError(
                f"DesignArena responded with status {response.status_code} for {response.url}."
            )
        return response.text

    def _extract_script_urls(self, text: str) -> Iterable[str]:
        # Explicit script tags
        for match in self._script_src_regex.finditer(text):
            path = match.group(1)
            if path.startswith("//"):
                path = "https:" + path
            if not path.startswith(("http://", "https://", "/")):
                path = "/" + path
            yield path

        # Fallback: any quoted .js reference in the text
        for match in re.finditer(r'["\']([^"\']+\.js[^"\']*)["\']', text):
            path = match.group(1)
            if path.startswith("//"):
                path = "https:" + path
            if not path.startswith(("http://", "https://", "/")):
                path = "/" + path
            yield path

        # Last resort: loose scan for _next/static JS paths even if unquoted
        for match in re.finditer(r'/_next/static[^\\s><"\']+\\.js[^\\s><"\']*', text):
            path = match.group(0)
            yield path

    def _extract_manifest_url(self, html: str) -> str | None:
        """
        Locate the Next.js build manifest URL within the HTML to resolve the hashed bundle path.
        """
        manifest_match = re.search(r'/_next/static/[^/]+/_buildManifest\\.js', html)
        if manifest_match:
            return manifest_match.group(0)
        return None

    def _find_matching_brace(self, text: str, start: int) -> int | None:
        depth = 0
        quote: str | None = None
        escape = False
        for index, char in enumerate(text[start:], start):
            if escape:
                escape = False
                continue
            if char == "\\":
                escape = True
                continue
            if quote:
                if char == quote:
                    quote = None
                continue
            if char in ('"', "'"):
                quote = char
                continue
            if char == "{":
                depth += 1
            elif char == "}":
                depth -= 1
                if depth == 0:
                    return index
        return None

    def _extract_top_level_object_values(self, text: str, start: int) -> list[str]:
        end = self._find_matching_brace(text, start)
        if end is None:
            return []

        values: list[str] = []
        depth = 0
        quote: str | None = None
        escape = False
        index = start
        while index <= end:
            char = text[index]
            if escape:
                escape = False
                index += 1
                continue
            if char == "\\":
                escape = True
                index += 1
                continue
            if quote:
                if char == quote:
                    quote = None
                index += 1
                continue
            if char in ('"', "'"):
                quote = char
                index += 1
                continue
            if char == "{":
                depth += 1
                index += 1
                continue
            if char == "}":
                depth -= 1
                index += 1
                continue
            if char == ":" and depth == 1:
                scan = index + 1
                while scan <= end and text[scan].isspace():
                    scan += 1
                if scan <= end and text[scan] == "{":
                    block_end = self._find_matching_brace(text, scan)
                    if block_end is not None:
                        values.append(text[scan : block_end + 1])
                        index = block_end + 1
                        continue
            index += 1

        return values

    def _iter_object_spans(self, text: str) -> Iterable[tuple[int, int]]:
        stack: list[int] = []
        quote: str | None = None
        escape = False
        for index, char in enumerate(text):
            if escape:
                escape = False
                continue
            if char == "\\":
                escape = True
                continue
            if quote:
                if char == quote:
                    quote = None
                continue
            if char in ('"', "'"):
                quote = char
                continue
            if char == "{":
                stack.append(index)
            elif char == "}" and stack:
                start = stack.pop()
                yield start, index

    def _find_largest_model_block(self, text: str) -> str | None:
        best_block = None
        best_count = 0
        for start, end in self._iter_object_spans(text):
            if end - start < 500:
                continue
            segment = text[start : end + 1]
            if "displayName" not in segment or "id" not in segment:
                continue
            entries = self._extract_model_entries(segment)
            if len(entries) > best_count:
                best_count = len(entries)
                best_block = segment
        return best_block

    def _extract_model_entries(self, block: str) -> list[tuple[str, str]]:
        entries: list[tuple[str, str]] = []
        objects = self._extract_top_level_object_values(block, 0)
        for obj in objects:
            id_match = re.search(r"\bid\s*:\s*['\"]([^'\"]+)['\"]", obj)
            display_match = re.search(r"\bdisplayName\s*:\s*['\"]([^'\"]+)['\"]", obj)
            if id_match and display_match:
                entries.append((id_match.group(1), display_match.group(1)))
        return entries
