"""CLI channel: terminal REPL for tinyClaw."""

from __future__ import annotations

from tinyclaw.channel.base import Channel, InboundMessage
from tinyclaw.utils.ansi import CYAN, BOLD, RESET


class CLIChannel(Channel):
    """Terminal REPL channel (stdin/stdout)."""

    name = "cli"

    def __init__(self, account_id: str = "cli-local") -> None:
        self.account_id = account_id

    def receive(self) -> InboundMessage | None:
        try:
            text = input(f"{CYAN}{BOLD}You > {RESET}").strip()
        except (KeyboardInterrupt, EOFError):
            return None
        if not text:
            return None
        return InboundMessage(
            text=text,
            sender_id="cli-user",
            channel="cli",
            account_id=self.account_id,
            peer_id="cli-user",
        )

    def send(self, to: str, text: str, **kwargs) -> bool:
        from tinyclaw.utils.ansi import GREEN
        print(f"\n{GREEN}{BOLD}Assistant:{RESET} {text}\n")
        return True
