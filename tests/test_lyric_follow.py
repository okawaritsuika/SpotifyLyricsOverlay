import unittest
from lyrics import Line
from lyric_follow import nearby_line,native_line_mapping

class ApproximateFollowTests(unittest.TestCase):
    def test_small_wording_difference_advances_without_exact_match(self):
        lines=tuple(Line(text,'','') for text in ['静かな空を見上げる','新しい道を歩いてゆく','次の街を目指して'])
        self.assertEqual(nearby_line('新しい道を歩いていく',lines,1,0,1),1)
    def test_merged_spotify_line_can_stay_on_same_target_but_repeated_rows_advance(self):
        lines=(Line('静かな空を見上げる 新しい道を歩く','',''),Line('次の街を目指して','',''))
        self.assertEqual(nearby_line('新しい道を歩く',lines,1,0,1),0)
        repeated=(Line('同じ場所に立って','',''),)*2
        self.assertEqual(nearby_line('同じ場所に立って',repeated,1,0,1),1)
    def test_unrelated_source_cannot_advance_to_an_expected_row(self):
        lines=tuple(Line(text,'','') for text in ['Red sunrise','Blue river','Green forest'])
        self.assertIsNone(nearby_line('zzzzzzzz',lines,1))


class NativeLineMappingTests(unittest.TestCase):
    def mapping(self,source,target):
        return native_line_mapping(source,tuple(Line(text,'','') for text in target))

    def test_untranslated_backing_rows_are_unmapped_between_exact_anchors(self):
        target=['Red sunrise','Blue river','Green forest','Silver clouds']
        source=[target[0],'zzzzzzzz','qqqqqqqq','xxxxxxxx',*target[1:]]
        self.assertEqual(self.mapping(source,target),({0:0,4:1,5:2,6:3},set()))

    def test_four_fragments_stay_on_one_translation_row(self):
        pieces=['aaaaaa','bbbbbb','cccccc','dddddd']
        target=['Red sunrise',''.join(pieces),'Silver clouds','Green forest']
        source=[target[0],*pieces,*target[2:]]
        mapping,grouped=self.mapping(source,target)
        self.assertEqual(mapping,{0:0,1:1,2:1,3:1,4:1,5:2,6:3})
        self.assertEqual(grouped,{(index,1) for index in range(1,5)})

    def test_merged_source_maps_to_the_first_translation_it_touches(self):
        target=['Red sunrise','Blue river','Green forest','Silver clouds']
        self.assertEqual(self.mapping([target[0],target[1]+' '+target[2],target[3]],target),
                         ({0:0,1:1,2:3},{(1,1)}))

    def test_different_split_boundaries_align_as_one_contiguous_span(self):
        self.assertEqual(self.mapping(['abcd','efghijklmnop','Sunrise'],
                                      ['abcdefgh','ijklmnop','Sunrise']),
                         ({0:0,1:0,2:2},{(0,0),(1,0)}))

    def test_onset_mapping_requires_the_same_start_of_a_complete_matched_span(self):
        target=tuple(Line(text,'','') for text in ['ab','cdef','Sunrise'])
        self.assertEqual(native_line_mapping(['abcd','ef','Sunrise'],target,onsets_only=True),
                         ({0:0,2:2},{(0,0)}))
        target=tuple(Line(text,'','') for text in ['abcdef','Sunrise'])
        self.assertEqual(native_line_mapping(['abcd','ef','Sunrise'],target,onsets_only=True),
                         ({0:0,2:1},{(0,0)}))

    def test_long_split_refrains_match_their_occurrences_in_order(self):
        pieces=['aaaaaa','bbbbbb','cccccc','dddddd','eeeeee','ffffff']
        refrain=''.join(pieces)
        target=['Red sunrise',refrain,'Blue river',refrain,'Silver clouds']
        source=[target[0],*pieces,target[2],*pieces,target[4]]
        mapping,grouped=self.mapping(source,target)
        self.assertEqual([mapping[index] for index in range(len(source))],
                         [0,*([1]*6),2,*([3]*6),4])
        self.assertEqual(grouped,{(index,1) for index in range(1,7)}|
                                 {(index,3) for index in range(8,14)})

    def test_exact_repeated_rows_use_the_first_occurrence_and_keep_one_to_one_pairs(self):
        self.assertEqual(self.mapping(['Same phrase','Same phrase'],['Same phrase','Same phrase']),
                         ({0:0,1:1},set()))
        self.assertEqual(self.mapping(['Same phrase','Same phrase'],['Same phrase']),({0:0},set()))

    def test_empty_and_unrelated_rows_never_form_matches(self):
        self.assertEqual(self.mapping(['♪','zzzzzzzz'],['','Blue river']),({},set()))
        self.assertEqual(self.mapping([],['Blue river']),({},set()))

    def test_concatenation_bound_does_not_accept_seven_source_fragments(self):
        pieces=['aaaaaa','bbbbbb','cccccc','dddddd','eeeeee','ffffff','gggggg']
        self.assertEqual(self.mapping(pieces,[''.join(pieces)]),({},set()))
