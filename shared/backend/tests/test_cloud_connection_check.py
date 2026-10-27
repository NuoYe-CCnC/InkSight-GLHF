"""Local-only WebDAV simulations. Never uses real credentials or cloud."""
import asyncio
import copy
import importlib.util
import json
import socket
from pathlib import Path
import threading
import urllib.error
from http.server import BaseHTTPRequestHandler,ThreadingHTTPServer

_LOCAL_CONNECT=socket.socket.connect

import pytest


@pytest.fixture
def pub(monkeypatch):
    path=Path(__file__).resolve().parents[2]/'tools/cloud_publish.py'
    spec=importlib.util.spec_from_file_location('connection_publish_test',path)
    module=importlib.util.module_from_spec(spec);spec.loader.exec_module(module)
    objects={},[]
    files,requests=objects
    def request(method,url,headers,body=None,**kwargs):
        name=url.rsplit('/',1)[1];requests.append((method,name,dict(headers)))
        if method=='GET':
            if name not in files:raise urllib.error.HTTPError(url,404,'missing',{},None)
            return 200,files[name]
        if method=='PUT':
            if headers.get('If-None-Match')=='*' and name in files:
                raise urllib.error.HTTPError(url,412,'exists',{},None)
            files[name]=body;return 201,b''
        if method=='MOVE':raise urllib.error.HTTPError(url,405,'unsupported',{},None)
        if method=='DELETE':del files[name];return 204,b''
        pytest.fail('unexpected '+method)
    monkeypatch.setattr(module,'_webdav_request',request)
    return module,files,requests


def test_create_only_and_move_fallback_are_honest_and_cleanup_owned_part(pub):
    module,files,requests=pub;receipt={}
    assert module.publish_one('https://example.invalid/test','user','synthetic','AABBCCDDEEFF',{'actual':'unknown'},
                              create_only=True,object_name='INKSIGHT-CHECK-unique.json',receipt=receipt)
    assert receipt['verified'] and receipt['move_supported'] is False and receipt['part_cleaned']
    assert list(files)==['INKSIGHT-CHECK-unique.json']
    puts=[row for row in requests if row[0]=='PUT']
    assert all(row[2]['If-None-Match']=='*' for row in puts)
    original=copy.deepcopy(files)
    assert not module.publish_one('https://example.invalid/test','user','synthetic','AABBCCDDEEFF',{'different':True},
                                  create_only=True,object_name='INKSIGHT-CHECK-unique.json')
    assert files==original


def test_probe_is_unique_and_does_not_touch_existing_fixed_probe(pub):
    module,files,requests=pub;files['.inksight_probe']=b'preexisting'
    a,b={},{}
    assert module.probe_webdav('https://example.invalid/test','u','p',receipt=a)
    assert module.probe_webdav('https://example.invalid/test','u','p',receipt=b)
    assert a['name']!=b['name'] and a['cleaned'] and b['cleaned']
    assert files=={'.inksight_probe':b'preexisting'}
    assert all(row[1]!='.inksight_probe' for row in requests)


def test_cleanup_failure_is_failure_not_false_pass(pub,monkeypatch):
    module,files,_=pub;original=module._webdav_request
    def denied(method,url,*args,**kwargs):
        if method=='DELETE':raise urllib.error.HTTPError(url,403,'denied',{},None)
        return original(method,url,*args,**kwargs)
    monkeypatch.setattr(module,'_webdav_request',denied)
    receipt={}
    assert not module.probe_webdav('https://example.invalid/test','u','p',receipt=receipt)
    assert receipt['created'] and receipt['verified'] and not receipt['cleaned']


def test_redirect_never_forwards_authentication(monkeypatch):
    path=Path(__file__).resolve().parents[2]/'tools/cloud_publish.py'
    spec=importlib.util.spec_from_file_location('redirect_publish_test',path)
    module=importlib.util.module_from_spec(spec);spec.loader.exec_module(module)
    leaked=[]
    class Sink(BaseHTTPRequestHandler):
        def do_GET(self):leaked.append(self.headers.get('Authorization'));self.send_response(200);self.end_headers()
        def log_message(self,*args):pass
    target=ThreadingHTTPServer(('127.0.0.1',0),Sink)
    class Redirect(BaseHTTPRequestHandler):
        def do_GET(self):
            self.send_response(302);self.send_header('Location',f'http://127.0.0.1:{target.server_port}/sink');self.end_headers()
        def log_message(self,*args):pass
    origin=ThreadingHTTPServer(('127.0.0.1',0),Redirect)
    allowed={('127.0.0.1',origin.server_port),('127.0.0.1',target.server_port)}
    def local_only(sock,address):
        if address not in allowed:pytest.fail('only the two local test ports are permitted')
        return _LOCAL_CONNECT(sock,address)
    monkeypatch.setattr(socket.socket,'connect',local_only)
    threads=[threading.Thread(target=s.serve_forever,daemon=True) for s in (origin,target)]
    for thread in threads:thread.start()
    try:
        with pytest.raises(urllib.error.HTTPError) as error:
            module._webdav_request('GET',f'http://127.0.0.1:{origin.server_port}/redirect',{'Authorization':'Basic synthetic'})
        assert error.value.code==302 and leaked==[]
    finally:
        for server in (origin,target):server.shutdown();server.server_close()


def test_failed_transfer_does_not_advance_successful_snapshot(pub,monkeypatch,tmp_path):
    from core import cloud_connection_check as check
    module,_,_=pub;before={'_desktop_publications':{'AABBCCDDEEFF':{'publication_kind':'connection_check','old':True}}}
    monkeypatch.setattr(module,'load_state',lambda:copy.deepcopy(before))
    monkeypatch.setattr(module,'probe_webdav',lambda *a,**k:False)
    monkeypatch.setattr(module,'save_state',lambda _:pytest.fail('failure advanced snapshot'))
    monkeypatch.setattr(check,'_save',lambda _:None)
    with pytest.raises(RuntimeError,match='探针'):
        check._transfer(module,{'base_url':'https://example.invalid','user':'u','password':'p'},'AABBCCDDEEFF',{},
                        {'objects':[],'object_name':'unique.json'})


def test_regular_snapshot_is_protected_before_any_cloud_io(pub,monkeypatch):
    from core import cloud_connection_check as check
    module,_,requests=pub
    monkeypatch.setattr(module,'load_state',lambda:{'_desktop_publications':{'AABBCCDDEEFF':{'publication_kind':'regular'}}})
    with pytest.raises(RuntimeError,match='不会覆盖'):
        check._transfer(module,{},'AABBCCDDEEFF',{}, {'objects':[]})
    assert requests==[]


def test_real_publish_readback_mismatch_keeps_previous_snapshot(pub,monkeypatch):
    from core import cloud_connection_check as check
    module,files,_=pub;request=module._webdav_request
    old={'_desktop_publications':{'AABBCCDDEEFF':{'publication_kind':'connection_check','old':True}}}
    monkeypatch.setattr(module,'load_state',lambda:copy.deepcopy(old))
    monkeypatch.setattr(module,'save_state',lambda _:pytest.fail('bad GET advanced snapshot'))
    monkeypatch.setattr(check,'_save',lambda _:None)
    def corrupt(method,url,*args,**kwargs):
        name=url.rsplit('/',1)[1]
        if method=='GET' and name=='mismatch.json' and name in files:return 200,b'corrupt-readback'
        return request(method,url,*args,**kwargs)
    monkeypatch.setattr(module,'_webdav_request',corrupt)
    report={'objects':[],'object_name':'mismatch.json'}
    with pytest.raises(RuntimeError,match='完整快照'):
        check._transfer(module,{'base_url':'https://example.invalid','user':'u','password':'p'},
                        'AABBCCDDEEFF',{'screen':{'publish_id':'new'}},report)
    assert report['objects'][1]['verified'] is False
    assert module.load_state()==old
