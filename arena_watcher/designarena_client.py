from __future__ import annotations

import logging
import re
from dataclasses import dataclass
from typing import List
from urllib.parse import urljoin

import requests

from .arena_client import ModelEntry

logger = logging.getLogger(__name__)


class DesignArenaFetchError(RuntimeError):
    """Raised when the DesignArena bundle cannot be fetched or parsed."""


@dataclass(frozen=True, slots=True)
class DesignArenaClientConfig:
    base_url: str = "https://www.designarena.ai/"
    bundle_path_pattern: str = r"/?_next/static/chunks/4529-[A-Za-z0-9-]+\\.js[^\"'\\s>]*"


class DesignArenaClient:
    def __init__(self, config: DesignArenaClientConfig | None = None) -> None:
        self._config = config or DesignArenaClientConfig()
        self._mapping_pattern = re.compile(r'id:"([^"]+)"[^}]*?displayName:"([^"]+)"', re.DOTALL)
        self._bundle_path_regex = re.compile(self._config.bundle_path_pattern)

    def fetch_models(self) -> List[ModelEntry]:
        bundle_url = self._discover_bundle_url()
        text = self._fetch_text(bundle_url)
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

    def _discover_bundle_url(self) -> str:
        """
        Fetch the DesignArena homepage and extract the hashed bundle path that contains the model mapping.
        Falls back to the Next.js build manifest if the bundle is not found directly in the HTML.
        """
        try:
            response = requests.get(self._config.base_url, timeout=30)
        except Exception as exc:  # pragma: no cover - network failure
            raise DesignArenaFetchError(f"Failed to reach {self._config.base_url}: {exc}") from exc

        if response.status_code < 200 or response.status_code >= 300:
            raise DesignArenaFetchError(
                f"DesignArena responded with status {response.status_code} for {response.url}."
            )

        html = response.text or ""
        path = self._extract_bundle_path(html)
        if path:
            return urljoin(self._config.base_url, path)

        manifest_url = self._extract_manifest_url(html)
        if manifest_url:
            manifest_text = self._fetch_text(urljoin(self._config.base_url, manifest_url))
            print(manifest_text)
            path = self._extract_bundle_path(manifest_text)
            if path:
                return urljoin(self._config.base_url, path)

        raise DesignArenaFetchError(
            "Could not locate DesignArena model bundle in the homepage HTML or manifest."
        )

    def _fetch_text(self, url: str) -> str:
        try:
            response = requests.get(url, timeout=30)
        except Exception as exc:  # pragma: no cover - network failure
            raise DesignArenaFetchError(f"Failed to reach {url}: {exc}") from exc

        if response.status_code < 200 or response.status_code >= 300:
            raise DesignArenaFetchError(
                f"DesignArena responded with status {response.status_code} for {response.url}."
            )
        return response.text

    def _extract_bundle_path(self, text: str) -> str | None:
        match = self._bundle_path_regex.search(text)
        if match:
            path = match.group(0)
            if not path.startswith("/"):
                path = "/" + path
            if "/_next/" not in path:
                path = "/_next" + path
            return path

        # Fallback heuristic: locate the chunk name manually when regex misses (e.g., minified HTML attributes)
        for marker in ("_next/static/chunks/4529-", "static/chunks/4529-"):
            idx = text.find(marker)
            if idx != -1:
                end = text.find(".js", idx)
                if end != -1:
                    end += 3
                    # include query string if present
                    while end < len(text) and text[end] not in "\"'\\s><":
                        end += 1
                    path = text[idx:end]
                    if not path.startswith("/"):
                        path = "/" + path
                    if "/_next/" not in path:
                        path = "/_next" + path
                    return path
        return None

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
