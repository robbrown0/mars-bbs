"""Exercise only an explicitly selected isolated SSH gateway."""
import os, pathlib, re, subprocess
import pexpect
host = os.environ['RECOVERY_SSH_HOST']
port = os.environ.get('RECOVERY_SSH_PORT', '2222')
args = ['-tt', '-p', port, '-o', 'StrictHostKeyChecking=accept-new', '-o', 'UserKnownHostsFile=/tmp/known_hosts', '-o', 'LogLevel=ERROR', '-o', 'ConnectTimeout=10', 'bbs@'+host]
password = pathlib.Path('/run/secrets/admin_password').read_text().strip()
def connect():
    return pexpect.spawn('ssh', args, encoding='utf-8', timeout=20, dimensions=(30,100))
bad = connect()
bad.expect('userid')
assert '[Mars Hotel Main Menu]' not in bad.before
bad.sendline('guest')
bad.expect('Password:')
bad.sendline('Synthetic wrong password!')
bad.expect('Invalid userid or password')
bad.close(force=True)
child = connect()
child.expect('userid')
child.sendline('admin')
child.expect('Password:')
child.sendline(password)
child.expect(re.escape('[Mars Hotel Main Menu] -> '))
for key, prompt in [('g', 'Arcade> '), ('t', 'Talk> ')]:
    child.send(key)
    child.expect(re.escape(prompt))
    child.send('x')
    child.expect(re.escape('[Mars Hotel Main Menu] -> '))
child.send('x')
child.expect('Goodbye from Mars Hotel Revival')
child.expect(pexpect.EOF)
command_args = [a for a in args if a != '-tt'] + ['id']
remote = subprocess.run(['ssh']+command_args, capture_output=True, text=True, timeout=15)
assert remote.returncode != 0 and 'uid=' not in remote.stdout
assert 'Only a Mars file-download ticket' in remote.stderr
print('PASS SSH transport, guest rejection, account login, menus, logout and remote-command restriction')
