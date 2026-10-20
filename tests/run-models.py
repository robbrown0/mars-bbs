"""Run recovered model suites against the rebuilt package."""
import pathlib, subprocess, sys
failed = []
for name in ["admin_authorization","session_lease","session_peer_addresses","games_model","chatd_state","mail_schema_migration","information_network","timezone_model","mail_editor_model","mail_recipient_model","federation_exchange","games_fuzz","door_games","external_irc_presence"]:
    result = subprocess.run([sys.executable, str(pathlib.Path(__file__).parent / 'recovered' / (name + '.py'))], timeout=60)
    if result.returncode: failed.append(name)
print('Recovered model failures:', failed)
raise SystemExit(bool(failed))
