from pathlib import Path
import re


ROOT = Path(__file__).parents[1] / "templates" / "tencent_ses"
EXPECTED_VARIABLES = {
    "verify_email": {"verify_token"},
    "activation_test": {"plan_name", "starts_on", "expires_on", "target_count"},
    "quota_alert": {"office", "date", "availability", "detected_at"},
    "manage_link": {"manage_token"},
}


def _variables(content: str) -> set[str]:
    return set(re.findall(r"\{\{([a-z_]+)\}\}", content))


def test_template_files_match_runtime_variable_contract() -> None:
    for name, expected in EXPECTED_VARIABLES.items():
        for suffix in ("html", "txt"):
            content = (ROOT / f"{name}.{suffix}").read_text(encoding="utf-8")
            assert _variables(content) == expected


def test_templates_have_no_complete_url_variables_or_rejected_brand_name() -> None:
    for path in ROOT.glob("*"):
        if path.suffix not in {".html", ".txt"}:
            continue
        content = path.read_text(encoding="utf-8")
        assert not re.search(r"\{\{[a-z_]*url[a-z_]*\}\}", content, re.IGNORECASE)
        assert "HKID Notice" not in content
        assert "HKID Alert" not in content


def test_action_links_use_fixed_domains_and_quoted_href_attributes() -> None:
    verify = (ROOT / "verify_email.html").read_text(encoding="utf-8")
    manage = (ROOT / "manage_link.html").read_text(encoding="utf-8")
    quota = (ROOT / "quota_alert.html").read_text(encoding="utf-8")

    assert 'href="https://hkid-notice.com/verify?token={{verify_token}}"' in verify
    assert 'href="https://hkid-notice.com/manage?token={{manage_token}}"' in manage
    assert (
        'href="https://www.gov.hk/tc/residents/immigration/idcard/hkic/bookregidcard.htm"'
        in quota
    )
