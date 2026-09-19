import pytest
from conftest import set_frozen

from facebook_ad_library.config import settings
from facebook_ad_library.proxy import proxy_url


def test_unset_is_none():
    assert proxy_url("") is None and proxy_url(None) is None


def test_plain_url_passes_through():
    assert proxy_url("http://user:pass@gw.example.com:10001") == "http://user:pass@gw.example.com:10001"


def test_four_field_form_is_percent_encoded():
    assert proxy_url("gw.dataimpulse.com:10001:user__cr.us:p%ss:w@rd") == "http://user__cr.us:p%25ss%3Aw%40rd@gw.dataimpulse.com:10001"


@pytest.mark.parametrize("raw", ["http://u:p@us.decodo.com:7000", "us.decodo.com:7000:u:p", "gw.dataimpulse.com:823:u:p", "http://u:p@gw.dataimpulse.com:823"])
def test_rotating_gateways_are_refused(raw):
    with pytest.raises(ValueError, match="rotating gateway"):
        proxy_url(raw)


def test_too_few_fields_is_a_config_fault():
    with pytest.raises(ValueError):
        proxy_url("host:10001:user")


def test_default_reads_settings():
    set_frozen(settings, "proxy", "host:10001:u:p")
    try:
        assert proxy_url() == "http://u:p@host:10001"
    finally:
        set_frozen(settings, "proxy", None)
