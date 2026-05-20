"""
TradeAgent Daily Lead Scan Script (v3)
- Searches globally across 15+ textile importing countries
- Uses ALL keywords (not just the first one)
- Skips previously scanned domains
- AI-generates personalized draft emails using page content
- Saves structured JSON for Web platform import
- Drafts stay in report for human review (not auto-sent)
- Target: ~10 new leads per day
"""
import json
import re
import os
import smtplib
from email.mime.text import MIMEText
from email.mime.multipart import MIMEMultipart
from datetime import datetime

SCRIPT_DIR = os.path.dirname(os.path.abspath(__file__))
SCANNED_FILE = os.path.join(SCRIPT_DIR, "scanned_domains.json")
LEADS_JSON_FILE = os.path.join(SCRIPT_DIR, "daily_leads.json")
TARGET_NEW_LEADS = 10

# Global textile importing countries
GLOBAL_COUNTRIES = [
    "Germany", "United States", "United Kingdom", "France", "Italy",
    "Spain", "Australia", "India", "Japan", "South Korea",
    "Brazil", "Turkey", "Netherlands", "Canada", "Mexico",
]

# Domain scan expiration: re-scan after 7 days
SCAN_EXPIRE_DAYS = 7


def load_scanned_domains() -> dict:
    """Load previously scanned domains with timestamps from file.
    
    Returns a dict: {domain: iso_timestamp}
    Domains older than SCAN_EXPIRE_DAYS are automatically pruned.
    """
    from datetime import timedelta
    now = datetime.now()
    expire_cutoff = now - timedelta(days=SCAN_EXPIRE_DAYS)

    if os.path.exists(SCANNED_FILE):
        try:
            with open(SCANNED_FILE, "r") as f:
                data = json.load(f)
                raw_domains = data.get("domains", [])

                # Handle old format (list of strings) -> convert to dict with timestamps
                if raw_domains and isinstance(raw_domains[0], str):
                    scanned = {d: data.get("updated", now.isoformat()) for d in raw_domains}
                else:
                    scanned = {d.get("domain", ""): d.get("scanned_at", now.isoformat()) for d in raw_domains if d.get("domain")}

                # Prune expired domains
                pruned = {}
                expired_count = 0
                for domain, ts in scanned.items():
                    try:
                        scan_date = datetime.fromisoformat(ts)
                        if scan_date > expire_cutoff:
                            pruned[domain] = ts
                        else:
                            expired_count += 1
                    except (ValueError, TypeError):
                        # Keep if can't parse date
                        pruned[domain] = ts

                if expired_count > 0:
                    print(f"  [INFO] Pruned {expired_count} expired domains (>{SCAN_EXPIRE_DAYS} days old)")
                    # Save pruned list immediately
                    save_scanned_domains(pruned)

                return pruned
        except Exception:
            return {}
    return {}


def save_scanned_domains(scanned: dict):
    """Save the full scanned domains dict to file."""
    try:
        domain_list = [{"domain": d, "scanned_at": ts} for d, ts in scanned.items()]
        with open(SCANNED_FILE, "w") as f:
            json.dump({
                "domains": domain_list,
                "updated": datetime.now().isoformat(),
                "total": len(domain_list),
            }, f, indent=2)
    except Exception as e:
        print(f"  [WARN] Could not save scanned domains: {e}")


def save_scanned_domain(domain: str, scanned: dict):
    """Add a domain to the scanned dict and save."""
    scanned[domain] = datetime.now().isoformat()
    save_scanned_domains(scanned)


def search_leads(product_keyword: str, target_country: str, max_results: int = 5, already_scanned: dict = None) -> list:
    """Search for potential buyers using ddgs for one keyword + one country."""
    from ddgs import DDGS

    already_scanned = already_scanned or {}

    queries = [
        f"{product_keyword} buyer {target_country}",
        f"{product_keyword} importer {target_country}",
        f"{product_keyword} wholesaler {target_country}",
        f"{product_keyword} distributor {target_country}",
        f"{product_keyword} retailer {target_country}",
        f'"{product_keyword}" company {target_country}',
    ]

    seen_domains = set()
    leads = []

    ddgs = DDGS()
    for query in queries:
        try:
            results = list(ddgs.text(query, max_results=max_results))
            for r in results:
                href = r.get("href", "")
                if href and href.startswith("http"):
                    domain = href.split("/")[2] if len(href.split("/")) > 2 else href
                    if domain not in seen_domains and domain not in already_scanned:
                        seen_domains.add(domain)
                        leads.append({
                            "company_name": r.get("title", "Unknown").split(" - ")[0].strip()[:100],
                            "website": href,
                            "snippet": r.get("body", "")[:200],
                            "domain": domain,
                            "keyword": product_keyword,
                            "country": target_country,
                        })
        except Exception as e:
            print(f"  [WARN] Search error for '{query}': {e}")

    return leads


def scrape_lead_details(leads: list) -> list:
    """Visit each lead's website and extract emails, company info."""
    import requests
    from bs4 import BeautifulSoup

    headers = {
        "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36",
    }

    enriched = []
    b2b_platforms = ["exporthub.com", "tradekey.com", "alibaba.com", "made-in-china.com",
                     "globalsources.com", "volza.com", "fibre2fashion.com"]

    for lead in leads[:20]:
        try:
            resp = requests.get(lead["website"], headers=headers, timeout=10, verify=False)
            raw_html = resp.text
            soup = BeautifulSoup(raw_html, "lxml")

            # Extract emails FIRST from raw HTML (before removing footer/header)
            # Footer and header often contain contact info
            all_emails = list(set(re.findall(
                r'[a-zA-Z0-9._%+-]+@[a-zA-Z0-9.-]+\.[a-zA-Z]{2,}', raw_html)))
            all_emails = [e for e in all_emails if not any(x in e.lower() for x in
                ["noreply", "example.com", "sentry", "wixpress", "wordpress",
                 ".png", ".jpg", ".gif", ".css", ".js"])]

            # Also extract emails specifically from footer/header areas
            for section in soup.find_all(["footer", "header"]):
                section_emails = re.findall(
                    r'[a-zA-Z0-9._%+-]+@[a-zA-Z0-9.-]+\.[a-zA-Z]{2,}',
                    section.get_text(separator=" "))
                all_emails.extend(section_emails)
            all_emails = list(set(all_emails))

            # Now clean up the page for text extraction
            for tag in soup(["script", "style", "nav"]):
                tag.decompose()
            # Keep footer/header text for page_text (contact info is valuable)
            page_text = soup.get_text(separator=" ", strip=True)[:8000]
            text_lower = page_text.lower()

            emails = all_emails

            # Score relevance
            score = 20
            is_b2b = any(p in lead["website"].lower() for p in b2b_platforms)
            if is_b2b:
                score += 30
            buyer_kw = ["import", "buyer", "wholesale", "distributor", "trade", "supplier"]
            if any(kw in text_lower for kw in buyer_kw):
                score += 20
            product_parts = lead.get("keyword", "textile").lower().split()
            if any(part in text_lower for part in product_parts if len(part) > 3):
                score += 20
            if emails:
                score += 10

            if score >= 40:
                lead["emails"] = emails[:3]
                lead["relevance_score"] = min(score, 100)
                lead["page_text_preview"] = page_text[:3000]
                enriched.append(lead)
        except Exception:
            pass

    enriched.sort(key=lambda x: x.get("relevance_score", 0), reverse=True)
    return enriched


def generate_draft_email(lead: dict) -> str:
    """Generate a personalized draft email for a lead.
    
    Uses AI with full page context for genuine personalization.
    Does NOT send - returns the draft text only.
    """
    api_key = os.environ.get("OPENAI_API_KEY", "")
    base_url = os.environ.get("OPENAI_BASE_URL", "https://api.xiaomimimo.com/v1")
    model = os.environ.get("OPENAI_MODEL", "mimo-v2.5")

    company = lead.get("company_name", "your company")
    website = lead.get("website", "")
    snippet = lead.get("snippet", "")
    keyword = lead.get("keyword", "textile products")
    country = lead.get("country", "your region")
    page_text = lead.get("page_text_preview", "")

    print(f"  [DEBUG] API key present: {bool(api_key)}, base_url: {base_url}, model: {model}")
    if api_key:
        try:
            from openai import OpenAI
            client = OpenAI(api_key=api_key, base_url=base_url)

            prompt = f"""Write a personalized cold outreach email (120-180 words) from a Chinese textile supplier.

About the target company:
- Company name: {company}
- Website: {website}
- Country: {country}
- Search snippet: {snippet[:200]}
- What their website says: {page_text[:1500]}

Your product: {keyword} (from Ningbo, China)

CRITICAL RULES:
1. Reference SOMETHING SPECIFIC from their website (their products, market, customers, niche, etc.)
2. Explain WHY your {keyword} is relevant to THEIR specific business and how it fits their product line
3. Mention product advantages: quality consistency, competitive MOQ, fast delivery from Ningbo
4. Offer a concrete next step: free samples, catalog with color card, or small trial order
5. Sound natural and human, NOT like a template
6. Professional but warm tone
7. DO NOT use phrases like "I hope this email finds you well" or "We are a leading manufacturer"
8. Include your company's flexibility on customization (colors, specs, packaging)

Return ONLY the email body (no subject line, no JSON)."""

            resp = client.chat.completions.create(
                model=model,
                messages=[
                    {"role": "system", "content": "You write personalized B2B cold emails for a Chinese textile supplier based in Ningbo. Each email must be 120-180 words, reference specific details about the recipient's business, and include product benefits, customization options, and a concrete next step."},
                    {"role": "user", "content": prompt},
                ],
                temperature=0.8,
                max_tokens=500,
            )
            result = resp.choices[0].message.content.strip()
            print(f"  [OK] AI draft generated ({len(result)} chars)")
            return result
        except Exception as e:
            print(f"  [ERROR] AI draft FAILED for {company}: {type(e).__name__}: {e}")
            # Print more details for debugging
            import traceback
            traceback.print_exc()

    # Fallback: smarter template with page-based context
    page_lower = page_text.lower()
    business_context = ""
    if "embroidery" in page_lower or "needlework" in page_lower:
        business_context = "your embroidery and needlework products"
    elif "fashion" in page_lower or "clothing" in page_lower or "garment" in page_lower:
        business_context = "your fashion and garment business"
    elif "quilt" in page_lower or "craft" in page_lower:
        business_context = "your quilting and craft supplies"
    elif "upholstery" in page_lower or "interior" in page_lower or "decor" in page_lower:
        business_context = "your interior decoration products"
    elif "import" in page_lower or "trade" in page_lower or "distribution" in page_lower:
        business_context = "your distribution network in the textile market"
    else:
        business_context = f"your business in {country}"

    return f"""Dear {company},

I came across your company and was impressed by {business_context}. We are a specialized manufacturer of {keyword} based in Ningbo, China, and I believe our products could be a strong fit for your product range.

We offer consistent quality, competitive pricing, and flexible order quantities. I'd love to send you our latest product catalog and samples so you can evaluate them firsthand.

Would you be interested in exploring this further?

Best regards"""


def build_report(leads: list, keywords_used: list, countries_scanned: list, skipped_count: int) -> str:
    """Build a human-readable morning report with draft emails."""
    date = datetime.now().strftime("%Y-%m-%d")
    lines = []
    lines.append(f"Good morning! TradeAgent Daily Report for {date}")
    lines.append(f"Keywords: {', '.join(keywords_used)}")
    lines.append(f"Regions: {', '.join(countries_scanned[:5])}{'...' if len(countries_scanned) > 5 else ''}")
    lines.append(f"Previously scanned (skipped): {skipped_count} domains")
    lines.append("=" * 60)
    lines.append("")

    if not leads:
        lines.append("[New Leads] No new leads found today.")
        lines.append("")
        lines.append("Tips: Try different keywords or expand target countries.")
    else:
        lines.append(f"[New Leads] Found {len(leads)} NEW potential customers:")
        lines.append("")
        for i, lead in enumerate(leads[:TARGET_NEW_LEADS], 1):
            score = lead.get("relevance_score", 0)
            emails = lead.get("emails", [])
            keyword = lead.get("keyword", "")
            country = lead.get("country", "")
            email_str = f" | Email: {', '.join(emails)}" if emails else " | No email found"
            lines.append(f"  {i}. {lead['company_name']}")
            lines.append(f"     Website: {lead['website']}")
            lines.append(f"     Found via: '{keyword}' in {country}")
            lines.append(f"     Score: {score}/100{email_str}")
            if lead.get("snippet"):
                lines.append(f"     Info: {lead['snippet'][:120]}")

            # Draft email section
            draft = lead.get("draft_email", "")
            if draft:
                lines.append("")
                lines.append(f"     --- DRAFT EMAIL (NOT SENT - REVIEW FIRST) ---")
                for dline in draft.split("\n"):
                    lines.append(f"     {dline}")
                lines.append(f"     --- END DRAFT ---")
            lines.append("")

    lines.append("=" * 60)
    lines.append("ALL drafts above are for REVIEW ONLY. Nothing was sent automatically.")
    lines.append("")
    lines.append(f"Structured data saved to: {LEADS_JSON_FILE}")
    lines.append("Import to Web platform: TradeAgent > Dashboard > Import Today's Leads")
    lines.append("Or send drafts manually: TradeAgent > Emails > Pending Review")
    lines.append("")
    lines.append("This report was generated automatically by TradeAgent.")
    lines.append("https://github.com/zeng111234/TradeAgent")
    return "\n".join(lines)


def save_leads_json(leads: list, keywords_used: list, countries_scanned: list):
    """Save leads as structured JSON for web platform import."""
    output = {
        "scan_date": datetime.now().isoformat(),
        "keywords_used": keywords_used,
        "countries_scanned": countries_scanned,
        "total_found": len(leads),
        "leads": []
    }

    for lead in leads:
        email_list = lead.get("emails", [])
        output["leads"].append({
            "company_name": lead.get("company_name", "Unknown"),
            "website": lead.get("website", ""),
            "country": lead.get("country", ""),
            "keyword": lead.get("keyword", ""),
            "snippet": lead.get("snippet", ""),
            "emails": email_list,
            "relevance_score": lead.get("relevance_score", 0),
            "page_text_preview": lead.get("page_text_preview", "")[:500],
            "draft_email": lead.get("draft_email", ""),
            "draft_subject": f"Partnership Inquiry - Quality {lead.get('keyword', 'Textile Products')} from China",
        })

    try:
        with open(LEADS_JSON_FILE, "w", encoding="utf-8") as f:
            json.dump(output, f, indent=2, ensure_ascii=False)
        print(f"[OK] Structured leads saved to {LEADS_JSON_FILE}")
    except Exception as e:
        print(f"[ERROR] Could not save leads JSON: {e}")


def send_email_report(report: str, leads: list):
    """Send the daily report via email."""
    smtp_host = os.environ.get("SMTP_HOST", "smtp.qq.com")
    smtp_port = int(os.environ.get("SMTP_PORT", "465"))
    smtp_user = os.environ.get("SMTP_USER", "")
    smtp_password = os.environ.get("SMTP_PASSWORD", "")
    recipient = os.environ.get("REPORT_EMAIL", smtp_user)

    if not smtp_user or not smtp_password:
        print("[INFO] SMTP not configured, printing report to console only.")
        print(report)
        return

    msg = MIMEMultipart("alternative")
    msg["Subject"] = f"TradeAgent Daily Report - {datetime.now().strftime('%Y-%m-%d')} ({len(leads)} new leads)"
    msg["From"] = smtp_user
    msg["To"] = recipient

    msg.attach(MIMEText(report, "plain", "utf-8"))

    # HTML version
    html = report.replace("\n", "<br>").replace("  ", "&nbsp;&nbsp;")
    html_body = f'<div style="font-family: monospace; font-size: 13px; max-width: 700px;">{html}</div>'
    msg.attach(MIMEText(html_body, "html", "utf-8"))

    try:
        with smtplib.SMTP_SSL(smtp_host, smtp_port) as server:
            server.login(smtp_user, smtp_password)
            server.sendmail(smtp_user, recipient, msg.as_string())
        print(f"[OK] Report emailed to {recipient}")
    except Exception as e:
        print(f"[ERROR] Failed to send email: {e}")
        print(report)


def main():
    print("=" * 60)
    print(f"TradeAgent Daily Scan v3 - {datetime.now().strftime('%Y-%m-%d %H:%M:%S')}")
    print("=" * 60)

    # Load config
    keywords_str = os.environ.get("SCAN_KEYWORDS", "embroidery thread, gold metallic yarn, metallic thread, gold yarn")
    countries_str = os.environ.get("SCAN_COUNTRIES", "")
    if countries_str:
        countries = [c.strip() for c in countries_str.split(",") if c.strip()]
    else:
        countries = GLOBAL_COUNTRIES

    keywords = [k.strip() for k in keywords_str.split(",") if k.strip()]

    # Load previously scanned domains
    scanned = load_scanned_domains()
    skipped_count = len(scanned)
    print(f"Previously scanned: {skipped_count} domains (will skip)")

    all_leads = []
    seen_this_run = set()

    # Search with EACH keyword in EACH country
    for keyword in keywords:
        for country in countries:
            if len(all_leads) >= TARGET_NEW_LEADS * 2:
                break
            print(f"\n--- Searching: '{keyword}' in {country} ---")
            raw = search_leads(keyword, country, max_results=5, already_scanned=scanned)
            print(f"  Found {len(raw)} new results")
            enriched = scrape_lead_details(raw)
            print(f"  {len(enriched)} relevant after filtering")

            for lead in enriched:
                domain = lead.get("domain", "")
                if domain not in seen_this_run and len(all_leads) < TARGET_NEW_LEADS * 2:
                    seen_this_run.add(domain)
                    all_leads.append(lead)

        if len(all_leads) >= TARGET_NEW_LEADS * 2:
            break

    # Take top N leads
    all_leads.sort(key=lambda x: x.get("relevance_score", 0), reverse=True)
    top_leads = all_leads[:TARGET_NEW_LEADS]

    print(f"\n=== Selected {len(top_leads)} new leads for today ===")

    # Generate draft emails for each lead
    print("\nGenerating personalized draft emails...")
    for lead in top_leads:
        company = lead.get("company_name", "Unknown")
        print(f"  Drafting for: {company}...")
        lead["draft_email"] = generate_draft_email(lead)

    # Mark all found domains as scanned (so we skip them tomorrow)
    for lead in top_leads:
        domain = lead.get("domain", "")
        if domain:
            save_scanned_domain(domain, scanned)

    print(f"\nTotal scanned domains now: {len(scanned)}")

    # Save structured JSON for web platform import
    save_leads_json(top_leads, keywords, countries)

    # Build and send report
    report = build_report(top_leads, keywords, countries, skipped_count)
    send_email_report(report, top_leads)

    print(f"\nDone! {len(top_leads)} new leads with draft emails.")
    return len(top_leads)


if __name__ == "__main__":
    count = main()
    print(f"\nLeads found: {count}")