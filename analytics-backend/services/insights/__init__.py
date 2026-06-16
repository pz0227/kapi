"""
Kapi Insights — business analytics engines for e-commerce / shop data.
"""
from .column_mapper import auto_detect_columns, apply_mapping, validate_mapping
from .shop_health import analyze_shop_health
from .product_performance import analyze_product_performance
from .trend_spotter import analyze_trends
from .recommendation_engine import generate_recommendations

ANALYSIS_MODES = {
    "shop_health": {
        "id": "shop_health",
        "label": "Shop Health Check",
        "description": "Revenue, orders, AOV, growth rate, top products, and overall business diagnosis.",
        "required_columns": ["revenue", "date"],
        "icon": "activity",
        "fn": analyze_shop_health,
    },
    "product_performance": {
        "id": "product_performance",
        "label": "Product Performance",
        "description": "Rank all products by composite score, identify stars, rising products, and dogs.",
        "required_columns": ["revenue", "product_name"],
        "icon": "package",
        "fn": analyze_product_performance,
    },
    "trend_spotter": {
        "id": "trend_spotter",
        "label": "Trend Spotter",
        "description": "WoW and MoM changes for revenue, orders, and AOV. Flags significant shifts.",
        "required_columns": ["revenue", "date"],
        "icon": "trending-up",
        "fn": analyze_trends,
    },
}

__all__ = [
    "auto_detect_columns",
    "apply_mapping",
    "validate_mapping",
    "analyze_shop_health",
    "analyze_product_performance",
    "analyze_trends",
    "generate_recommendations",
    "ANALYSIS_MODES",
]
