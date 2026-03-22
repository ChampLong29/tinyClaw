"""Message chunker: split messages by platform limits."""

from __future__ import annotations


CHANNEL_LIMITS: dict[str, int] = {
    "telegram": 4096,
    "telegram_caption": 1024,
    "discord": 2000,
    "whatsapp": 4096,
    "slack": 40000,
    "feishu": 4000,
    "default": 4096,
}


def chunk_message(text: str, channel: str = "default") -> list[str]:
    """Split message into chunks that fit within channel limits.

    Strategy: split at paragraph boundaries first (\n\n), then hard-split at limit.
    Returns a list of text chunks.
    """
    if not text:
        return []
    limit = CHANNEL_LIMITS.get(channel, CHANNEL_LIMITS["default"])
    if len(text) <= limit:
        return [text]

    chunks: list[str] = []
    for para in text.split("\n\n"):
        if chunks and len(chunks[-1]) + len(para) + 2 <= limit:
            chunks[-1] += "\n\n" + para
        else:
            while len(para) > limit:
                chunks.append(para[:limit])
                para = para[limit:]
            if para:
                chunks.append(para)
    return chunks or [text[:limit]]
