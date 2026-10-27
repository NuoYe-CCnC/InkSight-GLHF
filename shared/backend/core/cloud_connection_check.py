"""Explicit one-shot connection check. No scheduled or model/upstream work.

Random create-only objects, a persisted ownership receipt, and real SHA GET
verification. A connection-check snapshot is labelled and never replaces an
existing regular publisher snapshot. No caller-supplied URL, file or MAC.
"""
import asyncio
import base64
import copy
import hashlib
import hmac
import importlib.util
import json
from pathlib import Path
import threading
import time
import uuid

from . import firmware_tasks,local_console_preview as preview,operator_config,state_store
from .desktop_service import gate

_LOCK=threading.Lock()
_PUBLISHER=None


def publisher():
    global _PUBLISHER
    if _PUBLISHER is None:
        path=Path(__file__).resolve().parents[2]/'tools/cloud_publish.py'
        spec=importlib.util.spec_from_file_location('inksight_connection_publisher',path)
        _PUBLISHER=importlib.util.module_from_spec(spec);spec.loader.exec_module(_PUBLISHER)
    return _PUBLISHER


def ticket():
    _,_,stamp=firmware_tasks._input_snapshot()
    return hmac.new(firmware_tasks._CONFIRM_SECRET,('cloud-check:'+stamp).encode(),hashlib.sha256).hexdigest()


def latest():
    rows,error=state_store.read_json(state_store.state_path('cloud_connection_check.json'))
    return rows if not error else {}


def _save(report):
    state_store.write_json(state_store.state_path('cloud_connection_check.json'),report,meta=False)


async def run(user_id,device_id,confirmation):
    if not isinstance(confirmation,str) or not hmac.compare_digest(confirmation,ticket()):
        raise RuntimeError('已应用配置已变化，请重新检查后确认')
    mac=preview._mac(await preview._resolve_device(user_id,device_id))
    effective=operator_config.load_effective(use_last_known_good=False)
    cloud=effective._secrets.get('cloud') or {}
    from urllib.parse import urlsplit
    target=urlsplit(str(cloud.get('base_url') or ''))
    if target.scheme!='https' or not target.hostname or target.username or target.password or target.query or target.fragment or not cloud.get('user') or not cloud.get('password'):
        raise RuntimeError('请先应用完整 HTTPS WebDAV 配置')
    if not _LOCK.acquire(False):raise RuntimeError('连接验收正在进行，请等待结束')
    report={'id':'connection-'+uuid.uuid4().hex,'status':'running','started_at':int(time.time()),
            'content_policy':'cache_only','device_display_confirmed':False,'objects':[]}
    try:
        # This gate runs in the event-loop thread only until composition ends;
        # actual I/O uses its own critical gate below to avoid async thread-local nesting.
        from .json_content import _generate_ai_usage_content
        from .mode_registry import get_registry
        from .structured_payload import build_screen_payload
        definition=get_registry().get_json_mode('AI_USAGE','').definition
        content=await _generate_ai_usage_content(copy.deepcopy(definition['content']['fallback']),cache_only=True,mac=mac)
        payload=build_screen_payload('AI_USAGE',content,800,480,mode_def=definition)
        if not payload:raise RuntimeError('正式文档生成失败，未上传')
        pub=publisher();document=pub.combine_dashboard([payload]);document['ts']=int(time.time())
        if set(document['screen']['pages'])!={'ai','news_gold'}:raise RuntimeError('完整双页面数据缺失，未上传')
        report['object_name']='INKSIGHT-CHECK-'+uuid.uuid4().hex+'.json'
        _save(report)
        return await asyncio.to_thread(_transfer,pub,cloud,mac,document,report)
    except Exception:
        report['status']='failed';report['error']='连接验收未完成，旧成功快照保留；请检查网络和当前配置'
        _save(report)
        raise
    finally:_LOCK.release()


def _transfer(pub,cloud,mac,document,report):
    with gate.work(critical=True):
        existing=pub.load_state().get('_desktop_publications',{}).get(mac,{})
        if existing and existing.get('publication_kind')!='connection_check':
            raise RuntimeError('已有正式成功发布快照，连接验收不会覆盖它')
        probe={};report['objects'].append(probe)
        if not pub.probe_webdav(cloud['base_url'],cloud['user'],cloud['password'],receipt=probe):
            raise RuntimeError('探针上传、回读或清理未完成')
        upload={};report['objects'].append(upload);_save(report)
        if not pub.publish_one(cloud['base_url'],cloud['user'],cloud['password'],mac,document,
                create_only=True,object_name=report['object_name'],receipt=upload):
            _save(report);raise RuntimeError('完整快照上传或回读校验失败')
        # Serialize with the regular publisher; never roll back its state.
        import fcntl,os
        fd=os.open(pub.STATE_LOCK_FILE,os.O_CREAT|os.O_RDWR,0o600)
        try:
            fcntl.flock(fd,fcntl.LOCK_EX)
            state=pub.load_state();prior=state.get('_desktop_publications',{}).get(mac,{})
            if prior and prior.get('publication_kind')!='connection_check':raise RuntimeError('正式发布已更新，不替换该快照')
            clean=pub._strip_sensitive(document)
            state.setdefault('_desktop_publications',{})[mac]={
                'last_success_at':document['ts'],'publish_id':pub.publication_id(document),
                'document':clean,'document_sha256':hashlib.sha256(json.dumps(clean,ensure_ascii=False).encode()).hexdigest(),
                'publication_kind':'connection_check','object_name':report['object_name']}
            pub.save_state(state)
        finally:fcntl.flock(fd,fcntl.LOCK_UN);os.close(fd)
        report.update(status='completed',finished_at=int(time.time()),retained=[report['object_name']],
                      preview_ready=True,probe_cleaned=probe['cleaned'],part_cleaned=upload['part_cleaned'])
        _save(report);return report
