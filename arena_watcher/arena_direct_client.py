from __future__ import annotations

import base64
import json
import logging
import subprocess
import uuid
from dataclasses import dataclass, field
from typing import Any, Dict, List, Optional
from urllib.parse import urlsplit

import cloudscraper
from requests import Response

from .arena_client import _extract_path
from .model_probe import ModelProbeResult, ProbeKind, probe_prompt_for

logger = logging.getLogger(__name__)


class ArenaDirectProbeError(RuntimeError):
    """Raised when a direct Arena probe request fails."""


@dataclass(frozen=True, slots=True)
class ArenaDirectClientConfig:
    url: str
    request_template: Any | None = None
    headers: Dict[str, Any] = field(default_factory=dict)
    cookies: Dict[str, Any] = field(default_factory=dict)
    bootstrap_url: Optional[str] = None
    recaptcha_v3_token: Optional[str] = None
    recaptcha_v3_token_command: Optional[str] = None
    text_response_path: List[str] = field(default_factory=list)
    image_url_response_path: List[str] = field(default_factory=list)
    image_base64_response_path: List[str] = field(default_factory=list)
    image_mime_type_response_path: List[str] = field(default_factory=list)
    timeout_seconds: int = 60


class ArenaDirectClient:
    _DEFAULT_REQUEST_TEMPLATE = {
        "id": "$EVALUATION_ID",
        "mode": "direct-battle",
        "modelAId": "$MODEL_ID",
        "userMessageId": "$USER_MESSAGE_ID",
        "modelAMessageId": "$MODEL_MESSAGE_ID",
        "userMessage": {
            "content": "$PROMPT",
            "experimental_attachments": [],
            "metadata": {},
        },
        "modality": "$MODALITY",
        "recaptchaV3Token": "$RECAPTCHA_V3_TOKEN",
    }
    _DEFAULT_TEXT_PATHS = (
        ["text"],
        ["response"],
        ["output_text"],
        ["message"],
        ["content"],
        ["data", "text"],
        ["events"],
        ["output", "0", "content", "0", "text"],
    )
    _DEFAULT_IMAGE_URL_PATHS = (
        ["image_url"],
        ["url"],
        ["image", "url"],
        ["images", "0", "url"],
        ["images", "0", "image_url"],
        ["data", "0", "url"],
    )
    _DEFAULT_IMAGE_BASE64_PATHS = (
        ["image_base64"],
        ["b64_json"],
        ["image", "b64_json"],
        ["image", "base64"],
        ["images", "0", "b64_json"],
        ["images", "0", "image_base64"],
        ["data", "0", "b64_json"],
    )
    _DEFAULT_IMAGE_MIME_TYPE_PATHS = (
        ["mime_type"],
        ["image", "mime_type"],
        ["images", "0", "mime_type"],
        ["data", "0", "mime_type"],
    )

    def __init__(self, config: ArenaDirectClientConfig) -> None:
        self._config = config
        self._scraper = cloudscraper.create_scraper()

    def probe_model(self, model_id: str, kind: ProbeKind) -> ModelProbeResult:
        prompt = probe_prompt_for(kind)
        evaluation_id = self._new_request_id()
        user_message_id = self._new_request_id()
        model_message_id = self._new_request_id()
        recaptcha_v3_token = self._get_recaptcha_v3_token()
        replacements = {
            "model_id": model_id,
            "prompt": prompt,
            "modality": self._arena_modality(kind),
            "request_id": self._new_request_id(),
            "evaluation_id": evaluation_id,
            "user_message_id": user_message_id,
            "model_message_id": model_message_id,
            "recaptcha_v3_token": recaptcha_v3_token,
        }

        headers = self._build_headers(replacements)
        cookies = self._build_cookies(replacements)
        self._bootstrap_direct_page(headers, cookies, replacements)

        payload_template = self._config.request_template or self._DEFAULT_REQUEST_TEMPLATE
        payload = self._prune_none(self._render_template(payload_template, **replacements))
        request_kwargs = self._build_request_kwargs(payload, headers)

        try:
            response = self._scraper.post(
                self._config.url,
                headers=headers,
                cookies=cookies,
                timeout=self._config.timeout_seconds,
                **request_kwargs,
            )
        except Exception as exc:  # pragma: no cover - network failure
            raise ArenaDirectProbeError(f"Failed to reach Arena direct endpoint: {exc}") from exc

        self._ensure_ok(response)
        return self._parse_response(response, kind, prompt)

    def _build_headers(self, replacements: Dict[str, Any]) -> Dict[str, str]:
        headers = self._prune_none(self._render_template(self._default_headers(), **replacements)) or {}
        headers.update(
            self._prune_none(self._render_template(self._config.headers, **replacements)) or {}
        )
        return {str(key): str(value) for key, value in headers.items()}

    def _build_cookies(self, replacements: Dict[str, Any]) -> Dict[str, str]:
        cookies = self._prune_none(self._render_template(self._config.cookies, **replacements)) or {}
        return {str(key): str(value) for key, value in cookies.items()}

    def _default_headers(self) -> Dict[str, str]:
        parsed = urlsplit(self._config.url)
        origin = f"{parsed.scheme}://{parsed.netloc}" if parsed.scheme and parsed.netloc else ""
        headers: Dict[str, str] = {
            "accept": "*/*",
            "content-type": "text/plain;charset=UTF-8",
        }
        if origin:
            headers["origin"] = origin
            headers["referer"] = f"{origin}/c/$EVALUATION_ID"
            headers["x-request-id"] = "$REQUEST_ID"
        return headers

    def _bootstrap_direct_page(
        self,
        headers: Dict[str, str],
        cookies: Dict[str, str],
        replacements: Dict[str, Any],
    ) -> None:
        bootstrap_url = self._resolve_bootstrap_url()
        if not bootstrap_url:
            return

        bootstrap_headers = dict(headers)
        bootstrap_headers.pop("content-type", None)
        bootstrap_headers["referer"] = self._default_page_referer()
        bootstrap_headers = {
            key: value
            for key, value in bootstrap_headers.items()
            if value is not None and value != ""
        }

        try:
            response = self._scraper.get(
                self._render_template(bootstrap_url, **replacements),
                headers=bootstrap_headers,
                cookies=cookies,
                timeout=self._config.timeout_seconds,
            )
            response.raise_for_status()
        except Exception as exc:  # pragma: no cover - network failure
            if self._config.bootstrap_url:
                raise ArenaDirectProbeError(
                    f"Failed to bootstrap Arena direct page {bootstrap_url}: {exc}"
                ) from exc
            logger.debug("Arena direct bootstrap request failed: %s", exc)

    def _resolve_bootstrap_url(self) -> Optional[str]:
        if self._config.bootstrap_url:
            return self._config.bootstrap_url
        parsed = urlsplit(self._config.url)
        if parsed.netloc != "arena.ai":
            return None
        if not parsed.scheme:
            return None
        return f"{parsed.scheme}://{parsed.netloc}/text/direct"

    def _default_page_referer(self) -> str:
        parsed = urlsplit(self._config.url)
        if parsed.scheme and parsed.netloc:
            return f"{parsed.scheme}://{parsed.netloc}/"
        return ""

    def _get_recaptcha_v3_token(self) -> Optional[str]:
        if self._config.recaptcha_v3_token:
            return self._config.recaptcha_v3_token.strip() or None
        command = self._config.recaptcha_v3_token_command
        if not command:
            return None
        try:
            completed = subprocess.run(
                command,
                shell=True,
                capture_output=True,
                text=True,
                check=False,
            )
        except Exception as exc:  # pragma: no cover - subprocess failure
            raise ArenaDirectProbeError(
                f"Failed to execute ARENA_DIRECT_RECAPTCHA_V3_TOKEN_COMMAND: {exc}"
            ) from exc
        if completed.returncode != 0:
            stderr = completed.stderr.strip()
            raise ArenaDirectProbeError(
                "ARENA_DIRECT_RECAPTCHA_V3_TOKEN_COMMAND exited with "
                f"{completed.returncode}: {stderr or 'no stderr'}"
            )
        token = completed.stdout.strip()
        return token or None

    def _parse_response(
        self,
        response: Response,
        kind: ProbeKind,
        prompt: str,
    ) -> ModelProbeResult:
        content_type = response.headers.get("content-type", "").lower()
        if kind == "image" and content_type.startswith("image/"):
            return ModelProbeResult(
                kind=kind,
                prompt=prompt,
                image_bytes=response.content,
                image_mime_type=content_type.split(";", 1)[0],
            )

        payload: Any = None
        if "application/json" in content_type:
            try:
                payload = response.json()
            except ValueError as exc:
                raise ArenaDirectProbeError("Arena direct endpoint returned invalid JSON.") from exc
        else:
            payload = self._parse_stream_or_text(response.text or "")

        stream_error = self._extract_stream_error(payload)
        if kind == "text":
            text = self._extract_text(payload)
            if not text and isinstance(payload, str):
                text = payload.strip()
            if text:
                return ModelProbeResult(kind=kind, prompt=prompt, text=text)
            if stream_error:
                raise ArenaDirectProbeError(stream_error)
            raise ArenaDirectProbeError("Could not extract text from Arena direct response.")

        image_url = self._extract_image_url(payload)
        if image_url:
            return ModelProbeResult(kind=kind, prompt=prompt, image_url=image_url)

        image_bytes, mime_type = self._extract_image_bytes(payload)
        if image_bytes:
            return ModelProbeResult(
                kind=kind,
                prompt=prompt,
                image_bytes=image_bytes,
                image_mime_type=mime_type,
            )

        if stream_error:
            raise ArenaDirectProbeError(stream_error)
        raise ArenaDirectProbeError("Could not extract an image from Arena direct response.")

    def _build_request_kwargs(self, payload: Any, headers: Dict[str, str]) -> Dict[str, Any]:
        content_type = str(headers.get("content-type", "")).lower()
        if isinstance(payload, str):
            return {"data": payload}
        if content_type.startswith("text/plain"):
            return {"data": json.dumps(payload, separators=(",", ":"))}
        return {"json": payload}

    def _parse_stream_or_text(self, text: str) -> Any:
        stripped = text.strip()
        if not stripped:
            return None
        if stripped.startswith("{") or stripped.startswith("["):
            try:
                return json.loads(stripped)
            except ValueError:
                return stripped

        events: List[Dict[str, Any]] = []
        text_fragments: List[str] = []
        fallback_fragments: List[str] = []
        data_parts: List[Any] = []
        files: List[Dict[str, Any]] = []
        images: List[Dict[str, Any]] = []
        errors: List[str] = []

        for raw_line in stripped.splitlines():
            line = raw_line.strip()
            if not line or line.startswith("event:"):
                continue
            if line.startswith("data:"):
                line = line[5:].strip()
            if not line or line == "[DONE]":
                continue

            event = self._parse_arena_stream_line(line)
            if event is None:
                try:
                    parsed = json.loads(line)
                except ValueError:
                    fallback_fragments.append(line)
                    continue
                events.append({"code": "json", "value": parsed})
                continue

            events.append(event)
            code = event["code"]
            value = event["value"]
            if code == "0":
                text = self._coerce_text(value)
                if text:
                    text_fragments.append(text)
                continue
            if code == "2":
                parts = value if isinstance(value, list) else [value]
                data_parts.extend(parts)
                for part in parts:
                    image = self._normalize_stream_image(part)
                    if image:
                        images.append(image)
                continue
            if code == "3":
                error = self._coerce_error(value)
                if error:
                    errors.append(error)
                continue
            if code == "k":
                file_entry = self._normalize_stream_file(value)
                if file_entry:
                    files.append(file_entry)
                    images.append(file_entry)

        normalized: Dict[str, Any] = {"events": events}
        text_value = "".join(text_fragments).strip()
        if text_value:
            normalized["text"] = text_value
        elif fallback_fragments:
            normalized["text"] = "\n".join(fallback_fragments).strip()
        if data_parts:
            normalized["data"] = data_parts
        if files:
            normalized["files"] = files
        if images:
            normalized["images"] = images
            first_image = images[0]
            if first_image.get("image_url"):
                normalized["image_url"] = first_image["image_url"]
            if first_image.get("image_base64"):
                normalized["image_base64"] = first_image["image_base64"]
            if first_image.get("mime_type"):
                normalized["mime_type"] = first_image["mime_type"]
        if errors:
            normalized["errors"] = errors
        return normalized

    @staticmethod
    def _parse_arena_stream_line(line: str) -> Optional[Dict[str, Any]]:
        if len(line) < 3:
            return None
        participant = line[0]
        encoded = line[1:]
        if ":" not in encoded:
            return None
        code, raw_value = encoded.split(":", 1)
        if not code:
            return None
        try:
            value = json.loads(raw_value)
        except ValueError:
            value = raw_value
        return {"participant": participant, "code": code, "value": value}

    def _extract_text(self, payload: Any) -> Optional[str]:
        for path in self._iter_paths(
            configured=self._config.text_response_path,
            defaults=self._DEFAULT_TEXT_PATHS,
        ):
            value = self._safe_extract(payload, path)
            text = self._coerce_text(value)
            if text:
                return text
        if isinstance(payload, list):
            fragments = [self._extract_text(item) for item in payload]
            filtered = [fragment for fragment in fragments if fragment]
            if filtered:
                return "\n".join(filtered)
        return self._coerce_text(payload)

    def _extract_image_url(self, payload: Any) -> Optional[str]:
        for path in self._iter_paths(
            configured=self._config.image_url_response_path,
            defaults=self._DEFAULT_IMAGE_URL_PATHS,
        ):
            value = self._safe_extract(payload, path)
            if isinstance(value, str) and value.strip():
                return value.strip()
        return None

    def _extract_image_bytes(self, payload: Any) -> tuple[Optional[bytes], Optional[str]]:
        mime_type = None
        for path in self._iter_paths(
            configured=self._config.image_mime_type_response_path,
            defaults=self._DEFAULT_IMAGE_MIME_TYPE_PATHS,
        ):
            value = self._safe_extract(payload, path)
            if isinstance(value, str) and value.strip():
                mime_type = value.strip()
                break

        for path in self._iter_paths(
            configured=self._config.image_base64_response_path,
            defaults=self._DEFAULT_IMAGE_BASE64_PATHS,
        ):
            value = self._safe_extract(payload, path)
            if not isinstance(value, str) or not value.strip():
                continue
            encoded = value.strip()
            if encoded.startswith("data:") and "," in encoded:
                prefix, encoded = encoded.split(",", 1)
                if not mime_type and ";base64" in prefix:
                    mime_type = prefix[5:].split(";", 1)[0]
            try:
                return base64.b64decode(encoded), mime_type
            except ValueError:
                continue
        if isinstance(payload, list):
            for item in payload:
                image_bytes, item_mime_type = self._extract_image_bytes(item)
                if image_bytes:
                    return image_bytes, item_mime_type or mime_type
        return None, mime_type

    def _extract_stream_error(self, payload: Any) -> Optional[str]:
        if not isinstance(payload, dict):
            return None
        errors = payload.get("errors")
        if not isinstance(errors, list):
            return None
        messages = [self._coerce_error(item) for item in errors]
        filtered = [message for message in messages if message]
        if not filtered:
            return None
        return "; ".join(filtered)

    @staticmethod
    def _normalize_stream_image(value: Any) -> Optional[Dict[str, Any]]:
        if not isinstance(value, dict):
            return None
        image_url = value.get("url") or value.get("imageUrl") or value.get("image_url")
        image_base64 = value.get("data") or value.get("base64") or value.get("b64_json")
        mime_type = value.get("mimeType") or value.get("mime_type")
        if not any((image_url, image_base64)):
            return None
        normalized: Dict[str, Any] = {}
        if image_url:
            normalized["image_url"] = str(image_url)
        if image_base64:
            normalized["image_base64"] = str(image_base64)
        if mime_type:
            normalized["mime_type"] = str(mime_type)
        return normalized or None

    @staticmethod
    def _normalize_stream_file(value: Any) -> Optional[Dict[str, Any]]:
        if not isinstance(value, dict):
            return None
        mime_type = value.get("mimeType") or value.get("mime_type")
        if not isinstance(mime_type, str) or not mime_type.startswith("image/"):
            return None
        image_base64 = value.get("base64") or value.get("data")
        image_url = value.get("url") or value.get("imageUrl") or value.get("image_url")
        if not image_base64 and not image_url:
            return None
        normalized: Dict[str, Any] = {"mime_type": mime_type}
        if image_base64:
            normalized["image_base64"] = str(image_base64)
        if image_url:
            normalized["image_url"] = str(image_url)
        return normalized

    @staticmethod
    def _render_template(template: Any, **replacements: Any) -> Any:
        if isinstance(template, str):
            for key, value in replacements.items():
                placeholder = f"${key.upper()}"
                if template == placeholder:
                    return value
            rendered = template
            for key, value in replacements.items():
                placeholder = f"${key.upper()}"
                rendered = rendered.replace(placeholder, "" if value is None else str(value))
            return rendered
        if isinstance(template, list):
            return [ArenaDirectClient._render_template(item, **replacements) for item in template]
        if isinstance(template, dict):
            return {
                str(key): ArenaDirectClient._render_template(value, **replacements)
                for key, value in template.items()
            }
        return template

    @staticmethod
    def _prune_none(value: Any) -> Any:
        if isinstance(value, dict):
            return {
                key: ArenaDirectClient._prune_none(item)
                for key, item in value.items()
                if item is not None
            }
        if isinstance(value, list):
            return [ArenaDirectClient._prune_none(item) for item in value if item is not None]
        return value

    @staticmethod
    def _iter_paths(
        configured: List[str],
        defaults: tuple[List[str], ...],
    ) -> List[List[str]]:
        paths: List[List[str]] = []
        if configured:
            paths.append(configured)
        paths.extend(list(defaults))
        return paths

    @staticmethod
    def _safe_extract(payload: Any, path: List[str]) -> Any:
        try:
            return _extract_path(payload, path)
        except Exception:
            return None

    @staticmethod
    def _coerce_text(value: Any) -> Optional[str]:
        if value is None:
            return None
        if isinstance(value, str):
            cleaned = value.strip()
            return cleaned or None
        if isinstance(value, list):
            fragments = [ArenaDirectClient._coerce_text(item) for item in value]
            filtered = [fragment for fragment in fragments if fragment]
            return "\n".join(filtered) if filtered else None
        if isinstance(value, dict):
            for key in (
                "text",
                "content",
                "message",
                "output_text",
                "delta",
                "response",
                "output",
                "events",
                "data",
            ):
                nested = ArenaDirectClient._coerce_text(value.get(key))
                if nested:
                    return nested
            return None
        return str(value)

    @staticmethod
    def _coerce_error(value: Any) -> Optional[str]:
        if value is None:
            return None
        if isinstance(value, str):
            cleaned = value.strip()
            return cleaned or None
        if isinstance(value, dict):
            for key in ("message", "error", "text", "detail"):
                nested = ArenaDirectClient._coerce_error(value.get(key))
                if nested:
                    return nested
            return json.dumps(value, ensure_ascii=True)
        if isinstance(value, list):
            parts = [ArenaDirectClient._coerce_error(item) for item in value]
            filtered = [part for part in parts if part]
            return "; ".join(filtered) if filtered else None
        return str(value)

    @staticmethod
    def _new_request_id() -> str:
        generator = getattr(uuid, "uuid7", None)
        if callable(generator):
            return str(generator())
        return str(uuid.uuid4())

    @staticmethod
    def _arena_modality(kind: ProbeKind) -> str:
        if kind == "image":
            return "image"
        return "chat"

    @staticmethod
    def _ensure_ok(response: Response) -> None:
        if 200 <= response.status_code < 300:
            return
        body = response.text.strip()
        body_preview = f": {body[:300]}" if body else ""
        raise ArenaDirectProbeError(
            "Arena direct endpoint responded with status "
            f"{response.status_code} for {response.url}{body_preview}"
        )
