"""Tests for Telegram Mini App helpers."""

import hashlib
import hmac
import json
from urllib.parse import urlencode

from kodibot.telegram import webapp


def _signed_init_data(params: dict, token: str) -> str:
    data_check_string = "\n".join(f"{key}={value}" for key, value in sorted(params.items()))
    secret_key = hmac.new(b"WebAppData", token.encode("utf-8"), hashlib.sha256).digest()
    signature = hmac.new(
        secret_key,
        data_check_string.encode("utf-8"),
        hashlib.sha256,
    ).hexdigest()
    return urlencode({**params, "hash": signature})


class TestValidateWebAppInitData:
    def test_accepts_valid_payload(self):
        token = "123:ABC"
        params = {
            "auth_date": "1700000000",
            "query_id": "AAEAAAE",
            "user": json.dumps({"id": 1, "first_name": "Test"}, separators=(",", ":")),
        }
        init_data = _signed_init_data(params, token)

        parsed = webapp.validate_webapp_init_data(
            init_data,
            token,
            now=1700000010,
            max_age_s=900,
        )

        assert parsed is not None
        assert parsed["query_id"] == "AAEAAAE"
        assert parsed["user"]["id"] == 1
        assert parsed["auth_date"] == 1700000000

    def test_rejects_tampered_payload(self):
        token = "123:ABC"
        params = {
            "auth_date": "1700000000",
            "query_id": "AAEAAAE",
        }
        init_data = _signed_init_data(params, token) + "x"

        parsed = webapp.validate_webapp_init_data(
            init_data,
            token,
            now=1700000010,
            max_age_s=900,
        )

        assert parsed is None

    def test_rejects_stale_payload(self):
        token = "123:ABC"
        params = {
            "auth_date": "1700000000",
            "query_id": "AAEAAAE",
        }
        init_data = _signed_init_data(params, token)

        parsed = webapp.validate_webapp_init_data(
            init_data,
            token,
            now=1700002000,
            max_age_s=900,
        )

        assert parsed is None

    def test_accepts_payload_with_signature_field(self):
        token = "123:ABC"
        params = {
            "auth_date": "1700000000",
            "query_id": "AAEAAAE",
            "signature": "base64url-signature-from-telegram",
            "user": json.dumps({"id": 1, "first_name": "Test"}, separators=(",", ":")),
        }
        init_data = _signed_init_data(params, token)

        parsed = webapp.validate_webapp_init_data(
            init_data,
            token,
            now=1700000010,
            max_age_s=900,
        )

        assert parsed is not None
        assert parsed["signature"] == "base64url-signature-from-telegram"


class TestParseRgbTriplet:
    def test_parses_and_clamps_values(self):
        rgb = webapp.parse_rgb_triplet({"r": "280", "g": "-5", "b": "13"})
        assert rgb == (255, 0, 13)

    def test_rejects_invalid_payload(self):
        rgb = webapp.parse_rgb_triplet({"r": "x", "g": 2, "b": 3})
        assert rgb is None


class TestParseBrightnessPercent:
    def test_parses_and_clamps_values(self):
        brightness = webapp.parse_brightness_percent({"brightness_pct": "120"})
        assert brightness == 100

    def test_rejects_invalid_payload(self):
        brightness = webapp.parse_brightness_percent({"brightness_pct": "x"})
        assert brightness is None


class TestBuildHaColorWebAppHtml:
    def test_injects_absolute_app_base_url(self):
        html = webapp.build_ha_color_webapp_html("https://bot.example.com/app/ha-color")

        assert 'const APP_BASE_URL = "https://bot.example.com/app/ha-color";' in html
        assert 'postJson(buildApiUrl("state")' in html
        assert 'postJson(buildApiUrl("apply")' in html
        assert 'id="sliderBrightness"' in html
        assert 'brightness_pct: state.brightnessPct' in html
