"""Production refuses every sandbox shortcut; sandbox must be chosen explicitly."""
import pytest

from backend.config import ConfigError, load_settings
from backend.services.payment_rail import rail_from_env

STRONG = "x" * 40
PROD = {"DATABASE_URL": "postgresql://u:p@db.example:5432/mandate", "MANDATE_RAIL": "qatom",
        "MANDATE_SELLER_MODE": "http", "MANDATE_OWNER_TOKEN": STRONG}


def test_production_is_the_default_and_accepts_a_real_configuration():
    s = load_settings(PROD)
    assert (s.mode, s.rail, s.seller_mode, s.sandbox) == ("production", "qatom", "http", False)


def test_production_with_nothing_set_lists_every_problem():
    with pytest.raises(ConfigError) as err:
        load_settings({})
    msg = str(err.value)
    for needle in ("DATABASE_URL", "MANDATE_RAIL", "MANDATE_SELLER_MODE", "MANDATE_OWNER_TOKEN"):
        assert needle in msg


@pytest.mark.parametrize("change,needle", [
    ({"MANDATE_RAIL": "mock"}, "sandbox rail"),
    ({"MANDATE_SELLER_MODE": "mock"}, "sandbox seller"),
    ({"DATABASE_URL": ":memory:"}, "persistent database"),
    ({"MANDATE_OWNER_TOKEN": "short"}, "at least 32"),
    ({"MANDATE_CORS_ORIGINS": "*"}, "cannot be '*'"),
    ({"MANDATE_RAIL": "paypal"}, "must be one of"),
])
def test_production_refuses_each_unsafe_setting(change, needle):
    with pytest.raises(ConfigError, match=needle):
        load_settings({**PROD, **change})


def test_cors_origins_split_on_commas_and_one_value_stays_one_value():
    assert load_settings(PROD).cors_origins == ("http://localhost:3000",)
    single = load_settings({**PROD, "MANDATE_CORS_ORIGINS": "http://localhost:3000"})
    assert single.cors_origins == ("http://localhost:3000",)
    several = load_settings({**PROD, "MANDATE_CORS_ORIGINS":
                             " https://app.mandatepay.tech, , https://market.mandatepay.tech "})
    assert several.cors_origins == ("https://app.mandatepay.tech", "https://market.mandatepay.tech")
    with pytest.raises(ConfigError, match="cannot be '\\*'"):
        load_settings({**PROD, "MANDATE_CORS_ORIGINS": "http://localhost:3000, *"})


def test_sandbox_is_explicit_and_keeps_data_on_disk_by_default():
    s = load_settings({"MANDATE_MODE": "sandbox"})
    assert (s.rail, s.seller_mode, s.database_url) == ("mock", "mock", "sqlite:///mandate-sandbox.db")


def test_unknown_mode_and_unknown_rail_are_errors_not_fallbacks():
    with pytest.raises(ConfigError, match="production or sandbox"):
        load_settings({"MANDATE_MODE": "demo"})
    with pytest.raises(ValueError, match="unknown payment rail"):
        rail_from_env("paypal")
