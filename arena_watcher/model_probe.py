from __future__ import annotations

from dataclasses import dataclass
from typing import Literal, Optional, Sequence

TEXT_PROBE_PROMPT = "Tell me what model are you, who made you and what is your knowledge cutoff"
IMAGE_PROBE_PROMPT = (
    "Draw a flipboard and write on a flipboard your name and the company who made you"
)

ProbeKind = Literal["text", "image"]

_IMAGE_HINTS = (
    "image",
    "imagen",
    "text-to-image",
    "text_to_image",
    "img",
    "dall-e",
    "dalle",
    "diffusion",
    "flux",
    "sdxl",
    "picture",
    "photo",
    "drawing",
    "art",
)


@dataclass(frozen=True, slots=True)
class ModelProbeResult:
    kind: ProbeKind
    prompt: str
    text: Optional[str] = None
    image_bytes: Optional[bytes] = None
    image_mime_type: Optional[str] = None
    image_url: Optional[str] = None
    error: Optional[str] = None

    @property
    def failed(self) -> bool:
        return bool(self.error)


def probe_prompt_for(kind: ProbeKind) -> str:
    return IMAGE_PROBE_PROMPT if kind == "image" else TEXT_PROBE_PROMPT


def infer_probe_kind(
    identifier: str,
    name: str,
    input_capabilities: Optional[Sequence[str]] = None,
    output_capabilities: Optional[Sequence[str]] = None,
    modes: Optional[Sequence[str]] = None,
) -> ProbeKind:
    signal_parts = [identifier, name]
    signal_parts.extend(input_capabilities or [])
    signal_parts.extend(output_capabilities or [])
    signal_parts.extend(modes or [])
    signal = " ".join(part.lower() for part in signal_parts if part)
    if any(hint in signal for hint in _IMAGE_HINTS):
        return "image"
    return "text"
