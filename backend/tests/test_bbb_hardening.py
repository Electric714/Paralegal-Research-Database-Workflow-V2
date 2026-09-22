from app.research.sources.bbb import parse_profile_html


PROFILE = "https://www.bbb.org/us/wi/madison/profile/general-contractor/example-builders-llc-0694-1000000000"


def test_profile_url_slug_cannot_supply_missing_live_identity():
    html = "<html><body><h1>Business Profile</h1><p>Overview</p></body></html>"
    assert parse_profile_html(html, PROFILE) is None


def test_profile_name_without_live_location_is_not_identity_evidence():
    html = "<html><body><h1>Example Builders LLC</h1><p>Business Profile</p></body></html>"
    assert parse_profile_html(html, PROFILE) is None
