"""Cross-section sign-in banners and context-specific member selection."""
import contextlib
import io
import os
import socket
import sys
import unittest
from unittest.mock import patch

from marsv2 import ui, PROTOCOL_VERSION
from marsv2.rpc import encode_packet


class MonitorTests(unittest.TestCase):
    def setUp(self):
        self.previous = ui._active_pager
        self.one, self.two = socket.socketpair(socket.AF_UNIX, socket.SOCK_SEQPACKET)
        self.pager = ui.Pager.__new__(ui.Pager)
        self.pager.sock = self.one
        self.pager.userid = 'rob'
        self.pager.pending = {}
        self.pager.available = True
        self.pager.activity = lambda *a: None
        ui._active_pager = self.pager

    def tearDown(self):
        ui._active_pager = self.previous
        self.one.close()
        self.two.close()

    def send_monitor(self):
        self.two.sendall(encode_packet({'v': PROTOCOL_VERSION, 'op': 'event', 'data': {
            'kind': 'monitor', 'userid': 'alice', 'online': True}}))

    def test_monitor_picker_has_own_heading_and_local_help(self):
        with contextlib.redirect_stdout(io.StringIO()) as output:
            ui._draw_recipient_picker([{'userid': 'alice', 'display_name': 'Alice'}],
                                      ui.EditorBuffer(''), 0, '', 'monitor')
        self.assertIn('MONITOR - CHOOSE MEMBER', output.getvalue())
        self.assertNotIn('POST OFFICE', output.getvalue())
        self.assertNotIn('linked-bbs', output.getvalue())

    def test_monitor_directory_uses_members_without_mail_and_omits_self(self):
        with patch.object(ui, 'call', return_value={'users': [
                {'userid': 'rob', 'display_name': 'Rob'},
                {'userid': 'alice', 'display_name': 'Alice'}]}) as rpc:
            users = ui._monitor_directory()
        self.assertEqual([user['userid'] for user in users], ['alice'])
        rpc.assert_called_once_with('auth', 'member_directory', {'offset': 0})

    def test_chat_or_irc_wait_services_global_monitor_socket(self):
        other, peer = socket.socketpair()
        try:
            self.send_monitor()
            with contextlib.redirect_stdout(io.StringIO()) as output:
                with patch.object(output, 'isatty', return_value=True), patch.dict(os.environ, TERM='xterm'):
                    ready, _, _ = ui._notice_select([other], [], [], 0.1)
            self.assertEqual(ready, [])
            self.assertIn('ONLINE: @alice has signed in.', output.getvalue())
            self.assertIn('\0337\033[1;1H', output.getvalue())
            self.assertIn('\0338', output.getvalue())
        finally:
            other.close()
            peer.close()

    def test_admin_source_headings_match_values(self):
        def rpc(service, op, *args, **kwargs):
            if op == 'stats':
                return {'tracks': 2}
            return {'sources': [{'name': 'nas01', 'kind': 'base', 'available': True}]}
        with patch.object(ui, 'call', side_effect=rpc), patch.object(ui, 'key_input', return_value='x'), patch.object(ui, 'heading'), contextlib.redirect_stdout(io.StringIO()) as output:
            ui._admin_marsamp('fixture')
        lines = output.getvalue().splitlines()
        header = next(line for line in lines if 'SOURCE' in line and 'TYPE' in line)
        row = next(line for line in lines if 'nas01' in line)
        for label, value in [('SOURCE', 'nas01'), ('TYPE', 'BASE'), ('STATUS', 'AVAILABLE')]:
            self.assertEqual(header.index(label), row.index(value))

    def test_line_input_survives_monitor_banner_in_real_pty(self):
        import pexpect
        script = r'''
import socket, threading
from marsv2 import ui, PROTOCOL_VERSION
from marsv2.rpc import encode_packet
one,two=socket.socketpair(socket.AF_UNIX,socket.SOCK_SEQPACKET)
pager=ui.Pager.__new__(ui.Pager)
pager.sock=one; pager.pending={}; pager.userid='rob'; pager.available=True
pager.activity=lambda *a:None
ui._active_pager=pager
def alert():
    two.sendall(encode_packet({'v':PROTOCOL_VERSION,'op':'event','data':{'kind':'monitor','userid':'alice','online':True}}))
threading.Timer(0.7,alert).start()
print('RESULT:'+ui.input('Subject: '),flush=True)
'''
        child = pexpect.spawn(sys.executable, ['-c', script], encoding='utf-8', timeout=5,
                              env=dict(os.environ, TERM='xterm'))
        try:
            child.expect_exact('Subject: ')
            child.send('hel')
            child.expect_exact('ONLINE: @alice has signed in.')
            child.send('lo\r')
            child.expect_exact('RESULT:hello')
            child.expect(pexpect.EOF)
        finally:
            child.close(force=True)

    def test_external_game_keeps_monitor_and_keyboard_channels_separate(self):
        import pexpect
        script = r'''
import json, os, socket, struct, tempfile, threading
from marsv2 import ui, door_client, PROTOCOL_VERSION
from marsv2.rpc import encode_packet
one,two=socket.socketpair(socket.AF_UNIX,socket.SOCK_SEQPACKET)
pager=ui.Pager.__new__(ui.Pager)
pager.sock=one; pager.pending={}; pager.userid='rob'; pager.available=True
pager.activity=lambda *a:None
ui._active_pager=pager
tmp=tempfile.TemporaryDirectory(); path=tmp.name+'/play.sock'
server=socket.socket(socket.AF_UNIX,socket.SOCK_STREAM); server.bind(path); server.listen(1)
door_client.socket_path=lambda service:tmp.name+'/door.sock'
def game():
    conn,_=server.accept()
    length=struct.unpack('!I',door_client._read_exact(conn,4))[0]
    door_client._read_exact(conn,length)
    door_client.send_frame(conn,b'A')
    door_client.send_frame(conn,b'O',b'Game> ')
    typed=b''
    while True:
        kind,payload=door_client.recv_frame(conn)
        if kind==b'I':
            typed+=payload
            if b'\r' in typed:
                door_client.send_frame(conn,b'O',b'GAME INPUT:'+typed.replace(b'\r',b'')+b'\r\n')
                door_client.send_frame(conn,b'E',struct.pack('!i',0)); return
threading.Thread(target=game,daemon=True).start()
def alert():
    two.sendall(encode_packet({'v':PROTOCOL_VERSION,'op':'event','data':{'kind':'monitor','userid':'alice','online':True}}))
threading.Timer(0.7,alert).start()
door_client.play('fixture','token',notification_socket=one,on_notification=pager.receive,paint_notification=pager.draw_notification)
'''
        child = pexpect.spawn(sys.executable, ['-c', script], encoding='utf-8', timeout=6,
                              env=dict(os.environ, TERM='xterm'))
        try:
            child.expect_exact('Game> ')
            child.send('hel')
            child.expect_exact('ONLINE: @alice has signed in.')
            child.send('lo\r')
            child.expect_exact('GAME INPUT:hello')
            child.expect(pexpect.EOF)
        finally:
            child.close(force=True)
