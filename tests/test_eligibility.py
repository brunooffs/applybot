from applybot.eligibility import location_eligibility

PT = {"home_country": "Portugal", "authorised_regions": ["Portugal", "EU"]}


def test_us_only_is_no():
    assert location_eligibility("Remote - US", "", PT)[0] == "no"


def test_country_in_eu_is_yes():
    assert location_eligibility("Remote, Poland", "", PT)[0] == "yes"


def test_open_place_in_list_keeps_job():
    assert location_eligibility("Remote US; Remote Spain", "", PT)[0] == "yes"


def test_worldwide_is_yes():
    assert location_eligibility("Worldwide", "", PT)[0] == "yes"


def test_unknown_place_asks_model():
    assert location_eligibility("Remote", "", PT)[0] is None


def test_contractor_posting_not_rejected():
    assert location_eligibility("Remote US", "We hire contractors via B2B", PT)[0] is None


def test_relocation_goes_to_review():
    assert location_eligibility("Remote US", "", {**PT, "accepts_relocation": True})[0] is None


def test_unconfigured_asks_model():
    assert location_eligibility("Remote US", "", {})[0] is None


def test_us_inside_europe_word():
    assert location_eligibility("Remote - EMEA", "", PT)[0] == "yes"
