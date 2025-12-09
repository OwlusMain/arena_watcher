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
        self._mapping_pattern = re.compile(r'id:"([^"]+)"[^}]*?displayName:"([^"]+)"', re.DOTALL)
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
        bundle_url, text = self._fetch_bundle_with_mapping()
        mapping_start = text.find("let n=")
        if mapping_start == -1:
            raise DesignArenaFetchError("DesignArena bundle did not contain the expected mapping.")
        mapping_start = text.find("{", mapping_start)
        mapping_end = self._find_matching_brace(text, mapping_start)
        if mapping_end is None:
            raise DesignArenaFetchError("Could not parse the model mapping from DesignArena bundle.")

        segment = text[mapping_start : mapping_end + 1]
        matches = self._mapping_pattern.findall(segment)
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
