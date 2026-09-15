"""Localization invariants.

These tests protect the contract that Italian localization is UI-only:
- The frontend i18n bundle contains both `it` and `en` locales.
- Verdict / duplicate / checkpoint / engine constants exposed to the API
  remain the machine values (PASS / FAIL / UNRESOLVED / OLD36 / NEW12 /
  TOTAL48 / NEW36 / TOTAL72 / NOT_CONFIGURED / EXACT_DUPLICATE / ...).
- The frozen collector SHA256 stays unchanged.
- QA logic is not affected by locale (no locale plumbing in QA engine).
"""
from __future__ import annotations

import os
from pathlib import Path

# All env + init handled by conftest.py.

from fastapi.testclient import TestClient  # noqa: E402

from constants import (  # noqa: E402
    FROZEN_COLLECTOR_SHA256,
    VERDICT_FAIL,
    VERDICT_PASS,
    VERDICT_PASS_WITH_WARNING,
    VERDICT_UNRESOLVED,
    DUP_NEW,
    DUP_EXACT_DUPLICATE,
    DUP_SAME_SESSION_DIFFERENT_FILE,
    DUP_CONFLICT,
    CHECKPOINT_OLD36,
    CHECKPOINT_NEW12,
    CHECKPOINT_TOTAL48,
    CHECKPOINT_NEW36,
    CHECKPOINT_TOTAL72,
    ENGINE_NOT_CONFIGURED,
)
from server import app  # noqa: E402

client = TestClient(app)

I18N_PATH = Path("/app/frontend/src/lib/i18n.js")
LOCALE_PATH = Path("/app/frontend/src/lib/locale.jsx")


def test_i18n_bundle_has_it_and_en():
    src = I18N_PATH.read_text()
    assert "it:" in src and "en:" in src
    assert "LOCALES" in src
    assert "Italiano" in src
    assert "English" in src


def test_default_locale_is_italian():
    src = I18N_PATH.read_text()
    assert 'DEFAULT_LOCALE = "it"' in src
    provider_src = LOCALE_PATH.read_text()
    # Provider should not honor navigator.language on first visit
    assert "navigator.language" not in provider_src
    # Provider should fall back to DEFAULT_LOCALE
    assert "DEFAULT_LOCALE" in provider_src


def test_italian_verdict_strings_present():
    src = I18N_PATH.read_text()
    assert '"verdict.PASS": "SUPERATO"' in src
    assert '"verdict.PASS_WITH_WARNING": "SUPERATO CON AVVISO"' in src
    assert '"verdict.FAIL": "ERRORE"' in src
    assert '"verdict.UNRESOLVED": "DA RISOLVERE"' in src


def test_english_verdict_strings_present():
    src = I18N_PATH.read_text()
    assert '"verdict.PASS": "PASS"' in src
    assert '"verdict.FAIL": "FAIL"' in src


def test_machine_verdict_values_unchanged():
    assert VERDICT_PASS == "PASS"
    assert VERDICT_PASS_WITH_WARNING == "PASS_WITH_WARNING"
    assert VERDICT_FAIL == "FAIL"
    assert VERDICT_UNRESOLVED == "UNRESOLVED"


def test_machine_duplicate_states_unchanged():
    assert DUP_NEW == "NEW"
    assert DUP_EXACT_DUPLICATE == "EXACT_DUPLICATE"
    assert DUP_SAME_SESSION_DIFFERENT_FILE == "SAME_SESSION_DIFFERENT_FILE"
    assert DUP_CONFLICT == "CONFLICT"


def test_machine_checkpoint_labels_unchanged():
    assert CHECKPOINT_OLD36 == "OLD36"
    assert CHECKPOINT_NEW12 == "NEW12"
    assert CHECKPOINT_TOTAL48 == "TOTAL48"
    assert CHECKPOINT_NEW36 == "NEW36"
    assert CHECKPOINT_TOTAL72 == "TOTAL72"


def test_engine_status_unchanged():
    assert ENGINE_NOT_CONFIGURED == "NOT_CONFIGURED"


def test_collector_sha256_frozen():
    assert (
        FROZEN_COLLECTOR_SHA256
        == "e924edc1b8348d9cc8e74a37eaa17e6bbc80116de29d2f7a00cd43444d1265a3"
    )


def test_api_payloads_still_use_machine_values():
    fresh = TestClient(app)
    r = fresh.get("/api/policy")
    j = r.json()
    assert j["collector_sha256"] == FROZEN_COLLECTOR_SHA256
    assert j["verdicts"] == ["PASS", "PASS_WITH_WARNING", "FAIL", "UNRESOLVED"]
    assert j["duplicate_states"] == ["NEW", "EXACT_DUPLICATE", "SAME_SESSION_DIFFERENT_FILE", "CONFLICT"]
    assert j["engine"]["status"] == "NOT_CONFIGURED"


def test_qa_engine_is_locale_agnostic():
    """QA engine module must not import any locale/i18n plumbing."""
    src = Path("/app/backend/qa_engine.py").read_text()
    forbidden = ["locale", "i18n", "gettext", "translation", "italiano", "english"]
    for f in forbidden:
        assert f.lower() not in src.lower(), f"qa_engine.py must not reference {f!r}"


def test_qa_engine_still_produces_machine_verdicts_end_to_end():
    """Feed a synthetic PASS ZIP through the API and ensure PASS is returned."""
    import sys
    sys.path.insert(0, "/app/backend/tests")
    from fixtures import BuildOptions, build_zip

    fresh = TestClient(app)
    fresh.post("/api/auth/login", json={"password": os.environ["SUPERBOT_PASSWORD"]})
    z = build_zip(BuildOptions(session_id="20260910T123759Z_locale"))
    r = fresh.post("/api/sessions/upload", files={"files": ("t.zip", z, "application/zip")})
    assert r.status_code == 200, r.text
    result = r.json()["results"][0]
    # Internal machine strings must be unchanged
    assert result["verdict"] == "PASS"
    assert result["duplicate_status"] == "NEW"


def test_language_switcher_component_exists():
    p = Path("/app/frontend/src/components/LanguageSwitcher.jsx")
    assert p.exists(), "LanguageSwitcher component missing"
    src = p.read_text()
    assert "topbar-language-switcher" in src
    assert "setLocale" in src


def test_no_ui_string_leakage_into_backend_constants():
    """Italian labels must live only in the frontend bundle, not in Python
    constants that shape API payloads.
    """
    for f in ["/app/backend/constants.py", "/app/backend/frozen_engine.py"]:
        src = Path(f).read_text()
        # Detect any obvious Italian ui strings in backend constants
        forbidden = ["SUPERATO", "ERRORE", "DA RISOLVERE", "NUOVO", "DUPLICATO"]
        for word in forbidden:
            assert word not in src, f"{f} must not contain Italian UI string {word!r}"
