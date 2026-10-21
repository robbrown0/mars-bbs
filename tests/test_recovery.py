"""New recovery verification tests; synthetic data only."""
import ast
import importlib
from pathlib import Path
import tempfile
import unittest
from unittest.mock import patch
from marsv2 import rpc, authd, boardd, maild, games, migrate, sodium, informationd


def req(op, **data):
    return {"op": op, "data": data}


class RecoveryTests(unittest.TestCase):
    def test_required_password_change_uses_authenticated_uid(self):
        with tempfile.TemporaryDirectory() as directory:
            authority = authd.Authority(directory + "/auth.db")
            authority.add_account("tester", "Temporary test password!", "Test Member",
                                  password_change_required=True)
            uid = authority.db.execute(
                "SELECT runtime_uid FROM accounts WHERE userid='tester'").fetchone()[0]
            request = req("change_password", new_password="Replacement test password!")
            with self.assertRaises(rpc.RPCError):
                authority.handle(request, (123, 29999, 29999))
            self.assertTrue(authority.handle(request, (123, uid, uid))["changed"])
            row = authority.db.execute(
                "SELECT password_hash,password_change_required FROM accounts "
                "WHERE userid='tester'").fetchone()
            self.assertFalse(row["password_change_required"])
            self.assertTrue(sodium.verify_password(row["password_hash"],
                                                  "Replacement test password!"))
            with self.assertRaises(rpc.RPCError):
                authority.handle(request, (123, uid, uid))
            authority.db.close()

    def test_required_password_change_prompts_only_for_new_password(self):
        from marsv2 import ui
        identity = {"userid": "tester", "password_change_required": True}
        with patch.object(ui, "heading"), patch.object(ui, "call") as call, \
                patch.object(ui.getpass, "getpass", side_effect=[
                    "Replacement test password!", "Replacement test password!"]) as prompt:
            self.assertTrue(ui.required_password_change(identity))
        self.assertEqual(prompt.call_count, 2)
        self.assertNotIn("old_password", call.call_args.args[2])
        self.assertFalse(identity["password_change_required"])

    def test_syntax_and_imports(self):
        paths = sorted(Path("marsv2").glob("*.py"))
        self.assertGreaterEqual(len(paths), 37)
        for path in paths:
            with self.subTest(module=path.stem):
                ast.parse(path.read_text())
                importlib.import_module("marsv2." + path.stem)

    def test_password_hashing(self):
        secret = "Synthetic test password 487!"
        encoded = sodium.hash_password(secret)
        self.assertTrue(encoded.startswith("$argon2id$"))
        self.assertTrue(sodium.verify_password(encoded, secret))
        self.assertFalse(sodium.verify_password(encoded, "Wrong test password!"))

    def test_authentication_ticket_replay_and_admin_boundary(self):
        with tempfile.TemporaryDirectory() as directory:
            authority = authd.Authority(directory + "/auth.db")
            authority.add_account("tester", "Synthetic test password!", "Test Member")
            peer = (123, 29999, 29999)
            ticket = authority.handle(req("login", userid="tester", password="Synthetic test password!"), peer)["ticket"]
            identity = authority.handle(req("consume_ticket", ticket=ticket), (124, 21002, 21002))
            self.assertEqual(identity["userid"], "tester")
            self.assertNotIn("account.manage", identity["capabilities"])
            with self.assertRaises(rpc.RPCError):
                authority.handle(req("consume_ticket", ticket=ticket), peer)
            with self.assertRaises(rpc.RPCError):
                authority.handle(req("authorization", account_id=identity["account_id"]), peer)
            with self.assertRaises(rpc.RPCError):
                authority.handle(req("login", userid="guest", password="Wrong test password!"), peer)
            with self.assertRaises(rpc.RPCError):
                authority.handle(req("login", userid="tester", password="Wrong test password!"), peer)
            authority.db.close()

    def test_rpc_rejects_ambiguous_and_oversized_packets(self):
        for raw in (b'{"v":1,"op":"health","op":"login"}', b'[]', b'{"v":2,"op":"health"}', b'x' * 65537):
            with self.subTest(raw_size=len(raw)), self.assertRaises(rpc.RPCError):
                rpc.decode_packet(raw)
        self.assertEqual(rpc.decode_packet(b'{"v":1,"op":"health"}')["op"], "health")

    def test_board_authentication_and_write_read(self):
        identity = {"account_id": 1, "userid": "tester", "roles": ["Basic-1"], "capabilities": []}
        with tempfile.TemporaryDirectory() as directory:
            boards = boardd.Boards(directory + "/board.db", "/missing/session.sock")
            with self.assertRaises(rpc.RPCError):
                boards.handle(req("boards"), (123, 30001, 30001))
            with patch.object(boardd, "validate", return_value=identity):
                board_id = boards.db.execute("SELECT id FROM boards LIMIT 1").fetchone()[0]
                posted = boards.handle(req("post", board_id=board_id, subject="Recovery fixture", body="Synthetic public text"), (123, 30001, 30001))
                read = boards.handle(req("read", id=posted["id"]), (123, 30001, 30001))
                self.assertEqual(read["post"]["body"], "Synthetic public text")
                with self.assertRaises(rpc.RPCError):
                    boards.handle(req("admin_create", name="Forbidden", description="Test"), (123, 30001, 30001))
            boards.db.close()

    def test_private_mail_is_scoped_to_recipient(self):
        identity = {"account_id": 1, "userid": "tester", "capabilities": []}
        target = {"ok": True, "data": {"account_id": 2, "userid": "receiver"}}
        with tempfile.TemporaryDirectory() as directory:
            mail = maild.Mail(directory + "/mail.db", "/missing/session.sock", "/missing/auth.sock")
            with self.assertRaises(rpc.RPCError):
                mail.handle(req("list"), (123, 30001, 30001))
            with patch.object(maild, "validate", return_value=identity), patch.object(maild, "request", return_value=target):
                sent = mail.handle(req("send", recipient="receiver", subject="Synthetic", body="Private fixture"), (123, 30001, 30001))
                with self.assertRaises(rpc.RPCError):
                    mail.handle(req("read", id=sent["id"]), (123, 30001, 30001))
            with patch.object(maild, "validate", return_value={"account_id": 2, "userid": "receiver", "capabilities": []}):
                self.assertEqual(mail.handle(req("read", id=sent["id"]), (124, 30002, 30002))["message"]["body"], "Private fixture")
            mail.db.close()

    def test_game_initial_states_and_chess_engine(self):
        for name in games.GAMES:
            state = games.new_game(name, 1, "tester")
            self.assertIsInstance(games.render(name, state), str)
            self.assertEqual(state["game"], name)
        state = games.new_game("chess", 1, "tester")
        result = games.apply("chess", state, "e4")
        self.assertIsInstance(result, dict)
        self.assertEqual(games.GAMES["gtw"]["title"], "GLOBAL THERMONUCLEAR WAR")

    def test_migration_rejects_symlinks(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            (root / "unsafe").symlink_to("/etc/passwd")
            with self.assertRaises(ValueError):
                list(migrate.regular_files(root))

    def test_legacy_passwords_are_disabled(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            (root / "etc").mkdir()
            (root / "etc/passwds").write_text("tester:legacy-password:1:1:Test Member\n")
            accounts = migrate.parse_accounts(root)
            self.assertFalse(accounts[0]["enabled"])
            self.assertEqual(accounts[0]["hash"], "!MIGRATED-DISABLED!")

    def test_collector_text_cleans_terminal_controls(self):
        text = informationd.clean_text("hello\x1b[31mworld\x00")
        self.assertNotIn("\x1b", text)
        self.assertNotIn("\x00", text)


if __name__ == "__main__":
    unittest.main(verbosity=2)
