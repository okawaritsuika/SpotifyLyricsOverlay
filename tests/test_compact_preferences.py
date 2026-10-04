"""Validate compact-layout preferences and their appearance-settings controls."""
from copy import deepcopy
import tempfile
import tkinter as tk
import unittest
from unittest.mock import patch

from app import LyricsApp
from appearance import COMPACT_PRIORITIES, COMPACT_TARGETS, DEFAULT_COMPACT_LAYOUT, compact_from_settings
from storage import Store


class CompactSettingsTests(unittest.TestCase):
    def test_missing_or_invalid_container_returns_defaults(self):
        self.assertEqual(compact_from_settings({}), DEFAULT_COMPACT_LAYOUT)
        for value in (None, False, [], 'invalid', 6):
            with self.subTest(value=value):
                self.assertEqual(compact_from_settings({'compact_layout': value}), DEFAULT_COMPACT_LAYOUT)

    def test_modes_are_restricted_without_losing_valid_priorities(self):
        for mode in ('auto_hide', 'shrink_only'):
            result = compact_from_settings({'compact_layout': {'mode': mode, 'priorities': {'reading': 5}}})
            self.assertEqual(result['mode'], mode)
            self.assertEqual(result['priorities']['reading'], 5)
        for mode in (None, False, 'other', [], {}):
            with self.subTest(mode=mode):
                result = compact_from_settings({'compact_layout': {'mode': mode, 'priorities': {'reading': 5}}})
                self.assertEqual(result['mode'], 'auto_hide')
                self.assertEqual(result['priorities']['reading'], 5)

    def test_each_priority_accepts_only_integers_zero_through_six(self):
        for priority in range(7):
            supplied = {key: priority for key, _ in COMPACT_TARGETS}
            self.assertEqual(compact_from_settings({'compact_layout': {'priorities': supplied}})['priorities'], supplied)
        for priority in (False, True, -1, 7, 1.0, '2', None, [], {}):
            with self.subTest(priority=priority):
                supplied = {key: priority for key, _ in COMPACT_TARGETS}
                self.assertEqual(compact_from_settings({'compact_layout': {'priorities': supplied}}), DEFAULT_COMPACT_LAYOUT)

    def test_malformed_entries_preserve_valid_neighbors_and_drop_unknown_keys(self):
        supplied = {'title': 6, 'artists': 'bad', 'reading': 0, 'korean': 3, 'unknown': 1}
        result = compact_from_settings({'compact_layout': {'mode': 'shrink_only', 'priorities': supplied}})
        self.assertEqual(result, {'mode': 'shrink_only', 'priorities':
                                 {'title': 6, 'artists': 1, 'singers': 1, 'reading': 0, 'original': 0, 'korean': 3}})
        self.assertEqual(supplied['artists'], 'bad')
        for priorities in (None, [], 'bad', False):
            self.assertEqual(compact_from_settings({'compact_layout': {'priorities': priorities}}), DEFAULT_COMPACT_LAYOUT)

    def test_results_are_independent_of_defaults_and_saved_dictionary(self):
        saved = {'compact_layout': deepcopy(DEFAULT_COMPACT_LAYOUT)}
        first = compact_from_settings(saved)
        first['priorities']['title'] = 5
        self.assertEqual(saved['compact_layout']['priorities']['title'], 1)
        self.assertEqual(DEFAULT_COMPACT_LAYOUT['priorities']['title'], 1)
        self.assertEqual(compact_from_settings({})['priorities']['title'], 1)


class CompactPreferencesUITests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.store = Store(self.temp.name)
        self.store.save_settings({'online': False, 'transparent': False, 'borderless': False})
        self.root = tk.Tk()
        self.app = LyricsApp(self.root, self.store, start_services=False)
        self.root.update()
        self.app.open_appearance()
        self.dialog = self.app.appearance_dialog
        self.root.update()

    def tearDown(self):
        self.app.close()
        self.temp.cleanup()

    def select_priority(self, key, priority):
        combo = self.dialog.compact_combos[key]
        combo.current(priority)
        combo.event_generate('<<ComboboxSelected>>')
        self.root.update()

    def test_all_targets_have_readonly_choices_and_apply_selection_immediately(self):
        self.assertEqual(set(self.dialog.compact_combos), {key for key, _ in COMPACT_TARGETS})
        for key, combo in self.dialog.compact_combos.items():
            self.assertEqual(str(combo.cget('state')), 'readonly')
            self.assertEqual(tuple(combo.cget('values')), COMPACT_PRIORITIES)
            self.assertEqual(combo.current(), DEFAULT_COMPACT_LAYOUT['priorities'][key])
        original_styles = deepcopy(self.app.text_styles)
        with patch.object(self.app, 'apply_appearance', wraps=self.app.apply_appearance) as apply:
            self.select_priority('reading', 4)
            self.assertTrue(apply.called)
        self.assertEqual(self.app.compact_layout['priorities']['reading'], 4)
        self.assertEqual(self.app.text_styles, original_styles)

    def test_shrink_only_disables_priorities_without_discarding_choices(self):
        self.select_priority('title', 6)
        self.select_priority('artists', 0)
        self.select_priority('korean', 3)
        priorities = dict(self.app.compact_layout['priorities'])
        self.dialog.compact_radios['shrink_only'].invoke()
        self.root.update()
        self.assertEqual(self.app.compact_layout['mode'], 'shrink_only')
        self.assertTrue(all(str(combo.cget('state')) == 'disabled' for combo in self.dialog.compact_combos.values()))
        self.assertEqual(self.app.compact_layout['priorities'], priorities)
        self.dialog.compact_radios['auto_hide'].invoke()
        self.root.update()
        self.assertEqual(self.app.compact_layout['mode'], 'auto_hide')
        self.assertTrue(all(str(combo.cget('state')) == 'readonly' for combo in self.dialog.compact_combos.values()))
        self.assertEqual(self.app.compact_layout['priorities'], priorities)

    def test_close_saves_mode_and_priorities_and_reopening_restores_controls(self):
        self.select_priority('reading', 0)
        self.select_priority('original', 5)
        self.dialog.compact_radios['shrink_only'].invoke()
        expected = deepcopy(self.app.compact_layout)
        self.dialog.close()
        self.assertEqual(self.store.settings()['compact_layout'], expected)
        self.app.open_appearance()
        self.dialog = self.app.appearance_dialog
        self.root.update()
        self.assertEqual(self.dialog.compact_mode.get(), 'shrink_only')
        self.assertEqual(self.dialog.compact_combos['reading'].current(), 0)
        self.assertEqual(self.dialog.compact_combos['original'].current(), 5)
        self.assertTrue(all(str(combo.cget('state')) == 'disabled' for combo in self.dialog.compact_combos.values()))

    def test_reset_restores_default_order_and_reenables_choices(self):
        self.select_priority('title', 5)
        self.select_priority('korean', 2)
        self.dialog.compact_radios['shrink_only'].invoke()
        self.dialog.reset()
        self.root.update()
        self.assertEqual(self.app.compact_layout, DEFAULT_COMPACT_LAYOUT)
        self.assertEqual(self.dialog.compact_mode.get(), 'auto_hide')
        for key, combo in self.dialog.compact_combos.items():
            self.assertEqual(str(combo.cget('state')), 'readonly')
            self.assertEqual(combo.current(), DEFAULT_COMPACT_LAYOUT['priorities'][key])
        self.dialog.close()
        self.assertEqual(self.store.settings()['compact_layout'], DEFAULT_COMPACT_LAYOUT)

    def test_both_tabs_fit_at_minimum_window_size(self):
        self.dialog.window.geometry('560x510+20+20')
        for tab in (self.dialog.typography_tab, self.dialog.compact_tab):
            self.dialog.notebook.select(tab)
            self.root.update()
            self.root.update()
            self.assert_controls_fit(self.dialog.window)

    def assert_controls_fit(self, window):
        def visit(widget):
            for child in widget.winfo_children():
                if child.winfo_ismapped() and (isinstance(child, tk.Button) or child.winfo_class() in ('TCombobox', 'TSpinbox', 'TRadiobutton')):
                    self.assertGreater(child.winfo_width(), 15)
                    self.assertGreaterEqual(child.winfo_rootx(), window.winfo_rootx())
                    self.assertLessEqual(child.winfo_rootx() + child.winfo_width(), window.winfo_rootx() + window.winfo_width())
                    self.assertLessEqual(child.winfo_rooty() + child.winfo_height(), window.winfo_rooty() + window.winfo_height())
                visit(child)
        visit(window)


if __name__ == '__main__':
    unittest.main()
