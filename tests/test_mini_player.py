"""Compact-player geometry, direct menu keys and durable playback."""
import contextlib
import io
import os
import sys
import unittest
from unittest.mock import patch

from marsv2 import ui
import test_music


class MiniPlayerTests(unittest.TestCase):
    def setUp(self):
        self.saved = {name: getattr(ui, name) for name in vars(ui) if name.startswith('_mini_')
                      and not callable(getattr(ui, name))}
        ui._mini_state = {'track_id': 1, 'action': 'play', 'track': {
            'artist': 'Grateful Dead', 'title': 'Scarlet Begonias > Fire on the Mountain'}}
        ui._mini_started = 0
        ui._mini_visible = True
        ui._mini_token = 'fixture'

    def tearDown(self):
        for name, value in self.saved.items():
            setattr(ui, name, value)

    def test_borders_and_notes_stay_fixed_while_artist_title_scroll(self):
        for columns in (34, 40, 80, 120):
            frames = [ui._mini_lines(columns, now) for now in (0, 8, 50)]
            for lines in frames:
                self.assertEqual(len(set(map(len, lines))), 1)
                self.assertLessEqual(len(lines[0]), columns - 1)
                for line in lines[1:2]:
                    self.assertEqual(line.index('│'), lines[0].index('┌'))
                    self.assertEqual(line.rindex('│'), lines[0].index('┐'))
                self.assertIn('♪', lines[1])
                self.assertEqual(len(lines), 3)
                self.assertIn('[B]', lines[2])
                self.assertIn('[P]', lines[2])
                self.assertIn('[N]', lines[2])
                self.assertEqual(lines[2].index('└'), lines[0].index('┌'))
                self.assertEqual(lines[2].index('┘'), lines[0].index('┐'))
            self.assertNotEqual(frames[0][1], frames[1][1])
            self.assertEqual(frames[0][0], frames[1][0])
            self.assertEqual(frames[0][2:], frames[1][2:])

    def test_main_menu_direct_keys_and_reassigned_entries(self):
        import json
        identity = {'display_name': 'Listener', 'userid': 'listener',
                    'capabilities': ['music.play']}
        read_fd, write_fd = os.pipe()
        os.write(write_fd, json.dumps({'token': 'fixture', 'identity': identity}).encode())
        os.close(write_fd)
        keys = iter(('b', 'p', 'p', 'n', 'o', 'e', 's', 'x'))
        transports = []
        state = dict(ui._mini_state)
        def rpc(service, op, *args, **kwargs):
            if service == 'session' and op == 'validate':
                return identity
            if service == 'music':
                if op != 'status':
                    transports.append(op)
                    state['action'] = 'pause' if op == 'pause' else 'play'
                return dict(state)
            if op == 'site_identity':
                return {'bbs_name': 'Mars Hotel'}
            return {}
        with patch.dict(os.environ, {'MARS_V2_SESSION_FD': str(read_fd)}), patch.object(ui, 'Pager') as pager, patch.object(ui, 'call', side_effect=rpc), patch.object(ui, 'dashboard', return_value=(0, 0, 1)), patch.object(ui, 'boards') as boards, patch.object(ui, 'scan_new_posts') as new, patch.object(ui, 'utilities') as profile, patch.object(ui.shutil, 'get_terminal_size', return_value=os.terminal_size((80, 24))), contextlib.redirect_stdout(io.StringIO()) as output:
            pager.return_value.read_key.side_effect = lambda *a: next(keys)
            ui._mini_poll_at = 0
            ui.main()
            boards.assert_called_once_with('fixture')
            new.assert_called_once_with('fixture')
            profile.assert_called_once_with(identity)
        self.assertEqual(transports, ['previous', 'pause', 'resume', 'advance'])
        text = output.getvalue()
        for label in ('[O] Boards', '[E] New board messages', '[S] Profile'):
            self.assertIn(label, text)
        for frame in text.split('\033[H')[1:]:
            self.assertLessEqual(len(frame.splitlines()), 24)

    def test_small_and_dense_screens_skip_header(self):
        for columns, rows, title in ((33, 24, 'FILE LIBRARY'),
                                     (80, 23, 'MESSAGE BOARDS'),
                                     (40, 24, 'LOBBY')):
            ui._mini_visible = False
            with patch.object(ui.shutil, 'get_terminal_size', return_value=os.terminal_size((columns, rows))), patch.object(ui, 'call') as rpc:
                with contextlib.redirect_stdout(io.StringIO()) as output:
                    ui._mini_heading(title, lobby=True)
                self.assertFalse(ui._mini_visible)
                self.assertEqual(output.getvalue(), '')
                rpc.assert_not_called()

    def test_clear_removes_header_and_restores_full_scroll_region(self):
        with contextlib.redirect_stdout(io.StringIO()) as output:
            ui.clear()
        self.assertFalse(ui._mini_visible)
        self.assertIn('\033[r', output.getvalue())


    def test_default_playlist_picker_can_toggle_to_all(self):
        saved_view = dict(ui._marsamp_view)
        ui._marsamp_view.clear()
        ui._marsamp_view.update(query='', playlist='', cursor=0, mode='sequential')
        operations = []
        def rpc(service, op, *args, **kwargs):
            operations.append(op)
            if op == 'playlists':
                return {'playlists': [{'slug': 'rock', 'name': 'Rock', 'tracks': 3}]}
            if op == 'search':
                return {'tracks': []}
            if op == 'stats':
                return {'tracks': 3}
            return {}
        try:
            with patch.object(ui, 'call', side_effect=rpc), patch.object(ui, 'key_input', side_effect=['t', 'x']), contextlib.redirect_stdout(io.StringIO()) as output:
                ui.marsamp('fixture')
            self.assertEqual(operations[0], 'playlists')
            self.assertIn('search', operations)
            self.assertIn('all music', output.getvalue())
            self.assertEqual(ui._marsamp_view['browse'], 'all')
        finally:
            ui._marsamp_view.clear()
            ui._marsamp_view.update(saved_view)

    def test_direct_controls_through_real_raw_terminal(self):
        import pexpect
        script = r'''
import json, os, socket
from marsv2 import ui
readfd, writefd = os.pipe()
os.write(writefd,json.dumps({'token':'fixture','identity':{'userid':'listener','display_name':'Listener','capabilities':['music.play']}}).encode())
os.close(writefd)
os.environ['MARS_V2_SESSION_FD']=str(readfd)
one,two=socket.socketpair(socket.AF_UNIX,socket.SOCK_SEQPACKET)
pager=ui.Pager.__new__(ui.Pager)
pager.sock=one
pager.activity=lambda *a:None
pager.close=lambda:None
ui.Pager=lambda token:pager
ui.dashboard=lambda token:(0,0,1)
ui.boards=lambda token:print('BOARD OPENED',flush=True)
state={'track_id':1,'action':'play','track':{'artist':'Grateful Dead','title':'Scarlet Begonias and a long scrolling song title'}}
def rpc(service,op,*args,**kwargs):
    if op=='validate':return {'capabilities':['music.play']}
    if op=='site_identity':return {'bbs_name':'Mars Hotel'}
    if service=='music':
        if op!='status':
            print('TRANSPORT '+op,flush=True)
            state['action']='pause' if op=='pause' else 'play'
        return dict(state)
    return {}
ui.call=rpc
ui.main()
'''
        child = pexpect.spawn(sys.executable, ['-c', script], encoding='utf-8',
                              timeout=6, dimensions=(24, 80), env=dict(os.environ, TERM='xterm-256color'))
        try:
            child.expect_exact('[Main Lobby] -> ')
            for key, operation in [('b', 'previous'), ('p', 'pause'), ('p', 'resume'), ('n', 'advance')]:
                child.send(key)
                child.expect_exact('TRANSPORT ' + operation)
                child.expect_exact('[Main Lobby] -> ')
            child.send('o')
            child.expect_exact('BOARD OPENED')
            child.expect_exact('[Main Lobby] -> ')
            child.send('x')
            child.expect(pexpect.EOF)
        finally:
            child.close(force=True)

    def test_lobby_and_identity_stay_above_marquee(self):
        with patch.object(ui.shutil, 'get_terminal_size', return_value=os.terminal_size((80, 24))), patch.object(ui, '_mini_refresh', return_value=True), contextlib.redirect_stdout(io.StringIO()) as output:
            with patch.object(output, 'isatty', return_value=True), patch.dict(os.environ, {'TERM': 'xterm'}):
                ui.main_heading({'display_name': 'brownstone', 'userid': 'rob'}, 1,
                                {'bbs_name': 'The Mars Hotel'})
                header = output.getvalue()
                self.assertLess(header.index('THE MARS HOTEL LOBBY'), header.index('MARSAMP'))
                self.assertLess(header.index('ACTIVE USERS: 1'), header.index('MARSAMP'))
                self.assertIn('brownstone (@rob)', header)
                self.assertNotIn('Last check-in', header)
                self.assertEqual(ui._mini_row, 3)
                self.assertIn("\033[6;24r\033[6;1H", header)
                output.seek(0)
                output.truncate()
                ui._mini_tick()
                tick = output.getvalue()
                self.assertIn('\033[3;1H', tick)
                self.assertNotIn('\033[1;1H', tick)
                self.assertNotIn('\033[2;1H', tick)

    def test_no_music_authority_means_no_header_or_status_request(self):
        ui._mini_token = ''
        ui._mini_visible = False
        with patch.object(ui, 'call') as rpc, contextlib.redirect_stdout(io.StringIO()) as output:
            ui.main_heading({'display_name': 'Rob', 'userid': 'rob'}, 1,
                            {'bbs_name': 'The Mars Hotel'})
        rpc.assert_not_called()
        self.assertFalse(ui._mini_visible)
        self.assertNotIn('MARSAMP', output.getvalue())
        self.assertIn('@rob', output.getvalue())
        self.assertNotIn('Rob (@rob)', output.getvalue())


class ResumeTests(unittest.TestCase):
    setUp = test_music.MusicTests.setUp
    tearDown = test_music.MusicTests.tearDown

    def test_resume_preserves_queue_shuffle_history(self):
        self.music.scan()
        ids = [track['id'] for track in self.music._search('', 0, 20)['tracks']]
        self.music.command(7, 'play', ids[0], ids, 'shuffle')
        self.music.advance(7)
        before = dict(self.music.playback[7])
        self.music.command(7, 'pause')
        state = self.music.command(7, 'resume')
        for key in ('queue', 'shuffle_bag', 'history', 'track_id', 'mode'):
            self.assertEqual(before[key], self.music.playback[7][key])
        self.assertEqual(state['action'], 'play')
        self.assertTrue(state['resume'])
        self.assertFalse(self.music.advance(7).get('resume'))

    def test_saved_selection_survives_restart_and_is_account_local(self):
        from copy import deepcopy
        from pathlib import Path
        root = Path(self.temporary.name)
        ids = [row['id'] for row in self.music._search('', 0, 20)['tracks']]
        self.music.command(7, 'play', ids[0], ids, 'shuffle')
        self.music.advance(7)
        saved = deepcopy(self.music.playback[7])
        self.music.db.close()
        self.music = test_music.Music(str(root / 'music.db'), '/unused/session.sock',
            {'nas01': str(self.nas01), 'unr01': str(self.unr01)},
            'https://bbs.example.test/marsamp', str(self.extra), str(root / 'playlists.json'))
        state = self.music.state(7)
        self.assertEqual(state['action'], 'pause')
        self.assertEqual(state['track_id'], saved['track_id'])
        self.assertEqual(self.music.state(8)['track_id'], 0)
        self.assertFalse(self.music.browser_sessions)
        self.music.command(7, 'resume')
        for key in ('track_id', 'queue', 'history', 'shuffle_bag', 'mode'):
            self.assertEqual(self.music.playback[7][key], saved[key])
