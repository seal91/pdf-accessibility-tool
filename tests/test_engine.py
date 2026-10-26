"""The engine on a generated PDF: tagging, figures, editing."""
import pytest

from pdfa11y.audit import audit
from pdfa11y.batch import process_one
from pdfa11y.editor import Editor, EditError
from pdfa11y.figures import list_figures


@pytest.fixture()
def tagged(test_pdf, tmp_path):
    out = tmp_path / "out.pdf"
    r = process_one(str(test_pdf), str(out), True, False)
    assert r["error"] is None, r["error"]
    assert out.exists()
    return out


def test_untagged_pdf_gets_tagged(test_pdf, tagged):
    assert audit(test_pdf)["structure"] is None
    a = audit(tagged)
    assert a["structure"] is not None, "the tagger should have built a structure tree"
    assert a["document"]["lang"], "a language should have been set"


def test_figure_is_found_and_alt_text_is_written(tagged, tmp_path):
    figs = list_figures(tagged)
    assert len(figs) == 1 and figs[0].bbox is not None
    ed = Editor(tagged)
    try:
        saved = tmp_path / "saved.pdf"
        ed.save(saved, {figs[0].key: {"alt": "A small coloured square"}})
    finally:
        ed.close()
    assert list_figures(saved)[0].alt == "A small coloured square"


def test_editor_undo_redo_and_refusals(tagged):
    ed = Editor(tagged)
    try:
        blocks = ed.page_blocks(0)
        para = next(b for b in blocks if b.role == "P")
        ed.do("set_role", para.ref, "Caption")
        assert ed.role_of(para.ref) == "Caption"
        assert ed.undo() and ed.role_of(para.ref) == "P"
        assert ed.redo() and ed.role_of(para.ref) == "Caption"
        figure = next(b for b in blocks if b.role == "Figure")
        with pytest.raises(EditError):
            ed.do("artifact", figure.ref)               # a picture is marked decorative, not hidden
    finally:
        ed.close()
