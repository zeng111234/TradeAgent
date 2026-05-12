"""TradeAgent - FastAPI application entry point."""
import logging
from contextlib import asynccontextmanager

from fastapi import FastAPI
from fastapi.middleware.cors import CORSMiddleware

from app.config import settings
from app.database import init_db
from app.routers import customers, emails, tasks, analytics, agent

# Logging
logging.basicConfig(level=logging.INFO)
logger = logging.getLogger(__name__)


@asynccontextmanager
async def lifespan(app: FastAPI):
    """Application lifespan events."""
    logger.info("Starting TradeAgent...")
    # Initialize database
    await init_db()
    logger.info("Database initialized.")
    # Insert demo data if empty
    await _insert_demo_data()
    logger.info("TradeAgent is ready!")
    yield
    logger.info("Shutting down TradeAgent...")


app = FastAPI(
    title=settings.APP_NAME,
    version=settings.APP_VERSION,
    description="AI-powered foreign trade customer acquisition and email marketing assistant",
    lifespan=lifespan,
)

# CORS
app.add_middleware(
    CORSMiddleware,
    allow_origins=settings.CORS_ORIGINS,
    allow_credentials=True,
    allow_methods=["*"],
    allow_headers=["*"],
)

# Register routers
app.include_router(customers.router, prefix="/api/v1")
app.include_router(emails.router, prefix="/api/v1")
app.include_router(tasks.router, prefix="/api/v1")
app.include_router(analytics.router, prefix="/api/v1")
app.include_router(agent.router, prefix="/api/v1")


@app.get("/")
async def root():
    """Root endpoint."""
    return {
        "name": settings.APP_NAME,
        "version": settings.APP_VERSION,
        "docs": "/docs",
        "message": "TradeAgent - AI Foreign Trade Customer Acquisition Assistant",
    }


@app.get("/health")
async def health():
    """Health check endpoint."""
    return {"status": "ok"}


async def _insert_demo_data():
    """Insert demo data if the database is empty."""
    from sqlalchemy import select, func
    from app.database import async_session
    from app.models.customer import Customer, CustomerContact, CustomerNote
    from app.models.email_template import EmailTemplate
    from app.models.task import Task

    async with async_session() as db:
        # Check if data exists
        result = await db.execute(select(func.count(Customer.id)))
        count = result.scalar()
        if count > 0:
            return

        logger.info("Inserting demo data...")

        # Demo customers
        customers_data = [
            Customer(
                company_name="ABC Trading Co., Ltd",
                country="United States",
                city="Los Angeles",
                website="https://www.abctrading.com",
                industry="Home & Garden",
                products="furniture, home decor, kitchenware",
                source="scraper",
                stage="interested",
                score=85.0,
                tags="vip,home-garden",
                annual_import_value="$500,000+",
            ),
            Customer(
                company_name="EuroGoods GmbH",
                country="Germany",
                city="Hamburg",
                website="https://www.eurogoods.de",
                industry="Consumer Electronics",
                products="LED lights, smart devices, chargers",
                source="import",
                stage="quoting",
                score=72.0,
                tags="europe,electronics",
                annual_import_value="$300,000+",
            ),
            Customer(
                company_name="Mediterranean Imports S.L.",
                country="Spain",
                city="Barcelona",
                website="https://www.medimports.es",
                industry="Food & Beverage",
                products="packaged food, beverages, snacks",
                source="exhibition",
                stage="new",
                score=45.0,
                tags="food,spain",
            ),
            Customer(
                company_name="JapanTech Solutions",
                country="Japan",
                city="Tokyo",
                website="https://www.japantech.co.jp",
                industry="Automotive Parts",
                products="auto parts, rubber seals, bearings",
                source="alibaba",
                stage="contacted",
                score=68.0,
                tags="automotive,japan",
                annual_import_value="$1,200,000+",
            ),
            Customer(
                company_name="Ningbo Sunshine Electronics",
                company_name_cn="Ningbo Sunshine Electronics Co., Ltd",
                country="China",
                city="Ningbo",
                industry="Electronics Manufacturing",
                products="circuit boards, connectors, cables",
                source="referral",
                stage="completed",
                score=95.0,
                tags="local,nb,partner",
                annual_import_value="$2,000,000+",
            ),
        ]

        for c in customers_data:
            db.add(c)
        await db.flush()

        # Add contacts
        contacts_data = [
            CustomerContact(customer_id=customers_data[0].id, name="John Smith", title="Purchasing Manager", email="john@abctrading.com", phone="+1-310-555-0100", is_primary=1),
            CustomerContact(customer_id=customers_data[1].id, name="Hans Mueller", title="Import Director", email="hans@eurogoods.de", phone="+49-40-555-0200", is_primary=1),
            CustomerContact(customer_id=customers_data[3].id, name="Yuki Tanaka", title="Sourcing Manager", email="yuki@japantech.co.jp", phone="+81-3-5555-0400", is_primary=1),
        ]
        for c in contacts_data:
            db.add(c)

        # Add notes
        notes_data = [
            CustomerNote(customer_id=customers_data[0].id, content="Met at Canton Fair 2025, very interested in our new product line", note_type="meeting"),
            CustomerNote(customer_id=customers_data[0].id, content="Sent product catalog via email, waiting for feedback", note_type="email"),
            CustomerNote(customer_id=customers_data[1].id, content="Requested samples for LED panel lights, 3 variants", note_type="quotation"),
        ]
        for n in notes_data:
            db.add(n)

        # Demo templates
        templates = [
            EmailTemplate(
                name="Cold Outreach - General",
                subject="Quality {product_name} from {company_name}",
                body="<p>Dear {contact_name},</p><p>We are {company_name}, a leading manufacturer of {product_name} based in Ningbo, China.</p><p>We would love to discuss how we can support your business with high-quality products at competitive prices.</p><p>Best regards,<br/>{company_name}</p>",
                language="en",
                category="cold_outreach",
            ),
            EmailTemplate(
                name="Follow-up Template",
                subject="Following up - {product_name} inquiry",
                body="<p>Dear {contact_name},</p><p>I hope this message finds you well. I wanted to follow up on our previous correspondence regarding {product_name}.</p><p>Would you have time for a quick call this week?</p><p>Best regards,<br/>{company_name}</p>",
                language="en",
                category="follow_up",
            ),
        ]
        for t in templates:
            db.add(t)

        # Demo tasks
        tasks_data = [
            Task(title="Follow up with ABC Trading on LED panel samples", customer_id=customers_data[0].id, priority="high", task_type="follow_up"),
            Task(title="Send updated price list to EuroGoods", customer_id=customers_data[1].id, priority="medium", task_type="quotation"),
            Task(title="Prepare Canton Fair meeting notes", priority="low", task_type="meeting"),
        ]
        for t in tasks_data:
            db.add(t)

        await db.commit()
        logger.info("Demo data inserted successfully!")