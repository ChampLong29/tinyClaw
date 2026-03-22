"""Concurrency module: named lanes with FIFO queues."""

from tinyclaw.concurrency.lane_queue import LaneQueue
from tinyclaw.concurrency.command_queue import CommandQueue

# Standard lane names
LANE_MAIN = "main"
LANE_CRON = "cron"
LANE_HEARTBEAT = "heartbeat"

__all__ = ["LaneQueue", "CommandQueue", "LANE_MAIN", "LANE_CRON", "LANE_HEARTBEAT"]
