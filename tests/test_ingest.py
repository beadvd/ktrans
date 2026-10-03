import zipfile

from conftest import make_epub

from kt import ingest
from kt.text import normalise_ellipses, smart_quotes


def test_epub_chapters_headings_markup_and_breaks(tmp_path):
    book = ingest.load(make_epub(tmp_path / "b.epub"))
    assert book.title == "낯선 성의 검사" and book.author == "김작가"
    assert book.cover == "cover.png"
    assert [c.title for c in book.chapters] == ["제1화 낯선 성", "제2화 첫 번째 시험"]
    kinds = [b.kind for b in book.chapters[0].blocks]
    assert kinds == ["h", "p", "p", "p", "sep", "p", "p"]
    assert book.chapters[0].blocks[3].text == "세라가 {i}조용히{/i} 고개를 끄덕였다."
    # "……" is a beat of silence, not a divider.
    last = book.chapters[0].blocks[-1]
    assert (last.kind, last.text) == ("p", "……") and last.loc is not None


def test_epub_continuation_file_joins_previous_chapter(tmp_path):
    book = ingest.load(make_epub(tmp_path / "b.epub", split_second_file=True))
    assert len(book.chapters) == 2
    assert book.chapters[1].blocks[-1].text == "그날 밤 유현은 오래 걸었다."


def test_one_paragraph_with_brs_is_split(tmp_path):
    body = "<p>" + "<br/>".join(["가나다라마바사 아자차카타파하 " * 6] * 6) + "</p>"
    blocks = ingest.html_blocks(f"<html><body>{body}</body></html>")
    assert len(blocks) == 6 and all(b.kind == "p" for b in blocks)


def test_angle_brackets_are_text_and_braces_neutralised():
    blocks = ingest.html_blocks("<body><p>&lt;상태창&gt; {x}</p></body>")
    assert blocks[0].text == "<상태창> (x)"


def test_txt_cp949_and_headings(tmp_path):
    text = "제1화 시작\n\n그는 문을 열었다.\n\"누구야?\"\n\n제2화 끝\n\n비가 내렸다.\n"
    path = tmp_path / "novel.txt"
    path.write_bytes(text.encode("cp949"))
    book = ingest.load(path)
    assert [c.title for c in book.chapters] == ["제1화 시작", "제2화 끝"]
    assert [b.text for b in book.chapters[0].blocks] == ["제1화 시작", "그는 문을 열었다.", "\"누구야?\""]


def test_prose_starting_with_a_number_is_not_a_heading():
    assert not ingest.looks_like_heading("3장 이후로 그는 말이 없었다.")
    assert ingest.looks_like_heading("제 12 화")
    assert ingest.looks_like_heading("에필로그")


def test_docx(tmp_path):
    w = 'xmlns:w="http://schemas.openxmlformats.org/wordprocessingml/2006/main"'
    doc = (
        f'<w:document {w}><w:body>'
        '<w:p><w:pPr><w:pStyle w:val="1"/></w:pPr><w:r><w:t>첫 장</w:t></w:r></w:p>'
        '<w:p><w:r><w:t>그녀는 </w:t></w:r><w:r><w:rPr><w:i/></w:rPr><w:t>천천히</w:t></w:r><w:r><w:t> 걸었다.</w:t></w:r></w:p>'
        '</w:body></w:document>'
    )
    styles = f'<w:styles {w}><w:style w:styleId="1"><w:name w:val="heading 1"/></w:style></w:styles>'
    path = tmp_path / "n.docx"
    with zipfile.ZipFile(path, "w") as z:
        z.writestr("word/document.xml", doc)
        z.writestr("word/styles.xml", styles)
    book = ingest.load(path)
    assert book.chapters[0].title == "첫 장"
    assert book.chapters[0].blocks[1].text == "그녀는 {i}천천히{/i} 걸었다."


def test_typography():
    assert smart_quotes('"Hi," she said. \'No.\' It\'s \'em') == "“Hi,” she said. ‘No.’ It’s ’em"
    assert normalise_ellipses("그…… 그래…….") == "그... 그래..."
