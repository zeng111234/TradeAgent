"""Email sending utility with SMTP support and tracking."""
import asyncio
import re
import uuid
import aiosmtplib
from email.mime.text import MIMEText
from email.mime.multipart import MIMEMultipart
from jinja2 import Template
import logging

from app.config import settings
from app.timeouts import SMTP_SEND_SECONDS

logger = logging.getLogger(__name__)

# In-memory tracking pixel store (replace with DB in production)
tracking_pixels: dict[str, dict] = {}

#: Warn once per process about dead pixels, not once per message.
_pixel_base_warned = False


def generate_tracking_id() -> str:
    """Generate a unique tracking ID for email tracking."""
    return str(uuid.uuid4())


def render_template(template_str: str, variables: dict) -> str:
    """Substitute a template's variables, in the two spellings people write.

    The editor in the Templates page tells the author to write ``{company_name}``
    and ships its seeded templates in that form; Jinja2 only understands
    ``{{ company_name }}``. Left as two dialects, every template saved through
    the UI mailed its placeholders out verbatim -- a mistake the sender sees
    only after the customer has. So a placeholder written either way is
    substituted, and one whose name is not a variable is left standing rather
    than blanked: an untouched ``{comapny_name}`` in a preview is a typo worth
    catching, while a silent gap is not.
    """
    variables = variables or {}
    try:
        rendered = Template(template_str or "").render(**variables)
    except Exception as e:
        logger.error(f"Template render error: {e}")
        rendered = template_str or ""

    def _sub(match: "re.Match[str]") -> str:
        name = match.group(1)
        if name not in variables:
            return match.group(0)
        value = variables[name]
        return "" if value is None else str(value)

    # The lookarounds keep this out of Jinja's own ``{{ name }}``: if the
    # Jinja pass above failed on syntax, its braces are still in the string and
    # substituting inside them would mangle the template instead of repairing
    # the dialect.
    return re.sub(r"(?<!\{)\{(\w+)\}(?!\})", _sub, rendered)


def create_tracking_pixel(tracking_id: str) -> str:
    """Create an HTML tracking pixel for open tracking.

    The URL has to be absolute. Inside a mail client there is no page origin,
    so a relative ``/api/v1/...`` src resolves against nothing -- the pixel is
    a dead link and no open is ever recorded. With ``PUBLIC_BASE_URL`` set the
    link points at this service; without it, the pixel is still emitted (the
    HTML stays valid) and the fact that opens cannot work is said out loud
    once, rather than leaving a silent gap.
    """
    global _pixel_base_warned
    base = settings.PUBLIC_BASE_URL.rstrip("/")
    if base:
        tracking_url = f"{base}/api/v1/emails/track/{tracking_id}/pixel"
    else:
        if not _pixel_base_warned:
            logger.warning(
                "PUBLIC_BASE_URL is not set: tracking pixels in outgoing mail "
                "are dead links and email opens will not be recorded"
            )
            _pixel_base_warned = True
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
            # Stated rather than inherited: aiosmtplib's own default is 60
            # seconds, and this call happens while the process-wide send lock is
            # held. Thirty is enough for a handshake and one message against a
            # healthy server, and it keeps a hung one from owning the queue.
            timeout=SMTP_SEND_SECONDS,
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