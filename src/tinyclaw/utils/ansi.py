"""ANSI color utilities for terminal output."""

from __future__ import annotations

# ANSI escape codes
CYAN = "\033[36m"
GREEN = "\033[32m"
YELLOW = "\033[33m"
RED = "\033[31m"
DIM = "\033[2m"
RESET = "\033[0m"
BOLD = "\033[1m"
MAGENTA = "\033[35m"
BLUE = "\033[34m"
ORANGE = "\033[38;5;208m"


def colored_prompt() -> str:
    """Return a colored user prompt prefix."""
    return f"{CYAN}{BOLD}你 > {RESET}"


def print_assistant(text: str) -> None:
    """Print assistant response."""
    print(f"\n{GREEN}{BOLD}助手:{RESET} {text}\n")


def print_info(text: str) -> None:
    """Print informational text."""
    print(f"{DIM}{text}{RESET}")


def print_warn(text: str) -> None:
    """Print a warning."""
    print(f"{YELLOW}{text}{RESET}")


def print_error(text: str) -> None:
    """Print an error."""
    print(f"{RED}{text}{RESET}")


def print_section(title: str) -> None:
    """Print a section header."""
    print(f"\n{MAGENTA}{BOLD}--- {title} ---{RESET}")
