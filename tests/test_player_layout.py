"""Durable administrator player layout choice and safe terminal geometry."""
import contextlib
import io
import os
import unittest
from pathlib import Path
from unittest.mock import patch

import test_music
from marsv2 import ui
from marsv2.musicd import Music
from marsv2.rpc import RPCError


class PlayerSettingTests(unittest.TestCase):
    setUp = test_music.MusicTests.setUp
    tearDown = test_music.MusicTests.tearDown
    request = staticmethod(test_music.MusicTests.request)

    def test_listener_cannot_change_layout_but_can_read_it(self):
        with patch('marsv2.musicd.validate', return_value=test_music.IDENTITY):
            self.assertEqual(self.music.handle(self.request('status'), (1, 29901, 29901))['compact_player_lines'], 3)
            with self.assertRaises(RPCError):
                self.music.handle(self.request('admin_player_layout', {'lines': 4}), (1, 29901, 29901))
        self.assertEqual(self.music.player_layout(), 3)

    def test_admin_choice_survives_restart_and_rejects_invalid_values(self):
        manager = dict(test_music.IDENTITY, capabilities=['music.play', 'music.manage'])
        with patch('marsv2.musicd.validate', return_value=manager):
            for invalid in (True, 2, 5, '4', 3.5):
                with self.assertRaises((ValueError, RPCError)):
                    self.music.handle(self.request('admin_player_layout', {'lines': invalid}), (1, 29901, 29901))
            result = self.music.handle(self.request('admin_player_layout', {'lines': 4}), (1, 29901, 29901))
            self.assertEqual(result['compact_player_lines'], 4)
            self.assertEqual(self.music.handle(self.request('stats'), (1, 29901, 29901))['compact_player_lines'], 4)
        self.music.db.close()
        root = Path(self.temporary.name)
        self.music = Music(str(root / 'music.db'), '/unused/session.sock',
            {'nas01': str(self.nas01), 'unr01': str(self.unr01)},
            'https://bbs.example.test/marsamp', str(self.extra), str(root / 'playlists.json'))
        self.assertEqual(self.music.state(7)['compact_player_lines'], 4)


class PlayerLayoutUITests(unittest.TestCase):
    def test_both_layouts_keep_borders_aligned_and_controls_visible(self):
        for layout in (3, 4):
            for columns in (34, 40, 80, 120):
                for action in ('pause', 'play'):
                    with patch.object(ui, '_mini_state', {'action': action, 'compact_player_lines': layout}):
                        lines = ui._mini_lines(columns)
                    self.assertEqual(len(lines), layout)
                    self.assertEqual(len(set(map(len, lines))), 1)
                    self.assertEqual(lines[-1].index('└'), lines[0].index('┌'))
                    self.assertEqual(lines[-1].index('┘'), lines[0].index('┐'))
                    controls = lines[-1] if layout == 3 else lines[-2]
                    for key in ('[B]', '[P]', '[N]'):
                        self.assertIn(key, controls)

    def test_polling_defers_height_change_until_lobby_redraw(self):
        with patch.object(ui, '_mini_state', {'compact_player_lines': 4}), patch.object(ui, '_mini_layout', 3), patch.object(ui, '_mini_row', 3), patch.object(ui, '_mini_visible', True), patch.object(ui, '_mini_geometry', (80, 24)), patch.object(ui, '_mini_refresh', return_value=True), patch.object(ui.shutil, 'get_terminal_size', return_value=os.terminal_size((80, 24))), patch.dict(os.environ, TERM='xterm'), contextlib.redirect_stdout(io.StringIO()) as output:
            with patch.object(output, 'isatty', return_value=True):
                ui._mini_tick()
            self.assertIn('\033[5;1H', output.getvalue())
            self.assertNotIn('\033[6;1H', output.getvalue())

    def test_administration_toggle_updates_saved_choice(self):
        state = {'compact_player_lines': 3}
        def rpc(service, op, data=None, token=None):
            if op == 'stats':
                return dict(state)
            if op == 'admin_sources':
                return {'sources': []}
            self.assertEqual(op, 'admin_player_layout')
            self.assertEqual(data, {'lines': 4})
            state.update(compact_player_lines=data['lines'])
            return dict(state)
        with patch.object(ui, 'call', side_effect=rpc), patch.object(ui, 'key_input', side_effect=['l', 'x']), patch.object(ui, 'heading'), patch.object(ui, '_mini_poll_at', 123), contextlib.redirect_stdout(io.StringIO()) as output:
            ui._admin_marsamp('fixture')
            self.assertEqual(ui._mini_poll_at, 0)
        self.assertIn('Compact player: 3 lines', output.getvalue())
        self.assertIn('Compact player: 4 lines', output.getvalue())
