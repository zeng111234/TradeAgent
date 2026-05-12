"""AI service for email generation and customer analysis."""
import logging
from app.config import settings

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
    
    Falls back to a template-based approach if OpenAI API is not configured.
    """
    if settings.OPENAI_API_KEY:
        return await _generate_with_openai(
            product_name, target_industry, target_country,
            company_name, selling_points, tone, language,
        )
    else:
        return _generate_with_template(
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
    """Generate email using OpenAI API."""
    try:
        from openai import AsyncOpenAI
        client = AsyncOpenAI(
            api_key=settings.OPENAI_API_KEY,
            base_url=settings.OPENAI_BASE_URL,
        )

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

        response = await client.chat.completions.create(
            model=settings.OPENAI_MODEL,
            messages=[
                {"role": "system", "content": "You are a professional foreign trade email copywriter."},
                {"role": "user", "content": prompt},
            ],
            temperature=0.7,
            max_tokens=500,
        )

        content = response.choices[0].message.content
        
        # Parse response
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
        }
    except Exception as e:
        logger.error(f"OpenAI API error: {e}")
        return _generate_with_template(
            product_name, target_industry, target_country,
            company_name, selling_points, tone, language,
        )


def _generate_with_template(
    product_name: str,
    target_industry: str | None,
    target_country: str | None,
    company_name: str | None,
    selling_points: str | None,
    tone: str,
    language: str,
) -> dict:
    """Generate email using template (fallback when no API key)."""
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
    }


async def analyze_customer_match(
    customer_products: str,
    your_products: str,
) -> dict:
    """Analyze how well a customer matches your product offering.
    Returns a score and explanation.
    """
    # Simple keyword-based matching (can be enhanced with AI)
    customer_keywords = set(customer_products.lower().split(",")) if customer_products else set()
    your_keywords = set(your_products.lower().split(",")) if your_products else set()
    
    if not customer_keywords or not your_keywords:
        return {"score": 50.0, "explanation": "Insufficient data for matching analysis"}
    
    # Clean up keywords
    customer_keywords = {k.strip() for k in customer_keywords if k.strip()}
    your_keywords = {k.strip() for k in your_keywords if k.strip()}
    
    # Calculate overlap
    common = customer_keywords & your_keywords
    if not common:
        return {
            "score": 20.0,
            "explanation": f"No direct keyword match found. Customer products: {customer_products}",
        }
    
    match_ratio = len(common) / max(len(customer_keywords), len(your_keywords))
    score = min(100, match_ratio * 100 + 20)
    
    return {
        "score": round(score, 1),
        "explanation": f"Matched keywords: {', '.join(common)}. Match ratio: {match_ratio:.0%}",
    }