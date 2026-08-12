# Run from /Ice-AI/python: pytest tests/ -q

import sys
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import MagicMock

import pytest

# ensure the flat module layout is importable
ROOT = Path(__file__).resolve().parent.parent
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))


@pytest.fixture
def prospect_listing_stub():
    """Minimal ProspectListings-like object for AI field parsing tests."""

    return SimpleNamespace(
        ai_resell_overview=None,
        ai_work_and_repairs=None,
        ai_resell_notes=None,
        ai_value_add_improvements=None,
        ai_campervan_conversion=None,
        ai_target_market=None,
        ai_buy_price_low=None,
        ai_buy_price_high=None,
        ai_repair_cost=None,
        ai_sell_price_low=None,
        ai_sell_price_high=None,
    )


@pytest.fixture
def mock_db_session():
    """SQLAlchemy session stand-in for persist/delete integration tests."""

    session = MagicMock()
    session.add = MagicMock()
    session.commit = MagicMock()
    session.flush = MagicMock()
    return session


@pytest.fixture
def mock_page():
    """Playwright Page stand-in for navigation/backoff tests."""

    return MagicMock()
