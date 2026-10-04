import unittest
from unittest.mock import patch
from web_lyrics import parse_table_text,parse_html,validate_url,reading,fetch_lyrics

TABLE='窓を開ける\n마도오 아케루\n창문을 열다\n\n花を描く\n하나오 카쿠\n꽃을 그리다'
HTML='<html><h1>Example Song</h1><h2>1. 개요</h2><table><tr><td>가사 아님</td></tr></table><h2>3. 가사 [편집]</h2><table><tr><td>'+TABLE.replace('\n','<br>')+'</td></tr></table><h2>4. 기타</h2><p>다른 내용</p></html>'

ORIGINALS=tuple(f'Synthetic original phrase number {index}' for index in range(24))

def lyric_table(rows):
    return '<table><tr><td>'+'<br>'.join(value for row in rows for value in (row,'한글 발음','한국어 번역'))+'</td></tr></table>'

def lyric_document(body):
    return '<h1>Example Song</h1><h2>3. 가사</h2>'+body+'<h2>4. 기타</h2>'

def version_document(prefix=''):
    return lyric_document('<table><tr><td>'+prefix+lyric_table(ORIGINALS)*3+'</td></tr></table>')


class WebLyricsTests(unittest.TestCase):
    def test_original_pronunciation_translation_do_not_shift(self):
        lines=parse_table_text(TABLE,reader=lambda text:'독음')
        self.assertEqual([l.korean for l in lines],['창문을 열다','꽃을 그리다'])
        self.assertEqual([l.original for l in lines],['窓を開ける','花を描く'])

    def test_markdown_table_br_and_footnotes(self):
        text='| '+TABLE.replace('\n','<br>')+'[1] |\n| :---: |'
        self.assertEqual(len(parse_table_text(text,reader=lambda t:t)),2)

    def test_english_refrain_does_not_shift_neighbor_translations(self):
        text=TABLE+'\nOpen the window\n오픈 더 윈도우\n창문을 열어\n夜が来る\n요루가 쿠루\n밤이 와'
        lines=parse_table_text(text,reader=lambda t:t)
        self.assertEqual(len(lines),4)
        self.assertEqual(lines[2].original,'Open the window')
        self.assertEqual(lines[3].korean,'밤이 와')

    def test_untranslated_english_refrain_keeps_its_place_in_html_and_pasted_tables(self):
        text=TABLE+'\nOpen the window\n夜が来る\n요루가 쿠루\n밤이 와\nOpen the window'
        expected=['창문을 열다','꽃을 그리다','','밤이 와','']
        lines=parse_table_text(text,reader=lambda t:t)
        self.assertEqual([line.korean for line in lines],expected)
        html=HTML.replace(TABLE.replace('\n','<br>'),text.replace('\n','<br data-v-example="">'))
        lines,_=parse_html(html,'Example Song',reader=lambda t:t)
        self.assertEqual([line.korean for line in lines],expected)

    def test_html_break_attributes_preserve_translation_groups_when_pasted(self):
        text='<div>'+TABLE.replace('\n','<br data-v-example="">')+'</div>'
        self.assertEqual(len(parse_table_text(text,reader=lambda t:t)),2)

    def test_english_with_lone_pronunciation_remains_incomplete(self):
        with self.assertRaisesRegex(ValueError,'불완전'):
            parse_table_text(TABLE+'\nOpen the window\n오픈 더 윈도우',reader=lambda t:t)
        with self.assertRaises(ValueError):
            parse_table_text('Open the window\nClose the door',reader=lambda t:t)

    def test_incomplete_pronunciation_is_not_used_as_translation(self):
        with self.assertRaisesRegex(ValueError,'불완전'):
            parse_table_text(TABLE+'\n夜が来る\n요루가 쿠루',reader=lambda t:t)

    def test_live_table_keeps_untranslated_refrain_and_neighbor_translations(self):
        text='Open the window\n'+TABLE+'\n夜が来る\n요루가 쿠루'
        lines=parse_table_text(text,reader=lambda t:t,strict=False)
        self.assertEqual([line.korean for line in lines],['','창문을 열다','꽃을 그리다',''])
        self.assertEqual(lines[0].original,'Open the window')
        original_only=parse_table_text('Open the window\n夜が来る\n요루가 쿠루',reader=lambda t:t,strict=False)
        self.assertEqual([line.original for line in original_only],['Open the window','夜が来る'])
        self.assertTrue(all(not line.korean for line in original_only))

    def test_verified_live_table_accepts_japanese_or_english_originals_without_translation(self):
        for text in ('窓を開ける\n花を描く\n窓を開ける','Open the window\nDraw a flower','窓を開ける'):
            with self.subTest(text=text):
                lines=parse_table_text(text,reader=lambda t:'reading',strict=False)
                self.assertEqual([line.original for line in lines],text.splitlines())
                self.assertTrue(all(line.reading=='reading' and not line.korean for line in lines))
                with self.assertRaises(ValueError):parse_table_text(text,reader=lambda t:t)

    def test_originals_with_only_hangul_pronunciation_never_use_it_as_translation(self):
        text='窓を開ける\n마도오 아케루\n花を描く\n하나오 카쿠'
        lines=parse_table_text(text,reader=lambda t:'reading',strict=False)
        self.assertEqual([line.original for line in lines],['窓を開ける','花を描く'])
        self.assertEqual([line.korean for line in lines],['',''])
        with self.assertRaises(ValueError):parse_table_text(text,reader=lambda t:t)

    def test_live_table_still_rejects_empty_or_hangul_only_content(self):
        for text in ('','♪\n---','한글 발음\n한국어 설명'):
            with self.subTest(text=text),self.assertRaises(ValueError):
                parse_table_text(text,reader=lambda t:t,strict=False)

    def test_html_scopes_lyrics_section_and_verifies_title(self):
        lines,title=parse_html(HTML,'Example Song',reader=lambda t:t)
        self.assertEqual(len(lines),2)
        self.assertEqual(title,'Example Song')
        with self.assertRaisesRegex(ValueError,'제목'):
            parse_html(HTML,'Different',reader=lambda t:t)

    def test_missing_lyrics_section_is_rejected(self):
        with self.assertRaisesRegex(ValueError,'섹션'):
            parse_html(HTML.replace('3. 가사','3. 영상'),'Example Song')

    def test_local_furigana_is_hiragana_and_not_hangul_pronunciation(self):
        text=reading('窓を開ける')
        self.assertIn('まど',text)
        self.assertIn('あける',text.replace(' ',''))

    def test_only_public_wiki_document_urls_are_accepted(self):
        for url in ['http://namu.wiki/w/Test','https://evil.test/w/Test','https://namu.wiki@evil.test/w/Test',
                    'https://namu.wiki:8000/w/Test','https://namu.wiki/edit/Test','file:///C:/secret']:
            with self.assertRaises(ValueError):validate_url(url)

    def test_fetch_can_be_verified_without_network_or_user_data(self):
        lines,source=fetch_lyrics('Example Song',fetch=lambda url:HTML)
        self.assertEqual(len(lines),2)
        self.assertEqual(source['url'],'https://namu.wiki/w/Example%20Song')

    def test_nested_full_version_copies_select_one_complete_referenced_song(self):
        lines,_=parse_html(version_document(),'Example Song',reader=lambda text:'reading',reference_lines=ORIGINALS)
        self.assertEqual(tuple(row.original for row in lines),ORIGINALS)
        self.assertTrue(all(row.reading=='reading' and row.korean=='한국어 번역' for row in lines))

    def test_nested_versions_without_complete_provider_evidence_remain_unchanged(self):
        for references in ((),ORIGINALS[:2],ORIGINALS*3,tuple(row.replace('original','unrelated') for row in ORIGINALS)):
            with self.subTest(reference_count=len(references)):
                lines,_=parse_html(version_document(),'Example Song',reader=lambda text:'reading',reference_lines=references)
                self.assertEqual(tuple(row.original for row in lines),ORIGINALS*3)

    def test_real_repetition_inside_one_table_is_preserved(self):
        lines,_=parse_html(lyric_document(lyric_table(ORIGINALS*3)),'Example Song',reader=lambda text:'reading',reference_lines=ORIGINALS)
        self.assertEqual(tuple(row.original for row in lines),ORIGINALS*3)
        pasted='\n'.join(value for row in ORIGINALS*3 for value in (row,'한글 발음','한국어 번역'))
        self.assertEqual(tuple(row.original for row in parse_table_text(pasted,reader=lambda text:'reading')),ORIGINALS*3)

    def test_extra_outer_verses_prevent_truncating_nested_choruses(self):
        prefix='A unique introductory verse<br>한글 발음<br>한국어 번역<br>'
        lines,_=parse_html(version_document(prefix),'Example Song',reader=lambda text:'reading',reference_lines=ORIGINALS)
        self.assertEqual(tuple(row.original for row in lines),('A unique introductory verse',)+ORIGINALS*3)

    def test_one_nested_copy_wrapped_twice_is_not_two_version_copies(self):
        wrapped='<table><tr><td>'+lyric_table(ORIGINALS)+'</td></tr></table>'
        body='<table><tr><td>'+lyric_table(ORIGINALS).removeprefix('<table><tr><td>').removesuffix('</td></tr></table>')+'<br>'+wrapped+'</td></tr></table>'
        lines,_=parse_html(lyric_document(body),'Example Song',reader=lambda text:'reading',reference_lines=ORIGINALS)
        self.assertEqual(tuple(row.original for row in lines),ORIGINALS*2)

    def test_version_subheadings_stay_inside_lyrics_section(self):
        html=lyric_document('<h3>3.1. Full version</h3>'+lyric_table(ORIGINALS))
        lines,_=parse_html(html,'Example Song',reader=lambda text:'reading',reference_lines=ORIGINALS)
        self.assertEqual(tuple(row.original for row in lines),ORIGINALS)

    def test_http_fetch_forwards_provider_evidence_to_html_selection(self):
        from lyrics import Line
        references=tuple(Line(row,'','') for row in ORIGINALS)
        with patch('web_lyrics.parse_html',wraps=parse_html) as parser:
            lines,_=fetch_lyrics('Example Song',fetch=lambda url:version_document(),reference_lines=references)
        self.assertEqual(tuple(row.original for row in lines),ORIGINALS)
        self.assertEqual(parser.call_args.kwargs['reference_lines'],references)


if __name__=='__main__':unittest.main()
