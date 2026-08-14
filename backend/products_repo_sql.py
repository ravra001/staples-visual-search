"""
Cloud SQL (Postgres) repository — the DATA_BACKEND=sql implementation.

Mirrors the in-memory repository's four functions exactly, so main.py is
unchanged when you switch backends. Point DATABASE_URL at your Cloud SQL
instance, e.g.:

    postgresql+psycopg://user:pass@host:5432/staples
    # or via the Cloud SQL Python Connector / unix socket in production

Then seed it once:

    DATA_BACKEND=sql DATABASE_URL=... python -c "import products_repo_sql as r; r.init_and_seed()"

Requires SQLAlchemy + a driver (see requirements-ml.txt). This module is only
imported when DATA_BACKEND=sql, so the default path never needs these installed.
"""
import os

from sqlalchemy import (
    Column, Float, Integer, String, Text, create_engine, or_, func,
)
from sqlalchemy.orm import declarative_base, sessionmaker

import config

DATABASE_URL = config.DATABASE_URL
if not DATABASE_URL:
    raise RuntimeError("data.backend=sql requires data.database_url in config.yaml (or DATABASE_URL).")

engine = create_engine(DATABASE_URL, pool_pre_ping=True, future=True)
SessionLocal = sessionmaker(bind=engine, future=True)
Base = declarative_base()


class Product(Base):
    __tablename__ = "products"

    sku = Column(String(32), primary_key=True)
    name = Column(String(255), nullable=False)
    brand = Column(String(80), default="")
    category = Column(String(40), index=True, nullable=False)
    price = Column(Float, nullable=False)
    list_price = Column(Float, nullable=False)
    rating = Column(Float, default=0.0)
    reviews = Column(Integer, default=0)
    description = Column(Text, default="")

    def as_dict(self):
        return {
            "sku": self.sku,
            "name": self.name,
            "brand": self.brand,
            "category": self.category,
            "price": self.price,
            "list_price": self.list_price,
            "rating": self.rating,
            "reviews": self.reviews,
            "description": self.description,
        }


def get_all_products():
    with SessionLocal() as s:
        return [p.as_dict() for p in s.query(Product).all()]


def get_product_by_sku(sku):
    with SessionLocal() as s:
        p = s.get(Product, sku)
        return p.as_dict() if p else None


def get_products_by_category(category):
    with SessionLocal() as s:
        rows = s.query(Product).filter(Product.category == category).all()
        return [p.as_dict() for p in rows]


def search_products(query):
    q = (query or "").strip()
    if not q:
        return []
    with SessionLocal() as s:
        # AND across terms, each matched against name/brand/description.
        stmt = s.query(Product)
        for term in q.lower().split():
            like = f"%{term}%"
            stmt = stmt.filter(or_(
                func.lower(Product.name).like(like),
                func.lower(Product.brand).like(like),
                func.lower(Product.category).like(like),
                func.lower(Product.description).like(like),
            ))
        return [p.as_dict() for p in stmt.all()]


def init_and_seed():
    """Create the schema and load it from the in-memory PRODUCTS list (one-time)."""
    from products_data import PRODUCTS  # in-memory seed source
    Base.metadata.create_all(engine)
    with SessionLocal() as s:
        for p in PRODUCTS:
            if not s.get(Product, p["sku"]):
                s.add(Product(**{k: p.get(k) for k in (
                    "sku", "name", "brand", "category", "price",
                    "list_price", "rating", "reviews", "description")}))
        s.commit()
    print(f"[sql] seeded {len(PRODUCTS)} products into {engine.url.database}")
