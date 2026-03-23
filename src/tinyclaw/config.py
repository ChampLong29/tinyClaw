"""Configuration management for tinyClaw.

Loads settings from environment variables (.env file).
"""

from __future__ import annotations

import os
from pathlib import Path
from typing import Any

from dotenv import load_dotenv


def load_config(env_path: Path | None = None) -> dict[str, Any]:
    """Load configuration from .env file and environment variables."""
    if env_path is None:
        env_path = Path.cwd() / ".env"
    load_dotenv(env_path, override=True)

    return {
        "anthropic_api_key": os.getenv("ANTHROPIC_API_KEY", ""),
        "model_id": os.getenv("MODEL_ID", "claude-sonnet-4-20250514"),
        "anthropic_base_url": os.getenv("ANTHROPIC_BASE_URL") or None,
        "workspace_dir": Path(os.getenv("WORKSPACE_DIR", str(Path(__file__).parent.parent.parent.parent / "workspace"))),
        # Telegram
        "telegram_bot_token": os.getenv("TELEGRAM_BOT_TOKEN", "").strip(),
        "telegram_allowed_chats": os.getenv("TELEGRAM_ALLOWED_CHATS", ""),
        # Feishu
        "feishu_app_id": os.getenv("FEISHU_APP_ID", "").strip(),
        "feishu_app_secret": os.getenv("FEISHU_APP_SECRET", "").strip(),
        "feishu_encrypt_key": os.getenv("FEISHU_ENCRYPT_KEY", ""),
        "feishu_bot_open_id": os.getenv("FEISHU_BOT_OPEN_ID", ""),
        "feishu_is_lark": os.getenv("FEISHU_IS_LARK", "").lower() in ("1", "true"),
        "feishu_mode": os.getenv("FEISHU_MODE", "both").strip().lower(),
        "feishu_webhook_host": os.getenv("FEISHU_WEBHOOK_HOST", "0.0.0.0").strip(),
        "feishu_webhook_port": int(os.getenv("FEISHU_WEBHOOK_PORT", "8766")),
        "feishu_webhook_path": os.getenv("FEISHU_WEBHOOK_PATH", "/feishu/events").strip(),
        # Heartbeat
        "heartbeat_interval": float(os.getenv("HEARTBEAT_INTERVAL", "1800")),
        "heartbeat_active_start": int(os.getenv("HEARTBEAT_ACTIVE_START", "9")),
        "heartbeat_active_end": int(os.getenv("HEARTBEAT_ACTIVE_END", "22")),
    }


def resolve_workspace(workspace_arg: str | None = None) -> Path:
    """Resolve the workspace directory."""
    if workspace_arg:
        return Path(workspace_arg).resolve()
    return Path(__file__).parent.parent.parent.parent / "workspace"
