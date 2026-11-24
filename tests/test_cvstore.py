import shutil

import pytest
import yaml

from applybot import cvstore
from applybot.config import DEFAULTS, load_config
from applybot.factsimport import ImportProblem


def make_home(tmp_path, own_list=None):
    home = tmp_path / "h"
    (home / "cvs").mkdir(parents=True)
    shutil.copy(DEFAULTS / "cvs" / "cvs.yaml.example", home / "cvs" / "cvs.yaml")
    if own_list:
        (home / "cvs" / "cvs.yaml").write_text(own_list)
    return home


def listed(home):
    return yaml.safe_load((home / "cvs" / "cvs.yaml").read_text())["cvs"]


def test_names_are_made_safe():
    assert cvstore.safe_name("../../etc/Bruno Sousa CV (1).PDF") == "Bruno Sousa CV _1_.pdf"
    assert cvstore.safe_name("C:\\docs\\cv.docx") == "cv.docx"
    for bad in ("cvs.yaml", "script.sh", ".pdf", "", "..", "x.exe"):
        with pytest.raises(ImportProblem):
            cvstore.safe_name(bad)


def test_first_upload_takes_over_the_starter_entry(tmp_path):
    home = make_home(tmp_path)
    out = cvstore.store_cv(home, "Bruno-Sousa-CV.pdf", b"%PDF-1 a")
    assert out == {"file": "Bruno-Sousa-CV.pdf", "id": "bruno-sousa-cv", "replaced": False, "added": True}
    assert (home / "cvs" / "Bruno-Sousa-CV.pdf").read_bytes() == b"%PDF-1 a"
    cvs = listed(home)
    assert len(cvs) == 1 and cvs[0]["file"] == "Bruno-Sousa-CV.pdf" and len(cvs[0]["families"]) > 5   # the starter's families stay
    assert (home / "cvs" / "cvs.yaml").read_text().startswith("# CV pool (EXAMPLE)")                     # header kept


def test_same_name_replaces_the_file_silently_and_adds_no_second_entry(tmp_path):
    home = make_home(tmp_path)
    cvstore.store_cv(home, "cv.pdf", b"old")
    out = cvstore.store_cv(home, "cv.pdf", b"new")
    assert out["replaced"] and not out["added"]
    assert (home / "cvs" / "cv.pdf").read_bytes() == b"new" and len(listed(home)) == 1
    assert not list((home / "cvs").glob("*.tmp"))


def test_a_new_name_adds_an_entry_and_keeps_the_other_cv(tmp_path):
    home = make_home(tmp_path)
    cvstore.store_cv(home, "backend.pdf", b"a")
    out = cvstore.store_cv(home, "Flutter CV.pdf", b"b")
    assert out["added"] and out["id"] == "flutter-cv"
    assert [c["file"] for c in listed(home)] == ["backend.pdf", "Flutter CV.pdf"]
    assert listed(home)[1]["families"] == []
    assert (home / "cvs" / "backend.pdf").read_bytes() == b"a"
    assert len(load_config(home).cvs) == 2


def test_ids_stay_unique(tmp_path):
    home = make_home(tmp_path, "cvs:\n  - id: cv\n    file: other.pdf\n    families: [A]\n")
    assert cvstore.store_cv(home, "CV.pdf", b"x")["id"] == "cv-2"


def test_cover_letters_go_to_their_own_folder_and_not_into_the_list(tmp_path):
    home = make_home(tmp_path)
    cvstore.store_letter(home, "letter.docx", b"1")
    cvstore.store_letter(home, "letter.docx", b"2")
    assert (home / "cvs" / "cover-letters" / "letter.docx").read_bytes() == b"2"
    assert [c["file"] for c in listed(home)] == ["my-cv.pdf"]


def test_families_go_to_the_uploaded_cv_but_never_move_from_another(tmp_path):
    home = make_home(tmp_path, "cvs:\n  - id: a\n    file: a.pdf\n    families: [Platform Engineer]\n")
    cvstore.store_cv(home, "b.pdf", b"b")
    out = cvstore.set_families(home, "b.pdf", ["Platform Engineer", "Flutter Engineer", "Scrum Master"])
    assert out == {"cv": "b", "added": ["Flutter Engineer", "Scrum Master"], "elsewhere": {"Platform Engineer": "a"}}
    assert load_config(home).family_to_cv == {"Platform Engineer": "a", "Flutter Engineer": "b", "Scrum Master": "b"}
    again = cvstore.set_families(home, "b.pdf", ["Flutter Engineer"])
    assert again["added"] == []


def test_a_single_cv_gets_exactly_the_confirmed_families_when_exclusive(tmp_path):
    home = make_home(tmp_path)
    cvstore.store_cv(home, "me.pdf", b"x")
    cvstore.set_families(home, "me.pdf", ["Platform Engineer"], exclusive=True)
    assert listed(home)[0]["families"] == ["Platform Engineer"]
    with pytest.raises(ImportProblem, match="not in cvs/cvs.yaml"):
        cvstore.set_families(home, "gone.pdf", ["X"])
