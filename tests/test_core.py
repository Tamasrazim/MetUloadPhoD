from __future__ import annotations

import importlib.util
from pathlib import Path

APP_PATH = Path(__file__).resolve().parents[1] / "app.py"
spec = importlib.util.spec_from_file_location("metupload_app", APP_PATH)
assert spec and spec.loader
app = importlib.util.module_from_spec(spec)
spec.loader.exec_module(app)


def test_parse_rate_fraction() -> None:
    assert app.parse_rate("60/1") == 60.0
    assert round(app.parse_rate("60000/1001"), 3) == 59.94


def test_parse_rate_invalid() -> None:
    assert app.parse_rate("0/0") is None
    assert app.parse_rate("N/A") is None


def test_resolution_caps_preserve_aspect_ratio() -> None:
    assert app.output_dimensions(3840, 2160, 1920) == (1920, 1080)
    assert app.output_dimensions(2160, 3840, 1920) == (1080, 1920)


def test_resolution_never_upscales() -> None:
    assert app.output_dimensions(1920, 1080, 3840) == (1920, 1080)
    assert app.output_dimensions(1280, 720, None) == (1280, 720)


def test_constant_60_cadence() -> None:
    stamps = [i / 60 for i in range(600)]
    result = app.cadence_report(stamps)
    assert result["near_60"] is True
    assert result["constant"] is True
    assert result["frame_count"] == 600


def test_variable_cadence_is_not_constant() -> None:
    stamps = [0.0]
    for i in range(1, 100):
        stamps.append(stamps[-1] + (1 / 60 if i % 2 else 1 / 30))
    result = app.cadence_report(stamps)
    assert result["constant"] is False
