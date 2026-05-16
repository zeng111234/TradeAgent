"""
Standalone daily scan script for GitHub Actions.
Runs without FastAPI - uses ddgs for search, requests for scraping,
and SMTP for sending the daily report email.

This runs on GitHub's servers every day, so your computer can be off.
"""
import json
import re
import sys
import os
import smtplib
from email.mime.text import MIMEText
from email.mime.multipart import MIMEMultipart
from datetime import datetime


def search_leads(product_keywords: str, target_country: str, max_results: int = 10) -> list:
    """Search for potential buyers using ddgs."""
    from ddgs import DDGS

    queries = [
        f"{product_keywords} buyer {target_country}",
        f"{product_keywords} importer {target_country}",
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
                    if domain not in seen_domains:
                        seen_domains.add(domain)
                        leads.append({
                            "company_name": r.get("title", "Unknown").split(" - ")[0].strip()[:100],
                            "website": href,
                            "snippet": r.get("body", "")[:200],
                            "domain": domain,
                        })
        except Exception as e:
            print(f"  [WARN] Search error for '{query}': {e}")

    return leads


def scrape_lead_details(leads: list, product_keywords: str) -> list:
    """Visit each lead's website and extract emails, company info."""
    import requests
    from bs4 import BeautifulSoup

    headers = {
        "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36",
    }

    enriched = []
    b2b_platforms = ["exporthub.com", "tradekey.com", "alibaba.com", "made-in-china.com",
                     "globalsources.com", "volza.com", "fibre2fashion.com"]

    for lead in leads[:10]:
        try:
            resp = requests.get(lead["website"], headers=headers, timeout=10, verify=False)
            soup = BeautifulSoup(resp.text, "lxml")
            for tag in soup(["script", "style", "nav", "footer"]):
                tag.decompose()
            page_text = soup.get_text(separator=" ", strip=True)[:5000]
            text_lower = page_text.lower()

            # Extract emails
            emails = list(set(re.findall(r'[a-zA-Z0-9._%+-]+@[a-zA-Z0-9.-]+\.[a-zA-Z]{2,}', page_text)))
            emails = [e for e in emails if not any(x in e.lower() for x in
                ["noreply", "example.com", "sentry", "wixpress", "wordpress", ".png", ".jpg"])]

            # Score relevance
            score = 20
            is_b2b = any(p in lead["website"].lower() for p in b2b_platforms)
            if is_b2b:
                score += 30
            buyer_kw = ["import", "buyer", "wholesale", "distributor", "trade", "supplier"]
            if any(kw in text_lower for kw in buyer_kw):
                score += 20
            product_parts = product_keywords.lower().split()
            if any(part in text_lower for part in product_parts if len(part) > 3):
                score += 20
            if emails:
                score += 10

            if score >= 40:
                lead["emails"] = emails[:3]
                lead["relevance_score"] = min(score, 100)
                enriched.append(lead)
        except Exception as e:
            pass  # Skip failed sites

    enriched.sort(key=lambda x: x.get("relevance_score", 0), reverse=True)
    return enriched


def build_report(leads: list, search_keywords: str, target_countries: list) -> str:
    """Build a human-readable morning report."""
    date = datetime.now().strftime("%Y-%m-%d")
    lines = []
    lines.append(f"Good morning! TradeAgent Daily Report for {date}")
    lines.append(f"Search: '{search_keywords}' in {', '.join(target_countries)}")
    lines.append("=" * 60)
    lines.append("")

    if not leads:
        lines.append("[New Leads] No new leads found today.")
        lines.append("")
        lines.append("Tips: Try different keywords or expand target countries.")
    else:
        lines.append(f"[New Leads] Found {len(leads)} potential customers:")
        lines.append("")
        for i, lead in enumerate(leads[:10], 1):
            score = lead.get("relevance_score", 0)
            emails = lead.get("emails", [])
            email_str = f" | Email: {', '.join(emails)}" if emails else ""
            lines.append(f"  {i}. {lead['company_name']}")
            lines.append(f"     Website: {lead['website']}")
            lines.append(f"     Score: {score}/100{email_str}")
            if lead.get("snippet"):
                lines.append(f"     Info: {lead['snippet'][:120]}")
            lines.append("")

    lines.append("=" * 60)
    lines.append("This report was generated automatically by TradeAgent.")
    lines.append("https://github.com/zeng111234/TradeAgent")
    return "\n".join(lines)


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
    msg["Subject"] = f"TradeAgent Daily Report - {datetime.now().strftime('%Y-%m-%d')} ({len(leads)} leads)"
    msg["From"] = smtp_user
    msg["To"] = recipient

    # Plain text
    msg.attach(MIMEText(report, "plain", "utf-8"))

    # HTML version
    html = report.replace("\n", "<br>").replace("  ", "&nbsp;&nbsp;")
    html_body = f"""
    <div style="font-family: monospace; font-size: 13px; max-width: 700px;">
    {html}
    </div>
    """
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
    print(f"TradeAgent Daily Scan - {datetime.now().strftime('%Y-%m-%d %H:%M:%S')}")
    print("=" * 60)

    # Config from env or defaults
    keywords = os.environ.get("SCAN_KEYWORDS", "embroidery thread, gold metallic yarn")
    countries = os.environ.get("SCAN_COUNTRIES", "Germany, United States, United Kingdom").split(",")

    primary_keyword = keywords.split(",")[0].strip()
    all_leads = []

    for country in countries:
        country = country.strip()
        if not country:
            continue
        print(f"\n--- Scanning: '{primary_keyword}' in {country} ---")
        raw_leads = search_leads(primary_keyword, country, max_results=5)
        print(f"  Found {len(raw_leads)} raw results")
        enriched = scrape_lead_details(raw_leads, primary_keyword)
        print(f"  {len(enriched)} relevant leads after filtering")
        all_leads.extend(enriched)

    # Deduplicate by domain
    seen = set()
    unique_leads = []
    for lead in all_leads:
        domain = lead.get("domain", lead.get("website", ""))
        if domain not in seen:
            seen.add(domain)
            unique_leads.append(lead)

    print(f"\n=== Total: {len(unique_leads)} unique leads ===")

    report = build_report(unique_leads, primary_keyword, [c.strip() for c in countries])
    send_email_report(report, unique_leads)

    print("\nDone!")
    return len(unique_leads)


if __name__ == "__main__":
    count = main()
    print(f"\nLeads found: {count}")