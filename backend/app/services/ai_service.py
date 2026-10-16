"""AI service for email generation and customer analysis."""
import logging

from app.llm import (
    LLMBadResponse,
    LLMTruncated,
    LLMUnavailable,
    complete_text,
    is_configured,
)

logger = logging.getLogger(__name__)


async def generate_cold_email(
    product_name: str,
    target_industry: str | None = None,
    target_country: str | None = None,
    company_name: str | None = None,
    selling_points: str | None = None,
    tone: str = "professional",
    language: str = "en",
) -> dict:
    """Generate a cold outreach email using AI.

    Falls back to a template when the model cannot answer -- but says so. The
    returned mapping always carries ``ai_powered`` and, when it is False,
    ``fallback_reason``. Before this, a wrong API key produced a canned
    template that was indistinguishable from model output, on the path that
    ends in a real email to a real buyer.
    """
    if not is_configured():
        return _generate_with_template(
            product_name, target_industry, target_country,
            company_name, selling_points, tone, language,
            reason="no_api_key",
        )
    return await _generate_with_openai(
        product_name, target_industry, target_country,
        company_name, selling_points, tone, language,
    )


async def _generate_with_openai(
    product_name: str,
    target_industry: str | None,
    target_country: str | None,
    company_name: str | None,
    selling_points: str | None,
    tone: str,
    language: str,
) -> dict:
    """Generate email using the configured chat-completions provider."""
    prompt = f"""You are an experienced foreign trade sales expert. Write a cold outreach email to a potential buyer.

Product: {product_name}
Target Industry: {target_industry or 'General'}
Target Country: {target_country or 'Global'}
Your Company: {company_name or 'Our Company'}
Key Selling Points: {selling_points or 'High quality, competitive price, fast delivery'}
Tone: {tone}
Language: {language}

Requirements:
1. Write a compelling subject line
2. Keep the email concise (under 200 words)
3. Include a clear call-to-action
4. Be professional but friendly
5. Mention specific value propositions

Output format:
SUBJECT: [subject line]
BODY: [email body in HTML format]"""

    try:
        content = await complete_text(
            prompt,
            system="You are a professional foreign trade email copywriter.",
            temperature=0.7,
            max_tokens=500,
        )
    except LLMUnavailable:
        reason = "no_api_key"
    except LLMTruncated:
        # Half an email is not an email, and the caller offers this to a human
        # as something to send.
        reason = "model response was cut off at the token cap"
    except LLMBadResponse as exc:
        reason = f"model returned nothing usable: {exc}"
    except Exception as exc:                      # network, auth, rate limit
        reason = f"model call failed: {exc}"
    else:
        subject = ""
        body = ""
        if "SUBJECT:" in content and "BODY:" in content:
            parts = content.split("BODY:", 1)
            subject = parts[0].replace("SUBJECT:", "").strip()
            body = parts[1].strip()
        else:
            subject = f"Business Inquiry - {product_name}"
            body = content

        return {
            "subject": subject,
            "body": body,
            "suggestions": [
                "Consider personalizing with the recipient's company name",
                "Add a specific reference to their recent orders",
                "Include a time-limited offer to create urgency",
            ],
            "ai_powered": True,
            "fallback_reason": None,
        }

    logger.warning("cold email fell back to a template: %s", reason)
    return _generate_with_template(
        product_name, target_industry, target_country,
        company_name, selling_points, tone, language,
        reason=reason,
    )


def _generate_with_template(
    product_name: str,
    target_industry: str | None,
    target_country: str | None,
    company_name: str | None,
    selling_points: str | None,
    tone: str,
    language: str,
    reason: str = "template_requested",
) -> dict:
    """Generate email using template (fallback when the model is unavailable)."""
    company = company_name or "Our Company"
    industry = target_industry or "your industry"
    points = selling_points or "high quality products, competitive pricing, and reliable delivery"

    subject = f"Quality {product_name} Supplier - {company}"
    
    body = f"""<p>Dear Sir/Madam,</p>

<p>I hope this email finds you well. My name is [Your Name] from <strong>{company}</strong>, a professional manufacturer and exporter of <strong>{product_name}</strong>.</p>

<p>We noticed that your company is in the <strong>{industry}</strong> sector, and we believe our products would be an excellent fit for your needs. We specialize in providing:</p>

<ul>
<li>{points}</li>
</ul>

<p>With years of experience in international trade, we have served clients across {target_country or 'various countries'} and maintained a strong reputation for quality and reliability.</p>

<p>We would be delighted to:</p>
<ul>
<li>Provide detailed product specifications and pricing</li>
<li>Send free samples for your evaluation</li>
<li>Discuss customized solutions for your specific requirements</li>
</ul>

<p>Would you be available for a brief call this week to discuss potential cooperation?</p>

<p>Best regards,<br/>
[Your Name]<br/>
{company}<br/>
[Your Phone] | [Your Email]</p>"""

    return {
        "subject": subject,
        "body": body,
        "suggestions": [
            "Replace [Your Name], [Your Phone], [Your Email] with your actual info",
            "Customize the product details and selling points",
            "Add specific references to the recipient's business if possible",
            "Consider adding a link to your product catalog or website",
        ],
        "ai_powered": False,
        "fallback_reason": reason,
    }

