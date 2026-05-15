"""Notification module - send messages to user.

Phase 1: print to console
Phase 2: send via email
Phase 3: send via WeChat/DingTalk webhook
"""
import logging
from datetime import datetime

logger = logging.getLogger(__name__)


def send_notification(title: str, body: str, level: str = "info") -> bool:
    """Send a notification to the user.

    Args:
        title: Notification title
        body: Notification body (supports plain text or HTML)
        level: info / warning / action_required

    Returns:
        True if sent successfully
    """
    timestamp = datetime.now().strftime("%Y-%m-%d %H:%M:%S")

    # Phase 1: Console output (always works)
    print("\n" + "=" * 60)
    print(f"[{timestamp}] {level.upper()}: {title}")
    print("=" * 60)
    print(body)
    print("=" * 60 + "\n")

    # Log it too
    logger.info(f"Notification sent: {title}")

    # Phase 2: Email (future)
    # from app.utils.email_sender import send_email
    # send_email(...)

    # Phase 3: WeChat/DingTalk webhook (future)
    # requests.post(webhook_url, json={"msgtype": "text", ...})

    return True