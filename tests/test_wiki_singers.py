import unittest

from lyric_parts import LyricParts, text_fingerprint
from lyrics import Line, normalized
from wiki_singers import extract_colored_parts


LEGEND = '''
<tr><td colspan="2">캐릭터 이미지컬러</td></tr>
<tr><td style="background-color:rgb(238,118,46)"></td><td>타마키</td></tr>
<tr><td style="background-color:#f7e78e"></td><td>이쿠</td></tr>
<tr><td style="background-color:#efb864"></td><td>모모코</td></tr>
<tr><td style="background-color:#fff"></td><td>합창</td></tr>
'''
TAMAKI, IKU, MOMOKO = '오오가미 타마키', '나카타니 이쿠', '스오 모모코'
ALL = (TAMAKI, IKU, MOMOKO)


def table(body, legend=LEGEND):
    return '<table>' + legend + '<tr><td colspan="2" style="color:#fff;background-color:#555">' + body + '</td></tr></table>'


def rows(*values):
    return tuple(Line(value, '', '') for value in values)


def credits(result, lines, partial=False):
    parts = tuple((part['start'], part['end'], tuple(part['singers'])) for part in result['parts'])
    names, cursor = [], 0
    for line in lines:
        end = cursor + len(normalized(line.original))
        names.append(LyricParts._row_singers(cursor, end, parts, partial))
        cursor = end
    return tuple(names)


class WikiSingersTests(unittest.TestCase):
    def test_recording_credits_resolve_ambiguous_given_name_and_chorus(self):
        legend = '''<tr><td style="background-color:#d7385f"></td><td>줄리아</td></tr>
        <tr><td style="background-color:#454341"></td><td>메구미</td></tr>
        <tr><td style="background-color:#999"></td><td>합창</td></tr>'''
        html = table('<span style="color:#999">空</span><br>'
                     '<span style="color:#454341">星</span><br>'
                     '<span style="color:#d7385f">月</span><br>'
                     '<span style="color:#d7385f">風</span>'
                     '<span style="color:#454341">雨</span>', legend)
        lines = rows('空', '星', '月', '風雨')
        self.assertEqual(credits(extract_colored_parts(html, lines), lines),
                         ((), (), ('줄리아',), ()))
        for artists in (('ジュリア (CV.愛美)', '所 恵美 (CV.藤井ゆきよ)'),
                        ('줄리아', '토코로 메구미')):
            with self.subTest(artists=artists):
                result = extract_colored_parts(html, lines, artists=artists)
                self.assertEqual(credits(result, lines),
                    (('줄리아', '토코로 메구미'), ('토코로 메구미',),
                     ('줄리아',), ('줄리아', '토코로 메구미')))
                self.assertEqual(result['parts'][-2:],
                    [{'start': 2, 'end': 4, 'singers': ['줄리아']},
                     {'start': 4, 'end': 5, 'singers': ['토코로 메구미']}])
        for artists in (('줄리아',), ('토코로 메구미', '이쥬인 메구미'),
                        ('토코로 메구미와 이쥬인 메구미',), ('후지이 유키요',)):
            with self.subTest(ambiguous_artists=artists):
                result = extract_colored_parts(html, lines, artists=artists)
                self.assertEqual(credits(result, lines), ((), (), ('줄리아',), ()))

    def test_context_does_not_add_unlabelled_singers_to_chorus(self):
        lines = rows('空')
        result = extract_colored_parts(table('空'), lines,
                                       artists=('토코로 메구미', '줄리아'))
        self.assertEqual(credits(result, lines), (ALL,))

    def test_nested_colors_chorus_and_translation_do_not_override_original(self):
        html = table('<span style="color:#f7e78e">青い<span style="color:rgb(238,118,46)">空</span>'
                     '<br>아오이 소라<br>푸른 하늘</span><br>'
                     '<span style="color:#efb864">星</span><br>'
                     '<span style="color:#ee762e">호시<br>별</span><br>'
                     '歌う<br>우타우<br>노래한다')
        lines = rows('青い空', '星', '歌う')
        result = extract_colored_parts(html, lines)
        self.assertEqual(credits(result, lines), ((IKU, TAMAKI), (MOMOKO,), ALL))
        self.assertEqual((result['text_sha256'], result['text_length']), text_fingerprint(lines))

    def test_repeated_phrase_uses_its_occurrence_and_english_aside_position(self):
        html = table('<span style="color:#efb864">Hello</span><br>'
                     '<span style="color:#ee762e">同じ言葉</span><br>'
                     '<span style="color:#f7e78e">同じ言葉</span>')
        lines = rows('Hello', '同じ言葉', '同じ言葉')
        self.assertEqual(credits(extract_colored_parts(html, lines), lines),
                         ((MOMOKO,), (TAMAKI,), (IKU,)))

    def test_unknown_explicit_color_does_not_inherit_parent_or_global_character(self):
        html = table('青い<span style="color:#123456">空</span><br><span style="color:var(--singer)">星</span>')
        lines = rows('青い空', '星')
        result = extract_colored_parts(html, lines)
        self.assertEqual(credits(result, lines), ((), ()))
        self.assertEqual(credits(result, lines, True), (ALL, ()))

    def test_no_legend_never_guesses_a_character_from_a_familiar_color(self):
        self.assertIsNone(extract_colored_parts(table('<span style="color:#ee762e">青い空</span>', ''), rows('青い空')))

    def test_duplicate_color_labels_are_ambiguous_but_other_colors_survive(self):
        legend = LEGEND.replace('#f7e78e', '#ee762e')
        html = table('<span style="color:#ee762e">空</span><br><span style="color:#efb864">星</span>', legend)
        lines = rows('空', '星')
        self.assertEqual(credits(extract_colored_parts(html, lines), lines), ((), (MOMOKO,)))

    def test_unresolved_legend_member_keeps_chorus_unknown(self):
        html = table('空<br><span style="color:#efb864">星</span>', LEGEND.replace('타마키', '미확인 가수'))
        lines = rows('空', '星')
        self.assertEqual(credits(extract_colored_parts(html, lines), lines), ((), (MOMOKO,)))

    def test_shared_given_name_never_chooses_a_character_by_image_color(self):
        html = table('<span style="color:#ee762e">空</span><br><span style="color:#efb864">星</span>',
                     LEGEND.replace('타마키', '나오'))
        lines = rows('空', '星')
        self.assertEqual(credits(extract_colored_parts(html, lines), lines), ((), (MOMOKO,)))

    def test_explicit_combination_swatch_and_colored_name_legend(self):
        legend = '<tr><td style="background-color:#abc"></td><td>타마키 / 이쿠</td></tr>'
        html = table('<span style="color:#aabbcc">空</span>', legend)
        self.assertEqual(credits(extract_colored_parts(html, rows('空')), rows('空')), ((TAMAKI, IKU),))
        legend = '<tr><td><span style="color:#abc">타마키</span> <span style="color:#fff">ALL</span></td></tr>'
        html = table('<span style="color:#abc">空</span><br>星', legend)
        self.assertEqual(credits(extract_colored_parts(html, rows('空', '星')), rows('空', '星')),
                         ((TAMAKI,), (TAMAKI,)))

    def test_full_original_match_rejects_changed_missing_and_extra_rows(self):
        html = table('<span style="color:#ee762e">空</span><br><span style="color:#efb864">星</span>')
        for lines in (rows('空'), rows('海', '星'), rows('空', '星', '月'), rows('星', '空')):
            with self.subTest(lines=lines):self.assertIsNone(extract_colored_parts(html, lines))
        combined = rows('空星')
        self.assertEqual(credits(extract_colored_parts(html, combined), combined), ((TAMAKI, MOMOKO),))

    def test_footnotes_hidden_nodes_and_block_wrappers_preserve_offsets(self):
        html = table('<div><span style="color:#ee762e">空<a href="#fn-1">[1]</a></span></div>'
                     '<div><span style="color:#f7e78e">星<sup>2</sup>[3]</span>'
                     '<span hidden>moon</span><span style="display:none">sun</span></div>')
        lines = rows('空', '星')
        self.assertEqual(credits(extract_colored_parts(html, lines), lines), ((TAMAKI,), (IKU,)))

    def test_browser_excluded_tooltips_and_hidden_content_preserve_visible_originals(self):
        hidden = ('role="tooltip"', 'aria-hidden="true"', 'hidden', 'style="display:none"',
                  'style="visibility:hidden"', 'style="visibility:collapse"',
                  'style="content-visibility:hidden"', 'style="opacity:0"', 'style="opacity:0.0"')
        lines = rows('空')
        for attributes in hidden:
            with self.subTest(attributes=attributes):
                html = table('<span style="color:#ee762e">空</span><span ' + attributes +
                             '><span style="color:#efb864">hidden words</span></span>')
                self.assertEqual(credits(extract_colored_parts(html, lines), lines), ((TAMAKI,),))
        html = table('<span style="color:#ee762e">空</span><template>hidden words</template><!--comment-->')
        self.assertEqual(credits(extract_colored_parts(html, lines), lines), ((TAMAKI,),))

    def test_hidden_legend_member_is_excluded_from_chorus(self):
        for attribute in ('hidden', 'aria-hidden="true"', 'style="display:none"', 'role="tooltip"'):
            with self.subTest(attribute=attribute):
                legend = LEGEND.replace('<tr><td style="background-color:#efb864">',
                                        '<tr ' + attribute + '><td style="background-color:#efb864">')
                lines = rows('空')
                result = extract_colored_parts(table('空', legend), lines)
                self.assertEqual(credits(result, lines), ((TAMAKI, IKU),))

    def test_hidden_parent_or_legend_never_supplies_a_profile(self):
        html = table('<span style="color:#ee762e">空</span>')
        self.assertIsNone(extract_colored_parts('<div hidden>' + html + '</div>', rows('空')))
        self.assertIsNone(extract_colored_parts(table('空', '<tbody hidden>' + LEGEND + '</tbody>'), rows('空')))

    def test_conflicting_matching_tables_fail_closed(self):
        first = table('<span style="color:#ee762e">空</span>')
        second = table('<span style="color:#f7e78e">空</span>')
        self.assertIsNone(extract_colored_parts(first + second, rows('空')))
        self.assertIsNotNone(extract_colored_parts(first + first, rows('空')))


if __name__ == '__main__':unittest.main()
