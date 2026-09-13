import pytest
from aiverse_automations.adapters import _safe_http_target
from aiverse_automations.errors import ValidationError


def test_plain_http_remote_is_rejected():
    with pytest.raises(ValidationError):
        _safe_http_target("http://example.com/invoke")
    assert _safe_http_target("http://127.0.0.1:9999/invoke")[0]=="http"
    assert _safe_http_target("https://example.com/invoke")[0]=="https"
