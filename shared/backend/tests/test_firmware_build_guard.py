"""Offline guards and real compiler-process cancellation; never uses USB."""
import concurrent.futures
import os
import sys
import threading

import pytest
from core import firmware_tasks as fw


@pytest.fixture
def build_lab(tmp_path,monkeypatch):
    inputs={'input':'one','source':'first'}
    monkeypatch.setattr(fw,'_STATE',tmp_path/'jobs.json')
    monkeypatch.setattr(fw,'_PRIVATE',tmp_path/'private')
    monkeypatch.setattr(fw,'preflight',lambda:{'ready':True})
    monkeypatch.setattr(fw,'_input_snapshot',lambda:({}, {},inputs['input']))
    monkeypatch.setattr(fw,'_source_hash',lambda:inputs['source'])
    monkeypatch.setattr(fw,'_BUILD_CANCEL',{})
    return inputs


def ticket(inputs):
    return fw._build_confirmation(inputs['input'],inputs['source'],fw._read())


def test_changed_confirmation_and_completed_old_dialog_rejected(build_lab):
    old=ticket(build_lab);build_lab['input']='changed'
    with pytest.raises(RuntimeError,match='已变化'):fw.create_build(old)
    assert not fw._read()['builds']
    current=ticket(build_lab);job=fw.create_build(current)
    fw._set('builds',job['id'],status='completed')
    with pytest.raises(RuntimeError,match='已变化'):fw.create_build(current)
    assert len(fw._read()['builds'])==1


def test_multi_tab_simultaneous_same_confirmation_deduplicated(build_lab):
    current=ticket(build_lab)
    with concurrent.futures.ThreadPoolExecutor(max_workers=4) as pool:
        jobs=list(pool.map(lambda _:fw.create_build(current),range(4)))
    assert len({row['id'] for row in jobs})==1
    build_lab['input']='different'
    with pytest.raises(RuntimeError,match='正在执行'):fw.create_build(ticket(build_lab))


def test_source_and_tool_changes_reject_old_confirmation(build_lab):
    current=ticket(build_lab);build_lab['source']='new-font-or-tool-source'
    with pytest.raises(RuntimeError,match='已变化'):fw.create_build(current)


def test_cancel_only_owned_compiler_group(build_lab,tmp_path):
    event=threading.Event();fw._BUILD_CANCEL['build-lab']=event
    trigger=threading.Timer(.1,event.set);trigger.start()
    try:
        with pytest.raises(fw.BuildCancelled):
            fw._run_build_command([sys.executable,'-c','import time;time.sleep(30)'],timeout=5,
                env={**os.environ,'INKSIGHT_BUILD_ID':'build-lab'},cwd=tmp_path)
    finally:trigger.cancel()


def test_cancelled_build_can_retry_with_new_confirmation(build_lab,monkeypatch):
    job=fw.create_build(ticket(build_lab))
    def cancel(_ident):raise fw.BuildCancelled('已取消')
    monkeypatch.setattr(fw,'_run_build_checked',cancel)
    assert fw.run_build(job['id'])['status']=='cancelled'
    retry=fw.create_build(ticket(build_lab))
    assert retry['id']!=job['id'] and retry['status']=='queued'
