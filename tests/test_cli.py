import os

import pytest

from jms_mcp.cli import main
from jms_mcp.config import Settings


@pytest.fixture
def clean_config(monkeypatch, tmp_path):
    monkeypatch.chdir(tmp_path)
    for key in list(os.environ):
        if key.lower() in Settings.model_fields:
            monkeypatch.delenv(key)
    monkeypatch.setenv("jumpserver_url", "https://jumpserver.example")
    monkeypatch.setenv("api_token", "Bearer UPSTREAM-SECRET")
    monkeypatch.setenv("api_key", "INBOUND-SECRET")
    return monkeypatch


def test_check_config_offline(clean_config, capsys, monkeypatch):
    def forbidden(*args, **kwargs):
        raise AssertionError("Configuration validation must not open an HTTP client")

    monkeypatch.setattr("httpx.AsyncClient.__init__", forbidden)
    main(["check-config"])
    output = capsys.readouterr().out
    assert "Configuration OK" in output
    assert "SECRET" not in output


@pytest.mark.parametrize(
    "field,value,expected",
    [
        ("api_key", "", "api_key is required"),
        ("api_key", "Bearer INBOUND-SECRET", "api_key must be a token without whitespace"),
        ("api_key", '"INBOUND-SECRET"', "api_key contains quote characters"),
        ("api_token", '"Bearer UPSTREAM-SECRET"', "api_token contains quote characters"),
        ("api_token", "", "api_token must contain a non-empty"),
        ("jumpserver_url", "https://jumpserver.example/api/v1", "without /api/v1"),
        ("server_port", "INVALID-SECRET", "server_port: int_parsing"),
        ("log_level", "INVALID-SECRET", "log_level: value_error"),
        ("tls_verify", "INVALID-SECRET", "tls_verify: bool_parsing"),
        ("ca_bundle", "/missing/PRIVATE-SECRET.pem", "ca_bundle cannot be loaded"),
    ],
)
def test_config_error_identifies_field_without_values(clean_config, capsys, field, value, expected):
    clean_config.setenv(field, value)
    with pytest.raises(SystemExit) as result:
        main(["check-config"])
    assert result.value.code == 1
    output = capsys.readouterr().err
    assert expected in output
    assert "SECRET" not in output


def test_serve_reports_runtime_config_error(clean_config, capsys):
    clean_config.setenv("api_token", '"Bearer UPSTREAM-SECRET"')
    with pytest.raises(SystemExit):
        main(["serve"])
    output = capsys.readouterr().err
    assert "api_token contains quote characters" in output
    assert "UPSTREAM-SECRET" not in output
