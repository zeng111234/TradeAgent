"""Email sending utility with SMTP support and tracking."""
import uuid
import aiosmtplib
from email.mime.text import MIMEText
from email.mime.multipart import MIMEMultipart
from jinja2 import Template
import logging

from app.config import settings

logger = logging.getLogger(__name__)

# In-memory tracking pixel store (replace with DB in production)
tracking_pixels: dict[str, dict] = {}


def generate_tracking_id() -> str:
    """Generate a unique tracking ID for email tracking."""
    return str(uuid.uuid4())


def render_template(template_str: str, variables: dict) -> str:
    """Render a Jinja2 template string with variables."""
    try:
        template = Template(template_str)
        return template.render(**variables)
    except Exception as e:
        logger.error(f"Template render error: {e}")
        return template_str


def create_tracking_pixel(tracking_id: str) -> str:
    """Create an HTML tracking pixel for open tracking."""
    # This pixel URL would be served by your tracking endpoint
    tracking_url = f"/api/v1/emails/track/{tracking_id}/pixel"
    return f'<img src="{tracking_url}" width="1" height="1" style="display:none;" />'


async def send_email(
    to_email: str,
    to_name: str,
    subject: str,
    body: str,
    tracking_id: str | None = None,
) -> dict:
    """Send an email via SMTP.
    
    Returns dict with 'success' bool and optional 'error' message.
    """
    if not settings.SMTP_USER or not settings.SMTP_PASSWORD:
        logger.warning("SMTP credentials not configured, simulating email send")
        return {
            "success": True,
            "simulated": True,
            "message": "Email simulated (SMTP not configured)",
        }

    try:
        msg = MIMEMultipart("alternative")
        msg["From"] = settings.SMTP_USER
        msg["To"] = f"{to_name} <{to_email}>" if to_name else to_email
        msg["Subject"] = subject

        # Add tracking pixel if tracking_id provided
        html_body = body
        if tracking_id:
            tracking_pixel = create_tracking_pixel(tracking_id)
            html_body = body + tracking_pixel

        # Plain text version
        msg.attach(MIMEText(body, "plain", "utf-8"))
        # HTML version
        msg.attach(MIMEText(html_body, "html", "utf-8"))

        await aiosmtplib.send(
            msg,
            hostname=settings.SMTP_HOST,
            port=settings.SMTP_PORT,
            username=settings.SMTP_USER,
            password=settings.SMTP_PASSWORD,
            use_tls=settings.SMTP_USE_TLS,
        )

        logger.info(f"Email sent to {to_email}")
        return {"success": True, "simulated": False}

    except Exception as e:
        logger.error(f"Failed to send email to {to_email}: {e}")
        return {"success": False, "error": str(e)}


async def send_batch_emails(
    recipients: list[dict],
    subject_template: str,
    body_template: str,
) -> list[dict]:
    """Send emails to multiple recipients with template rendering.
    
    Each recipient dict should have: to_email, to_name, and any template variables.
    Returns list of results.
    """
    results = []
    for recipient in recipients:
        variables = {k: v for k, v in recipient.items() if k not in ("to_email", "to_name")}
        subject = render_template(subject_template, variables)
        body = render_template(body_template, variables)

        result = await send_email(
            to_email=recipient["to_email"],
            to_name=recipient.get("to_name", ""),
            subject=subject,
            body=body,
        )
        results.append({
            "to_email": recipient["to_email"],
            **result,
        })

    return results