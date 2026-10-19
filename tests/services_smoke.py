"""Synthetic integration against an isolated stack via Unix sockets."""
import os, pathlib, socket, time
from marsv2.rpc import request, encode_packet, decode_packet, MAX_PACKET
assert os.environ.get('RECOVERY_TEST_ONLY') == 'isolated-fresh-volumes'
def path(service): return '/run/mars-v2/'+service+'/'+service+'.sock'
def raw(service, op, data=None, token=None): return request(path(service),op,data or {},token)
def call(service, op, data=None, token=None):
    response=raw(service,op,data,token)
    assert response['ok'], (service,op,response.get('data'))
    return response['data']
password=pathlib.Path('/run/secrets/admin_password').read_text().strip()
login=call('auth','login',{'userid':'admin','password':password})
identity=call('session','create',{'ticket':login['ticket']})
token=identity['token']; uid=identity['runtime_uid']
assert not raw('board','boards',token=token)['ok'], 'token accepted for wrong Unix UID'
os.setgroups([]);os.setgid(uid);os.setuid(uid)
assert not raw('board','boards')['ok'], 'unauthenticated board access'
boards=call('board','boards',token=token)['boards'];assert boards
post=call('board','post',{'board_id':boards[0]['id'],'subject':'Recovery integration fixture','body':'Synthetic isolated-stack text'},token)
assert call('board','read',{'id':post['id']},token)['post']['body']=='Synthetic isolated-stack text'
mail=call('mail','send',{'recipient':'admin','subject':'Recovery integration','body':'Synthetic private test message'},token)
assert call('mail','read',{'id':mail['id']},token)['message']['body']=='Synthetic private test message'
assert 'areas' in call('file','areas',token=token)
assert len(call('game','catalog',token=token)['games'])==5
for game in ['oregon','wopr','maze','gtw','chess']:
    started=call('game','start',{'game':game},token)
    assert started['screen']
    call('game','quit',{'lease':started['lease']},token)
for operation, data in [('news', {'category':'science'}), ('solar', {}), ('earthquakes', {}), ('entertainment', {'kind':'television'}), ('iss', {})]:
    result = call('information', operation, data, token)
    assert result, operation
query='Starkville'
for attempt in range(30):
    search=call('information','location_search',{'query':query},token)
    if search['results']:break
    time.sleep(0.5)
if search['results']:
    call('information','location_set',{'query':query,'index':0},token)
    weather = {}
    for attempt in range(30):
        response = raw('information','weather',token=token)
        if not response['ok']:
            assert response.get('data',{}).get('error') == 'data refresh pending; try again shortly', response
            time.sleep(0.5)
            continue
        weather=response['data']
        if weather.get('cache',{}).get('data'):break
        time.sleep(0.5)
    print('Live weather cache available:', bool(weather.get('cache',{}).get('data')))
else:
    print('Live weather location search unavailable; deterministic parser/cache suite covers offline behavior')
status=call('information','status',token=token)
print('Collector source states:',[(s['source'], bool(s.get('last_success')), bool(s.get('last_error'))) for s in status['sources']])
def connect_stream(service, operation, data):
    conn=socket.socket(socket.AF_UNIX,socket.SOCK_SEQPACKET);conn.settimeout(15);conn.connect(path(service));conn.sendall(encode_packet({'v':1,'op':operation,'data':data,'token':token}));return conn
def event(conn):return decode_packet(conn.recv(MAX_PACKET+1))
chat=connect_stream('chat','join',{'mode':'chat','room':'recoverytest'})
assert event(chat)['data']['kind']=='joined';assert event(chat)['data']['kind']=='motd'
chat.sendall(encode_packet({'v':1,'op':'message','data':{'message':'Synthetic isolated chat'},'token':token}))
assert 'Synthetic isolated chat' in event(chat)['data']['message'];chat.close()
irc=connect_stream('irc','connect',{'network':'local'})
connected=False;deadline=time.monotonic()+20
while time.monotonic()<deadline:
    item=event(irc)
    if item.get('data',{}).get('kind')=='connected':connected=True;break
    assert item.get('data',{}).get('kind')!='error',item
assert connected,'internal TLS IRC bridge did not connect'
irc.close()
call('session','end',token=token)
print('PASS interservice authentication and UID binding, boards, private mail, files, five games, collector status, chat and internal TLS IRC bridge')
