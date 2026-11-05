"""Frozen old urllib path with a deterministic slow SocketIO; no real HTTP."""
import importlib.util
import json
from pathlib import Path
import sys
from unittest.mock import patch
import urllib.request

ROOT=Path(__file__).resolve().parents[2]
sys.path.insert(0,str(ROOT));sys.dont_write_bytecode=True
from tools.project_tasks.test_transport_deadline import Clock,Peer

BASE=ROOT/'.q4t-work/serve-client-transport-20261104/baseline'


def main():
    spec=importlib.util.spec_from_file_location('baseline_client',BASE/'source/tools/examples/chat_client.py')
    client=importlib.util.module_from_spec(spec);sys.modules[spec.name]=client;spec.loader.exec_module(client)
    clock=Clock()
    peer=Peer(clock,b'HTTP/1.1 200 OK\r\nContent-Length: 8\r\nConnection: close\r\n\r\n{"ok":1}',step=.01)
    peer.setsockopt=lambda *_:None
    def connect(_address,timeout,*_args,**_kwargs):
        peer.settimeout(timeout);return peer
    api=client.ChatClient('http://127.0.0.1/v1')
    with patch('http.client.socket.create_connection',connect):
        with api._open(urllib.request.Request(api.base+'/models'),.2) as response:
            raw=response.read(9)
    value=dict(observation='old total deadline exceeded despite continuous progress',
        timeout=.2,virtual_seconds=clock.now,returned_success=raw==b'{"ok":1}',
        socket_closed=peer.closed,models=0,http=0,gpu=0)
    assert value['returned_success'] and clock.now>.2 and peer.closed
    with (BASE/'result.json').open('x') as out:json.dump(value,out,indent=2)
    print(json.dumps(value))


if __name__=='__main__':main()
