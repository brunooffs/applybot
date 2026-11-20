from applybot.drafting import parroting_guard

POSTING = "You bring strong experience designing scalable distributed systems and mentoring engineers in a fast paced team."
FACTS = "Built payment APIs in Node.js. Mentored engineers."


def test_copied_requirement_is_flagged():
    letter = "I have strong experience designing scalable distributed systems for banks."
    out = parroting_guard(letter, POSTING, FACTS)
    assert len(out) == 1 and "strong experience designing scalable distributed systems" in out[0]


def test_own_wording_passes():
    assert parroting_guard("I built payment APIs in Node.js and mentored engineers.", POSTING, FACTS) == []


def test_phrase_present_in_facts_is_allowed():
    facts = FACTS + " Strong experience designing scalable distributed systems at Acme."
    assert parroting_guard("I have strong experience designing scalable distributed systems.", POSTING, facts) == []


def test_job_title_is_allowed():
    posting = "Senior Staff Backend Platform Engineer wanted"
    assert parroting_guard("I am applying for the Senior Staff Backend Platform Engineer role.", posting, "",
                           "Senior Staff Backend Platform Engineer") == []


def test_short_overlap_is_ignored():
    assert parroting_guard("I enjoy a fast paced team.", POSTING, FACTS) == []
