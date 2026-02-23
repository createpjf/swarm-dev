"""Hello World — example plugin for Cleo agent stack.

Demonstrates how to hook into task lifecycle and message events.
"""


def on_task_completed(task_id: str, result: str = "", **kwargs):
    """Called when any task finishes execution."""
    print(f"[hello-world] Task {task_id} completed")


def on_message_received(channel: str, text: str = "", **kwargs):
    """Called when a message arrives from any channel."""
    print(f"[hello-world] Message from {channel}: {text[:50]}")
