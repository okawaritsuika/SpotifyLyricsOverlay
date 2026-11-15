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
        combo = self.dialog.combos[key]
        combo.current(priority)
        combo.event_generate('<<ComboboxSelected>>')
        self.root.update()

    def test_selection_remains_draft_until_apply(self):
        self.assertEqual(set(self.dialog.combos), {key for key, _ in COMPACT_TARGETS})
        before=deepcopy(self.app.compact_layout)
        self.select_priority('reading', 4)
        self.assertEqual(self.app.compact_layout,before)
        self.assertEqual(self.dialog.draft()['compact_layout']['priorities']['reading'],4)
        self.dialog.apply()
        self.assertEqual(self.app.compact_layout['priorities']['reading'],4)
        self.assertEqual(self.store.settings()['compact_layout']['priorities']['reading'],4)

    def test_shrink_only_keeps_draft_priorities_and_cancel(self):
        before=deepcopy(self.app.compact_layout)
        self.select_priority('title',6)
        self.dialog.compact_mode.set('shrink_only'); self.dialog.update()
        self.assertTrue(all(str(combo.cget('state'))=='disabled' for combo in self.dialog.combos.values()))
        self.dialog.compact_mode.set('auto_hide'); self.dialog.update()
        self.assertEqual(self.dialog.draft()['compact_layout']['priorities']['title'],6)
        self.dialog.close()
        self.assertEqual(self.app.compact_layout,before)

    def test_reset_restores_draft_defaults(self):
        self.select_priority('title',5)
        self.dialog.compact_mode.set('shrink_only'); self.dialog.update()
        self.dialog.reset()
        self.assertEqual(self.dialog.draft()['compact_layout'],DEFAULT_COMPACT_LAYOUT)
        self.assertTrue(all(str(combo.cget('state'))=='readonly' for combo in self.dialog.combos.values()))

if __name__ == '__main__':
    unittest.main()
