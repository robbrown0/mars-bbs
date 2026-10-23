"""Compose rendering stays quiet when idle and updates only changed rows."""
import contextlib
import io
import os
import sys
import unittest
from unittest.mock import patch
from marsv2 import ui


class ComposeRenderTests(unittest.TestCase):
    def test_edit_and_cursor_movement_do_not_clear_or_redraw_header(self):
        frame = []
        buffer = ui.EditorBuffer('hello')
        def draw():
            return ui._draw_mail_editor('rob', 'hi', buffer, 0, '5 characters',
                'edit', 'POST OFFICE - COMPOSE', 'To', 'Send', True, previous_frame=frame)
        with patch.object(ui.shutil, 'get_terminal_size', return_value=os.terminal_size((80, 24))):
            with contextlib.redirect_stdout(io.StringIO()) as output:
                draw()
            self.assertEqual(output.getvalue().count('\033[2J'), 1)
            buffer.insert('!')
            with contextlib.redirect_stdout(io.StringIO()) as output:
                draw()
            self.assertNotIn('\033[2J', output.getvalue())
            self.assertNotIn('POST OFFICE - COMPOSE', output.getvalue())
            self.assertIn('hello!', output.getvalue())
            buffer.left()
            with contextlib.redirect_stdout(io.StringIO()) as output:
                draw()
            self.assertNotIn('\033[K', output.getvalue())
            self.assertNotIn('\033[2J', output.getvalue())

    def test_real_pty_is_quiet_while_idle_and_retains_text_after_resize(self):
        import pexpect
        script = "from marsv2 import ui; print('RESULT:'+repr(ui.mail_editor('rob','hi')),flush=True)"
        child = pexpect.spawn(sys.executable, ['-c', script], encoding='utf-8', timeout=4,
                              dimensions=(24, 80), env=dict(os.environ, TERM='xterm'))
        try:
            child.expect_exact('0 / 8192 characters')
            child.expect_exact('\033[?25h')
            child.expect(pexpect.TIMEOUT, timeout=0.8)
            self.assertEqual(child.before, '')
            child.send('hello')
            child.expect_exact('5 / 8192 characters')
            self.assertNotIn('\033[2J', child.before)
            self.assertNotIn('POST OFFICE - COMPOSE', child.before)
            child.setwinsize(30, 90)
            child.expect_exact('Terminal resized; message reflowed.')
            child.send('\x13')
            child.expect_exact('Send this message now? [Y/N]')
            child.send('y')
            child.expect_exact("RESULT:('send', 'hello')")
            child.expect(pexpect.EOF)
        finally:
            child.close(force=True)

    def test_compose_monitor_banner_preserves_message(self):
        import pexpect
        script = r'''
import socket, threading
from marsv2 import ui, PROTOCOL_VERSION
from marsv2.rpc import encode_packet
one,two=socket.socketpair(socket.AF_UNIX,socket.SOCK_SEQPACKET)
pager=ui.Pager.__new__(ui.Pager)
pager.sock=one; pager.userid='rob'; pager.pending={}; pager.available=True
pager.activity=lambda *a:None
ui._active_pager=pager
def alert():
    two.sendall(encode_packet({'v':PROTOCOL_VERSION,'op':'event','data':{'kind':'monitor','userid':'alice','online':True}}))
threading.Timer(0.7,alert).start()
print('RESULT:'+repr(ui.mail_editor('rob','hi')),flush=True)
'''
        child = pexpect.spawn(sys.executable, ['-c', script], encoding='utf-8', timeout=5,
                              dimensions=(24, 80), env=dict(os.environ, TERM='xterm'))
        try:
            child.expect_exact('0 / 8192 characters')
            child.send('hello')
            child.expect_exact('ONLINE: @alice has signed in.')
            self.assertNotIn('\033[2J', child.before)
            child.send('\x13')
            child.expect_exact('Send this message now? [Y/N]')
            child.send('y')
            child.expect_exact("RESULT:('send', 'hello')")
            child.expect(pexpect.EOF)
        finally:
            child.close(force=True)
