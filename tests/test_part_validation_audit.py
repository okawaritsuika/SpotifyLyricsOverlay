"""Independent synthetic checks for false part attribution; no external data."""
import unittest
from types import SimpleNamespace

from lyric_parts import LyricParts,_recording,text_fingerprint
from lyrics import Line,normalized
from tools.build_lyric_part_expansion import aligned_values,parse_page,parts_of
from user_annotations import EMPTY,UserAnnotations


class PartValidationAuditTests(unittest.TestCase):
    def parse(self,body):
        html=('<div class="skin-entryBody"><p>'
              '<span style="color:#f00">天海春香</span><br>'
              '<span style="color:#00f">春日未来</span></p>'+body+'</div>')
        return parse_page(html,{'天海春香':'天海春香','春日未来':'春日未来'})

    def test_unregistered_color_ends_an_explicit_singer_scope(self):
        text,values,_=self.parse('<p>（春香）あいうえお</p>'
            '<p><span style="color:#0f0">'+'かきくけこ'*25+'</span></p>')
        self.assertEqual(text,'あいうえお'+'かきくけこ'*25)
        self.assertTrue(all(names==('天海春香',) for names in values[:5]))
        self.assertFalse(any(values[5:]),'An unknown color cannot inherit a previous named singer.')

    def test_mid_sentence_parenthetical_word_is_not_a_singer_label(self):
        original='あいうえお'*30+'（未来）'+'かきくけこ'*30
        text,values,_=self.parse('<p>'+original+'</p>')
        self.assertEqual(text,normalized(original),'Ordinary lyric parentheses must stay in the text.')
        self.assertFalse(any(values),'A matching character name inside a lyric is not an explicit credit.')
        mapped,_=aligned_values(text,normalized(original),values)
        self.assertIsNotNone(mapped)
        self.assertFalse(parts_of(mapped))

    def test_real_line_start_label_split_across_html_nodes_remains_usable(self):
        text,values,_=self.parse('<p><span>（春</span><span>香）</span>'+'あいうえお'*25+'</p>')
        self.assertEqual(text,'あいうえお'*25)
        self.assertTrue(all(names==('天海春香',) for names in values))

    def catalog(self,partial_singer):
        lines=(Line('青い空を見る','',''),)
        fingerprint,length=text_fingerprint(lines)
        metadata={'title':'Synthetic Song','artists':['Synthetic Unit'],'album':'Synthetic Album',
                  'text_sha256':fingerprint,'text_length':length,
                  'sources':[{'url':'https://example.org/parts','kind':'fan_part_transcription','note':'Synthetic fixture.'}]}
        full={**metadata,'parts':[{'start':0,'end':length,'singers':['天海春香']}]}
        partial={**metadata,'parts':[{'start':0,'end':2,'singers':[partial_singer]}]}
        catalog=LyricParts.__new__(LyricParts)
        catalog.recordings=[_recording(full),_recording(partial)]
        playback=SimpleNamespace(title=metadata['title'],artist=metadata['artists'][0],album=metadata['album'])
        return catalog,playback,lines

    def test_conflicting_partial_source_blocks_a_complete_row_attribution(self):
        catalog,playback,lines=self.catalog('春日未来')
        self.assertEqual(catalog.match(playback,lines),((),))
        annotations=UserAnnotations.__new__(UserAnnotations);annotations.parts=catalog
        row=annotations._with_parts(playback,lines,(),(),[EMPTY])[0]
        self.assertFalse(row.singers,'The UI must not prefer an unchecked complete claim over a conflicting fragment.')

    def test_consistent_partial_source_does_not_negate_a_complete_fact(self):
        catalog,playback,lines=self.catalog('天海春香')
        self.assertEqual(catalog.match(playback,lines),(('天海春香',),))


if __name__=='__main__':unittest.main()
