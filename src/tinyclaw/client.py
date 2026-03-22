"""Anthropic client factory for tinyClaw."""

from __future__ import annotations

from anthropic import Anthropic


def create_client(api_key: str, base_url: str | None = None) -> Anthropic:
    """Create an Anthropic client instance."""
    return Anthropic(api_key=api_key, base_url=base_url)
