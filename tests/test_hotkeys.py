import unittest
from shortcuts import DEFAULTS,ShortcutMatcher,validate_bindings,shortcut_label


RIGHT_SHIFT_ENTER={'key':0x0D,'modifiers':4,'shift':'right'}


class ShortcutTests(unittest.TestCase):
    def test_arrows_pass_through_outside_lyrics_focus(self):
        shortcut=ShortcutMatcher({'next':{'key':0x27,'modifiers':0,'shift':'any'}})
        self.assertEqual(shortcut.event(0x27,True,enabled=False),(False,None))
        self.assertEqual(shortcut.event(0x27,True,enabled=True),(False,None))
        self.assertEqual(shortcut.event(0x27,False,enabled=True),(False,None))
        self.assertEqual(shortcut.event(0x27,True,enabled=True),(True,'next'))
        self.assertEqual(shortcut.event(0x27,False,enabled=False),(False,None))

    def test_side_specific_chord_does_not_fire_in_other_app(self):
        shortcut=ShortcutMatcher({'stamp':RIGHT_SHIFT_ENTER})
        shortcut.event(0xA1,True,enabled=False)
        self.assertEqual(shortcut.event(0x0D,True,enabled=False),(False,None))
        self.assertEqual(shortcut.event(0x0D,False,enabled=False),(False,None))
        self.assertEqual(shortcut.event(0x0D,True,enabled=True),(True,'stamp'))

    def test_right_shift_stamp_records_once_and_consumes_its_keyup(self):
        shortcut=ShortcutMatcher({'stamp':RIGHT_SHIFT_ENTER})
        self.assertEqual(shortcut.event(0xA1,True),(False,None))
        self.assertEqual(shortcut.event(0x0D,True),(True,'stamp'))
        self.assertEqual(shortcut.event(0x0D,True),(True,None))
        shortcut.event(0xA1,False)
        self.assertEqual(shortcut.event(0x0D,False),(True,None))
        self.assertEqual(shortcut.event(0x0D,True),(False,None))
    def test_left_shift_plain_enter_and_other_modifiers_pass_through(self):
        for held in ([],[0xA0],[0xA1,0xA2],[0xA1,0xA3],[0xA1,0xA4],[0xA1,0x5B]):
            shortcut=ShortcutMatcher({'stamp':RIGHT_SHIFT_ENTER},held)
            self.assertEqual(shortcut.event(0x0D,True),(False,None))
            self.assertEqual(shortcut.event(0x0D,False),(False,None))
    def test_holding_enter_before_shift_does_not_stamp_on_repeat(self):
        shortcut=ShortcutMatcher({'stamp':RIGHT_SHIFT_ENTER})
        shortcut.event(0x0D,True);shortcut.event(0xA1,True)
        self.assertEqual(shortcut.event(0x0D,True),(False,None))
        shortcut.event(0x0D,False)
        self.assertEqual(shortcut.event(0x0D,True),(True,'stamp'))
    def test_custom_key_disabled_actions_and_initial_held_keys(self):
        shortcut=ShortcutMatcher({'stamp':{'key':0x78,'modifiers':0,'shift':'any'},'align':None},[0x78])
        self.assertEqual(shortcut.event(0x78,True),(False,None))
        shortcut.event(0x78,False)
        self.assertEqual(shortcut.event(0x78,True),(True,'stamp'))
        self.assertEqual(shortcut.event(0x41,True),(False,None));self.assertNotIn(0x41,shortcut.down)
    def test_validation_rejects_overlaps_and_plain_typing_but_accepts_distinct_shift_sides(self):
        with self.assertRaises(ValueError):validate_bindings({'previous':DEFAULTS['next']})
        with self.assertRaises(ValueError):validate_bindings({'previous':{'key':0x41,'modifiers':0,'shift':'any'}})
        with self.assertRaises(ValueError):validate_bindings({'previous':{'key':0x0D,'modifiers':4,'shift':'any'},'next':RIGHT_SHIFT_ENTER})
        result=validate_bindings({'previous':RIGHT_SHIFT_ENTER,'next':{'key':0x0D,'modifiers':4,'shift':'left'},'lock':None})
        self.assertIsNone(result['lock']);self.assertIn('오른쪽 Shift',shortcut_label(result['previous']))
        self.assertIn('왼쪽 Shift',shortcut_label(result['next']))

    def test_defaults_are_not_mutated_by_edits(self):
        values=validate_bindings();values['previous']['key']=0x79
        self.assertEqual(DEFAULTS['previous']['key'],0x26)

    def test_removed_overrides_are_ignored(self):
        result=validate_bindings({'stamp':{'key':0x41,'modifiers':0,'shift':'any'},'align':None})
        self.assertEqual(result,DEFAULTS)
        self.assertNotIn('stamp',result)
        self.assertNotIn('align',result)

    def test_four_plain_direction_keys_are_configurable_without_modifiers(self):
        chosen={action:{'key':key,'modifiers':0,'shift':'any'} for action,key in
                [('previous',0x25),('next',0x27),('lock',0x28),('chrome',0x26)]}
        matcher=ShortcutMatcher(validate_bindings(chosen))
        for action,binding in chosen.items():
            self.assertEqual(matcher.event(binding['key'],True),(True,action))
            self.assertEqual(matcher.event(binding['key'],True),(True,None))
            matcher.event(binding['key'],False)
