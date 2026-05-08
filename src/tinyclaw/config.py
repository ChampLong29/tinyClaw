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

    legacy_wecom_cli_enabled = os.getenv("WECOM_CLI_ENABLED", "").strip().lower() in ("1", "true")
    raw_wecom_tool_enabled = os.getenv("WECOM_CLI_TOOL_ENABLED", "").strip().lower()
    raw_wecom_poll_enabled = os.getenv("WECOM_CLI_POLL_ENABLED", "").strip().lower()

    wecom_cli_tool_enabled = (
        raw_wecom_tool_enabled in ("1", "true")
        if raw_wecom_tool_enabled
        else legacy_wecom_cli_enabled
    )
    wecom_cli_poll_enabled = (
        raw_wecom_poll_enabled in ("1", "true")
        if raw_wecom_poll_enabled
        else legacy_wecom_cli_enabled
    )

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
        "feishu_reminder_to": os.getenv("FEISHU_REMINDER_TO", "").strip(),
        # WeCom CLI bridge
        "wecom_cli_enabled": legacy_wecom_cli_enabled,
        "wecom_cli_tool_enabled": wecom_cli_tool_enabled,
        "wecom_cli_poll_enabled": wecom_cli_poll_enabled,
        "wecom_cli_bin": os.getenv("WECOM_CLI_BIN", "wecom-cli").strip(),
        "wecom_cli_poll_interval": float(os.getenv("WECOM_CLI_POLL_INTERVAL", "3")),
        "wecom_cli_health_log_interval": float(os.getenv("WECOM_CLI_HEALTH_LOG_INTERVAL", "30")),
        "wecom_cli_lookback_seconds": int(os.getenv("WECOM_CLI_LOOKBACK_SECONDS", "300")),
        "wecom_cli_overlap_seconds": int(os.getenv("WECOM_CLI_OVERLAP_SECONDS", "5")),
        "wecom_cli_debug": os.getenv("WECOM_CLI_DEBUG", "").lower() in ("1", "true"),
        # Work WeChat (企业微信)
        "workwechat_mode": os.getenv("WORKWECHAT_MODE", "off").strip().lower(),
        "workwechat_bot_id": os.getenv("WORKWECHAT_BOT_ID", "").strip(),
        "workwechat_bot_secret": os.getenv("WORKWECHAT_BOT_SECRET", "").strip(),
        "workwechat_ws_url": os.getenv("WORKWECHAT_WS_URL", "wss://openws.work.weixin.qq.com").strip(),
        "workwechat_ping_interval_sec": int(os.getenv("WORKWECHAT_PING_INTERVAL_SEC", "30") or "30"),
        "workwechat_corp_id": os.getenv("WORKWECHAT_CORP_ID", "").strip(),
        "workwechat_corp_secret": os.getenv("WORKWECHAT_CORP_SECRET", "").strip(),
        "workwechat_agent_id": int(os.getenv("WORKWECHAT_AGENT_ID", "0") or "0"),
        "workwechat_webhook_host": os.getenv("WORKWECHAT_WEBHOOK_HOST", "0.0.0.0").strip(),
        "workwechat_webhook_port": int(os.getenv("WORKWECHAT_WEBHOOK_PORT", "8767")),
        "workwechat_webhook_path": os.getenv("WORKWECHAT_WEBHOOK_PATH", "/workwechat/events").strip(),
        "workwechat_webhook_token": os.getenv("WORKWECHAT_WEBHOOK_TOKEN", "").strip(),
        # DingTalk (钉钉)
        "dingtalk_mode": os.getenv("DINGTALK_MODE", "off").strip().lower(),
        "dingtalk_client_id": os.getenv("DINGTALK_CLIENT_ID", "").strip(),
        "dingtalk_client_secret": os.getenv("DINGTALK_CLIENT_SECRET", "").strip(),
        "dingtalk_access_token": os.getenv("DINGTALK_ACCESS_TOKEN", "").strip(),
        "dingtalk_secret": os.getenv("DINGTALK_SECRET", "").strip(),
        "dingtalk_webhook_url": os.getenv("DINGTALK_WEBHOOK_URL", "").strip(),
        "dingtalk_api_base": os.getenv("DINGTALK_API_BASE", "https://oapi.dingtalk.com").strip(),
        "dingtalk_webhook_host": os.getenv("DINGTALK_WEBHOOK_HOST", "0.0.0.0").strip(),
        "dingtalk_webhook_port": int(os.getenv("DINGTALK_WEBHOOK_PORT", "8768")),
        "dingtalk_webhook_path": os.getenv("DINGTALK_WEBHOOK_PATH", "/dingtalk/events").strip(),
        "dingtalk_webhook_token": os.getenv("DINGTALK_WEBHOOK_TOKEN", "").strip(),
        # Heartbeat
        "heartbeat_interval": float(os.getenv("HEARTBEAT_INTERVAL", "1800")),
        "heartbeat_active_start": int(os.getenv("HEARTBEAT_ACTIVE_START", "9")),
        "heartbeat_active_end": int(os.getenv("HEARTBEAT_ACTIVE_END", "22")),
        "reminder_check_interval": float(os.getenv("REMINDER_CHECK_INTERVAL", "60")),
    }


def resolve_workspace(workspace_arg: str | None = None) -> Path:
    """Resolve the workspace directory."""
    if workspace_arg:
        return Path(workspace_arg).resolve()
    return Path(__file__).parent.parent.parent.parent / "workspace"
