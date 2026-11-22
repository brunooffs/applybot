import io
import zipfile

import pytest
import yaml

from applybot import factsimport as fi
from applybot.config import DEFAULTS

CV = """Sam Example
Lisbon, Portugal | sam@example.com | +351 911 000 111
linkedin.com/in/sam-example

SUMMARY
Backend engineer with 15+ years of experience in payments and logistics.

EXPERIENCE
Initech Payments, Senior Backend Engineer, 2018 to 2024
Built an event-driven settlement service with Python, AWS Lambda, SQS and DynamoDB.
Cut reconciliation time from 6 hours to 40 minutes.

Umbrella Logistics, Software Engineer, 2012 to 2018
Maintained a PHP and MySQL warehouse system used by 30 sites.

EDUCATION
University of Example, BSc Computer Science

CERTIFICATIONS
Certified Scrum Master, Scrum Alliance, 2014

LANGUAGES
Portuguese: native. English: C1.
"""


def item(kind, quote, **fields):
    return {"kind": kind, "quote": quote, **fields}


GOOD = [
    item("identity", "sam@example.com", field="email", value="sam@example.com"),
    item("identity", "+351 911 000 111", field="phone", value="+351 911 000 111"),
    item("experience_total", "15+ years of experience", value="15+ years"),
    item("role", "Initech Payments, Senior Backend Engineer, 2018 to 2024", employer="Initech Payments",
         title="Senior Backend Engineer", dates="2018 to 2024", stack=["Python", "AWS Lambda", "SQS", "DynamoDB"],
         work="Built an event-driven settlement service.", highlights=["Cut reconciliation time from 6 hours to 40 minutes"]),
    item("role", "Umbrella Logistics, Software Engineer, 2012 to 2018", employer="Umbrella Logistics",
         title="Software Engineer", dates="2012 to 2018", stack=["PHP", "MySQL"]),
    item("skill", "Python, AWS Lambda, SQS and DynamoDB", category="backend", item="Python"),
    item("skill", "PHP and MySQL warehouse system", category="databases", item="MySQL"),
    item("certification", "Certified Scrum Master, Scrum Alliance, 2014", name="Certified Scrum Master", issuer="Scrum Alliance", year="2014"),
    item("education", "University of Example, BSc Computer Science", institution="University of Example", field="Computer Science", degree="BSc"),
    item("language", "English: C1", language="English", level="C1"),
]
BAD = [
    item("skill", "Kubernetes and Terraform", category="containers", item="Kubernetes"),                 # quote not in CV
    item("role", "Initech Payments, Senior Backend Engineer, 2018 to 2024", employer="Hooli", title="CTO"),  # employer not in CV
    item("skill", "Built an event-driven settlement service", category="x", item="Kafka"),                # item not in CV
    {"kind": "nonsense", "quote": "x"}, "not a dict", {"kind": "skill"},
]


class Fake:
    def __init__(self, items):
        self.items, self.calls = items, 0

    def complete_json(self, system, user):
        self.calls += 1
        assert "CV TEXT" in user and "never" in system.lower()
        return {"items": self.items}


# --- reading ---------------------------------------------------------------------

def make_docx(paragraphs):
    body = "".join(f"<w:p><w:r><w:t>{p}</w:t></w:r></w:p>" for p in paragraphs)
    xml = ('<?xml version="1.0"?><w:document xmlns:w="http://schemas.openxmlformats.org/wordprocessingml/2006/main">'
           f"<w:body>{body}</w:body></w:document>")
    buf = io.BytesIO()
    with zipfile.ZipFile(buf, "w") as z:
        z.writestr("word/document.xml", xml)
    return buf.getvalue()


def make_pdf(text):
    stream = f"BT /F1 12 Tf 50 750 Td ({text}) Tj ET".encode()
    objs = [b"<< /Type /Catalog /Pages 2 0 R >>", b"<< /Type /Pages /Kids [3 0 R] /Count 1 >>",
            b"<< /Type /Page /Parent 2 0 R /MediaBox [0 0 612 792] /Contents 4 0 R /Resources << /Font << /F1 5 0 R >> >> >>",
            b"<< /Length %d >>\nstream\n" % len(stream) + stream + b"\nendstream",
            b"<< /Type /Font /Subtype /Type1 /BaseFont /Helvetica >>"]
    out, offs = b"%PDF-1.4\n", []
    for i, o in enumerate(objs, 1):
        offs.append(len(out))
        out += b"%d 0 obj\n" % i + o + b"\nendobj\n"
    x = len(out)
    out += b"xref\n0 %d\n0000000000 65535 f \n" % (len(objs) + 1) + b"".join(b"%010d 00000 n \n" % o for o in offs)
    return out + b"trailer\n<< /Size %d /Root 1 0 R >>\nstartxref\n%d\n%%%%EOF" % (len(objs) + 1, x)


def test_read_txt_docx_pdf():
    assert "Initech" in fi.read_document("cv.txt", CV.encode())
    assert "Initech" in fi.read_document("cv.docx", make_docx(CV.split("\n")))
    pdf = fi.read_document("cv.pdf", make_pdf("Sam Example Backend engineer with fifteen years of experience in payments and logistics systems"))
    assert "fifteen years" in pdf


def test_unreadable_files_give_clear_errors():
    with pytest.raises(fi.ImportProblem, match="scanned"):
        fi.read_document("cv.pdf", make_pdf(""))
    with pytest.raises(fi.ImportProblem, match="valid .docx"):
        fi.read_document("cv.docx", b"not a zip")
    with pytest.raises(fi.ImportProblem, match="Unsupported"):
        fi.read_document("cv.exe", b"x" * 500)
    with pytest.raises(fi.ImportProblem, match="10 MB"):
        fi.read_document("cv.txt", b"x" * 10_000_001)


# --- extracting and checking --------------------------------------------------------

def test_good_items_survive_and_bad_ones_are_dropped():
    props, dropped = fi.extract(Fake(GOOD + BAD), CV)
    assert len(props) == len(GOOD) and dropped >= 3
    assert {p["id"] for p in props} == {f"p{i}" for i in range(1, len(GOOD) + 1)}
    assert not any(p["fields"].get("item") == "Kafka" for p in props)


def test_phone_spacing_differences_are_tolerated():
    p = [item("identity", "+351 911 000 111", field="phone", value="351911000111")]
    kept, _ = fi.verify(fi.sanitize({"items": p}), CV)
    assert len(kept) == 1


def test_sanitize_coerces_shapes():
    out = fi.sanitize({"items": [item("role", "q", employer="A", title="T", stack="Go, Rust", highlights=None)]})
    assert out[0]["fields"]["stack"] == ["Go", "Rust"] and out[0]["fields"]["highlights"] == []
    assert fi.sanitize("garbage") == [] and fi.sanitize({"items": "x"}) == []


def test_long_cv_is_read_in_parts_and_deduplicated():
    long_cv = (CV + "\n") * 40
    fake = Fake(GOOD)
    props, _ = fi.extract(fake, long_cv)
    assert fake.calls > 1 and len(props) == len(GOOD)


# --- writing into the real template, keeping comments --------------------------------

def template_text():
    return (DEFAULTS / "config" / "facts.yaml.example").read_text(encoding="utf-8")


def confirmed(extra=()):
    props, _ = fi.extract(Fake(GOOD), CV)
    for p in props:
        if p["kind"] == "skill":
            p["fields"]["level"] = "production"
    return props + list(extra)


def test_merge_text_equals_merge_data_and_keeps_comments():
    text = template_text()
    items = confirmed()
    new = fi.merge_text(text, items)
    assert yaml.safe_load(new) == fi.merge_data(yaml.safe_load(text), items)
    assert "# CONFIRM" in new and "# example figure" in new and "do not describe as current" in new
    d = yaml.safe_load(new)
    names = [e["name"] for e in d["experience"]["employers"]]
    assert "Initech Payments" in names and "Umbrella Logistics" in names and "Acme Logistics B.V." in names


def test_existing_employer_gets_the_role_added_under_it():
    items = fi.sanitize({"items": [item("role", "q", employer="Acme Logistics B.V.", title="Staff Engineer", dates="2024 to 2025")]})
    d = yaml.safe_load(fi.merge_text(template_text(), items))
    acme = next(e for e in d["experience"]["employers"] if e["name"] == "Acme Logistics B.V.")
    assert [r["title"] for r in acme["roles"]] == ["Senior Backend Engineer", "Software Engineer", "Staff Engineer"]
    assert len(d["experience"]["employers"]) == 3


def test_skill_levels_never_change_existing_claims():
    items = [{"kind": "skill", "fields": {"category": "messaging", "item": "RabbitMQ", "level": "production"}, "quote": ""},
             {"kind": "skill", "fields": {"category": "messaging", "item": "NATS", "level": "project"}, "quote": ""},
             {"kind": "skill", "fields": {"category": "backend", "item": "Go", "level": "working"}, "quote": ""}]
    d = yaml.safe_load(fi.merge_text(template_text(), items))
    assert d["skills"]["messaging"] == {"level": "project", "items": ["Kafka", "NATS"]}      # same level: appended
    assert d["skills"]["messaging_production"] == {"level": "production", "items": ["RabbitMQ"]}   # other level: own entry
    assert d["skills"]["backend"]["level"] == "production" and "Go" not in d["skills"]["backend"]["items"]
    assert d["skills"]["backend_working"] == {"level": "working", "items": ["Go"]}


def test_skill_without_level_and_duplicates_are_not_written():
    items = [{"kind": "skill", "fields": {"category": "backend", "item": "Go", "level": ""}, "quote": ""},
             {"kind": "skill", "fields": {"category": "backend", "item": "node.js", "level": "production"}, "quote": ""}]
    new = fi.merge_text(template_text(), items)
    assert new == template_text().rstrip("\n") + "\n"


def test_identity_only_fills_what_is_unset():
    items = fi.sanitize({"items": [item("identity", "q", field="email", value="other@example.com"),
                                   item("identity", "q", field="location", value="Lisbon")]})
    d = yaml.safe_load(fi.merge_text(template_text(), items))
    assert d["identity"]["email"] == "alex@example.com"    # already set: untouched


def test_education_degree_stays_todo_until_the_person_sets_it():
    items = fi.sanitize({"items": [item("education", "q", institution="University of Example", field="Computer Science", degree="")]})
    text = "education:\n  - institution: Other\n    degree: BSc\n"
    d = yaml.safe_load(fi.merge_text(text, items))
    assert d["education"][1]["degree"] == "TODO"


def test_skeleton_start_yields_valid_facts_with_todo_markers():
    base = yaml.safe_load(fi.SKELETON)
    items = confirmed()
    new = fi.merge_text(fi.SKELETON, items)
    d = yaml.safe_load(new)
    assert d == fi.merge_data(base, items)
    assert d["identity"]["email"] == "sam@example.com" and d["experience"]["total"] == "15+ years"
    assert d["skills"]["backend"] == {"level": "production", "items": ["Python"]}
    assert "availability: TODO" in new and "# VERIFIED FACTS" in new


def test_apply_import_replaces_example_with_backup(tmp_path):
    (tmp_path / "config").mkdir()
    (tmp_path / "config" / "facts.yaml").write_text(template_text(), encoding="utf-8")
    cfg = type("C", (), {"root": tmp_path})()
    fi.apply_import(cfg, confirmed(), from_skeleton=True)
    d = yaml.safe_load((tmp_path / "config" / "facts.yaml").read_text())
    assert d["identity"]["name"] != "Alex Example" and "Alex" not in str(d)
    assert "Alex Example" in (tmp_path / "config" / "facts.yaml.bak").read_text()


def test_apply_import_refuses_when_result_would_differ(tmp_path, monkeypatch):
    (tmp_path / "config").mkdir()
    p = tmp_path / "config" / "facts.yaml"
    p.write_text(template_text(), encoding="utf-8")
    cfg = type("C", (), {"root": tmp_path})()
    monkeypatch.setattr(fi, "merge_text", lambda text, items: text + "\nbroken: [unclosed\n")
    with pytest.raises(fi.ImportProblem, match="Nothing was changed"):
        fi.apply_import(cfg, confirmed(), from_skeleton=False)
    assert p.read_text(encoding="utf-8") == template_text()


def test_mark_existing_flags_what_facts_already_has():
    props = fi.sanitize({"items": [
        item("skill", "q", category="backend", item="Node.js"), item("skill", "q", category="backend", item="Go"),
        item("certification", "q", name="AWS Certified Developer - Associate"),
        item("role", "q", employer="Acme Logistics B.V.", title="Senior Backend Engineer", dates="2019 to 2024"),
        item("language", "q", language="Portuguese", level="Native")]})
    fi.mark_existing(yaml.safe_load(template_text()), props)
    assert [p["existing"] for p in props] == [True, False, True, True, True]


def test_is_template():
    assert fi.is_template(yaml.safe_load(template_text()))
    assert not fi.is_template({"identity": {"name": "Sam"}})


def test_pdf_line_break_hyphens_and_ligatures_do_not_break_quote_checks():
    text = "Built an event-\ndriven service and an oﬃce tool with Python for payments teams."
    p = fi.sanitize({"items": [item("skill", "event-driven service and an office tool", category="backend", item="Python")]})
    kept, dropped = fi.verify(p, text)
    assert len(kept) == 1 and dropped == 0
