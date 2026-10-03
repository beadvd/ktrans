import json
import zipfile

from conftest import ENGLISH, make_epub, write_english

from kt import build, check, glossary, project, segfile
from kt.cli import main
from kt.handoff import import_folder


def init(workdir):
    return project.init(make_epub(workdir / "b.epub"), slug="b")


def test_segfile_roundtrip_and_strays():
    parsed = segfile.parse("# head\n[[1]] One\n~~~ scene break ~~~\n[[2]] Two\nloose line\n[[2]] again\n")
    assert parsed.segments == {1: "One", 2: "Two"}
    assert parsed.stray == [(5, "loose line")] and parsed.duplicates == [2]


def test_init_writes_chunks_and_scene_breaks(workdir):
    proj = init(workdir)
    assert [c.id for c in proj.chunks] == ["c0001", "c0002"]
    text = proj.ko("c0001").read_text("utf-8")
    assert "~~~ scene break ~~~\n[[5]]" in text


def test_long_chapter_splits_at_scene_break():
    blocks = [{"k": "p", "t": "가" * 1000, "n": i} for i in range(1, 8)]
    blocks.insert(4, {"k": "sep", "t": "* * *"})
    parts = project.plan_chapter(1, blocks, max_chars=5000)
    assert parts == [(1, 4, 4000), (5, 7, 3000)]


def test_check_catches_the_classic_defects(workdir):
    proj = init(workdir)
    write_english(proj)
    path = proj.en("c0001")
    lines = path.read_text("utf-8").splitlines()
    lines[2] = lines[2] + " 유현"  # Hangul
    lines[4] = "[[5]] " + ENGLISH[1][2]  # echo of segment 2
    lines.append("an extra line")
    path.write_text("\n".join(lines), encoding="utf-8")
    report = check.check_chunk(proj, proj.chunk("c0001"))
    codes = {i.code for i in report.issues if i.level == "ERROR"}
    assert {"hangul", "stray-line", "repeated-paragraph"} <= codes


def test_glossary_parse_filter_and_merge(workdir):
    proj = init(workdir)
    glossary.append(proj.glossary_file, [glossary.Entry("유현 / 현이", "Yoo-hyun", "person", "he")])
    entries = glossary.load(proj.glossary_file)
    assert entries[0].aliases == ["유현", "현이"]
    assert glossary.relevant(entries, "현이가 웃었다") == entries
    proj.notes("c0001").write_text(
        "## New terms\n| 한국어 | English | type | pronoun | notes |\n|---|---|---|---|---|\n"
        "| 세라 | Sera | person | she | |\n| 유현 | Yu-hyeon | person | he | |\n"
        "## Doubts\n- [[3]] unsure who speaks\n", encoding="utf-8")
    result = glossary.merge_notes(proj.glossary_file, proj.path / "notes")
    assert [e.english for e in result.added] == ["Sera"]
    assert result.conflicts[0][1].english == "Yu-hyeon"
    assert result.doubts == [("c0001", "[[3]] unsure who speaks")]
    assert not proj.notes("c0001").exists()


def test_packet_filters_glossary_and_carries_english(workdir):
    proj = init(workdir)
    glossary.append(proj.glossary_file, [glossary.Entry("세라", "Sera", "person", "she"),
                                          glossary.Entry("마왕", "Demon King", "title")])
    write_english(proj)
    text = main_output(["packet", "b", "c0002"])
    assert "| 세라 | Sera |" in text and "Demon King" not in text
    assert "PREVIOUS ENGLISH" in text and "could not sleep" in text


def test_build_is_a_valid_epub(workdir):
    proj = init(workdir)
    write_english(proj)
    result = build.build(proj)
    assert build.validate(result.path) == []
    with zipfile.ZipFile(result.path) as z:
        assert z.namelist()[0] == "mimetype"
        chapter = z.read("OEBPS/text/ch0001.xhtml").decode()
    assert "<h1>Chapter 1: The Strange Castle</h1>" in chapter
    assert "<em>quietly</em>" in chapter and "“Is this the place?”" in chapter
    assert '<p class="sep">* * *</p>' in chapter and "<p>...</p>" in chapter


def test_partial_build_needs_flag(workdir):
    proj = init(workdir)
    try:
        build.build(proj)
    except SystemExit as exc:
        assert "not translated" in str(exc)
    else:
        raise AssertionError("expected refusal")
    result = build.build(proj, allow_partial=True)
    assert len(result.untranslated) == 2 and build.validate(result.path) == []


def test_audit_and_replace(workdir):
    proj = init(workdir)
    write_english(proj)
    glossary.append(proj.glossary_file, [glossary.Entry("세라", "Sera", "person", "she")])
    path = proj.en("c0002")
    path.write_text(path.read_text("utf-8").replace("Sera", "Serra"), encoding="utf-8")
    report, _ = check.audit(proj)
    assert any(i.code == "name-variant" and "Serra" in i.detail for i in report.issues)
    assert main(["replace", "b", "Serra", "Sera", "--word"]) == 0
    assert "Serra" not in path.read_text("utf-8")
    assert segfile.read(path).order == [1, 2, 3, 4]


def test_import_old_handoff_folder(workdir):
    proj = init(workdir)
    folder = workdir / "handoff"
    folder.mkdir()
    korean = {str(i): t for i, t in enumerate(
        ["제1화 낯선 성", "유현은 성문 앞에 서서 한참 동안 위를 올려다보았다.", "\"여기가 맞아?\"",
         "세라가 ⟦1⟧조용히⟦/1⟧ 고개를 끄덕였다.", "밤이 되자 성 안의 등불이 하나둘 꺼졌다. 유현은 잠들지 못했다.", "……"], 1)}
    english = {"1": ENGLISH[1][1], "2": ENGLISH[1][2], "3": ENGLISH[1][3],
               "4": "Sera nodded ⟦1⟧quietly⟦/1⟧.", "5": ENGLISH[1][5], "6": "..."}
    (folder / "chunk_001.json").write_text(json.dumps({"previous_section": "", "translate": korean}), "utf-8")
    (folder / "chunk_001.reply.json").write_text(json.dumps(english), "utf-8")
    result = import_folder(proj, folder)
    assert result.imported == ["c0001"]
    assert segfile.read(proj.en("c0001")).segments[4] == "Sera nodded {i}quietly{/i}."


def test_next_batches_and_edit_next(workdir, capsys):
    proj = init(workdir)
    main(["next", "b", "--workers", "2", "--max-chunks", "1"])
    out = capsys.readouterr().out
    assert "[1] c0001" in out and "[2] c0002" in out
    write_english(proj)
    main(["edit-next", "b"])
    assert (proj.path / "en_draft" / "c0001.txt").exists()
    main(["mark-edited", "b", "c0001-c0002"])
    assert proj.state["edited"] == ["c0001", "c0002"]


def main_output(argv):
    import io
    from contextlib import redirect_stdout

    buffer = io.StringIO()
    with redirect_stdout(buffer):
        main(argv)
    return buffer.getvalue()


def test_inplace_build_keeps_the_publishers_formatting(workdir):
    proj = project.init(make_epub(workdir / "s.epub", system_window=True), slug="s")
    english = {1: dict(ENGLISH[1]), 2: dict(ENGLISH[2])}
    english[2][5] = "[Quest Complete]"
    write_english(proj, english)
    proj.meta["title_en"] = "The Swordsman of the Strange Castle"
    proj.save_meta()
    result = build.build_inplace(proj)
    assert result.problems == [] and build.validate(result.path) == []
    with zipfile.ZipFile(result.path) as z:
        ch1 = z.read("OEBPS/ch1.xhtml").decode()
        ch2 = z.read("OEBPS/ch2.xhtml").decode()
        nav = z.read("OEBPS/nav.xhtml").decode()
        opf = z.read("OEBPS/content.opf").decode()
        assert b"sys-window" in z.read("OEBPS/style.css")
    assert '<h2 class="chapter-title">Chapter 1: The Strange Castle</h2>' in ch1
    assert '<p class="txt">Sera nodded <em>quietly</em>.</p>' in ch1
    assert '<div class="sys-window"><p class="sys">[Quest Complete]</p></div>' in ch2
    assert 'xml:lang="en"' in ch1 and "<title>Chapter 1: The Strange Castle</title>" in ch1
    assert ">Chapter 2: The First Trial</a>" in nav
    assert "<dc:title>The Swordsman of the Strange Castle</dc:title>" in opf and "<dc:language>en</dc:language>" in opf


def test_inplace_partial_keeps_korean(workdir):
    proj = project.init(make_epub(workdir / "s.epub"), slug="s")
    result = build.build_inplace(proj, allow_partial=True)
    with zipfile.ZipFile(result.path) as z:
        assert "유현은" in z.read("OEBPS/ch1.xhtml").decode()


def test_check_log_keeps_first_and_latest_counts(workdir, capsys):
    proj = init(workdir)
    write_english(proj)
    path = proj.en("c0001")
    clean = path.read_text("utf-8")
    path.write_text(clean.replace("Sera nodded", "세라 nodded"), encoding="utf-8")
    assert main(["check", "b", "c0001"]) == 1
    path.write_text(clean, encoding="utf-8")
    assert main(["check", "b", "c0001-c0002"]) == 0
    first, last, chunks = check.summarise_log(proj)
    assert chunks == 2 and first["ERROR hangul"] == 1 and last["ERROR hangul"] == 0
    assert check.summarise_log(proj, {"c0002"})[0]["ERROR hangul"] == 0
    capsys.readouterr()
    main(["checklog", "b"])
    assert "ERROR hangul" in capsys.readouterr().out


def test_init_missing_file_is_a_clear_error(workdir):
    try:
        project.init(workdir / "nope.txt")
    except SystemExit as exc:
        assert "File not found" in str(exc)
    else:
        raise AssertionError("expected SystemExit")
