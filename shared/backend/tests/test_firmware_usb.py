"""Byte-addressed synthetic flash; these tests are NOT hardware acceptance."""
import hashlib
import json
from pathlib import Path
import struct
import subprocess
import time

import pytest
from core import firmware_tasks as fw, firmware_usb as usb, firmware_setup as setup, firmware_nvs as nvs, state_store


def synthetic_nvs(version=2):
    page=bytearray(b'\xff'*4096)
    struct.pack_into('<II',page,0,0xfffffffe,1);page[8]=0xfe
    struct.pack_into('<I',page,28,nvs.crc(page[4:28]))
    for index,(namespace,kind,key,value) in enumerate([(0,1,'inksight',1),(1,0x14,'cfg_version',version),(1,0x14,'wifi_n',0)]):
        item=bytearray(b'\xff'*32);item[:4]=bytes([namespace,kind,1,255])
        item[8:24]=key.encode().ljust(16,b'\0')
        item[24:28]=int(value).to_bytes(4,'little')
        struct.pack_into('<I',item,4,nvs.crc(item[:4]+item[8:]))
        page[64+index*32:96+index*32]=item
        page[32+index//4]&=~(1<<(index%4*2))
    return bytes(page)+b'\xff'*12288


@pytest.fixture
def board(tmp_path,monkeypatch):
    monkeypatch.setattr(fw,'_STATE',tmp_path/'tasks.json');monkeypatch.setattr(fw,'_PRIVATE',tmp_path/'private')
    monkeypatch.setattr(fw,'_DEVICE_CACHE',{'usb-unit':{'port':'/dev/cu.synthetic','description':'synthetic'}})
    monkeypatch.setattr(fw,'_identify',lambda _port:('AA:BB:CC:DD:EE:FF','32MB'))
    monkeypatch.setattr(fw,'_port_owner',lambda _port:None)
    monkeypatch.setattr(fw,'get_build',lambda _id:{'stale':False})
    monkeypatch.setattr(setup,'storage_status',lambda:{'ready':True})
    monkeypatch.setattr(setup,'tool_paths',lambda:None)
    image=bytearray(b'\xff'*fw.FLASH_SIZE_BYTES)
    partition=b'P'*int(fw.PARTITION_SIZE,16);image[0x8000:0x8c00]=partition
    image[0x9000:0xd000]=synthetic_nvs()
    header=b'\xe9'+b'\0'*11+b'\x09\x00'+b'\0'*10
    old_app=header+b'INKSIGHT_COMPAT_V1_SSD1677_N32R16'+b'\xff'*0x600000
    old_app=old_app[:0x600000].ljust(0x600000,b'\xff')
    image[0x10000:0x610000]=old_app
    image[0xc10000:0xe00000]=b'F'*0x1f0000
    artifacts=tmp_path/'artifacts';artifacts.mkdir()
    metadata={}
    for name,data,offset in [('firmware.bin',header+b'new-image'*40,'0x10000'),
        ('partitions.bin',partition,'0x8000'),('bootloader.bin',header+b'boot','0x0'),('boot_app0.bin',b'ota','0xe000')]:
        (artifacts/name).write_bytes(data)
        metadata[name]={'bytes':len(data),'sha256':hashlib.sha256(data).hexdigest(),'offset':offset}
    build={'id':'build-unit','status':'completed','source_hash':'source-unit','target':fw.TARGET,'private_dir':str(artifacts),'artifacts':metadata}
    state_store.write_json(fw._STATE,{'builds':{'build-unit':build},'plans':{},'flashes':{},'rollback_plans':{},'rollbacks':{}},meta=False)
    writes=[];commands=[];corrupt=set()
    def read(_port,offset,size,path,**_kwargs):
        offset=int(offset,16);size=int(size,16);raw=bytes(image[offset:offset+size])
        if offset in corrupt:
            corrupt.remove(offset);raw=bytes([raw[0]^1])+raw[1:]
        Path(path).parent.mkdir(parents=True,exist_ok=True);Path(path).write_bytes(raw);return raw
    def command(args,**_kw):
        commands.append(args)
        if 'write_flash' in args:
            index=args.index('write_flash');address=int(args[index+1],16);raw=Path(args[index+2]).read_bytes()
            writes.append((address,len(raw)));image[address:address+len(raw)]=raw
        output='Flash Encryption: Disabled\nSecure Boot: Disabled' if 'get_security_info' in args else 'ok'
        return subprocess.CompletedProcess(args,0,output,'')
    monkeypatch.setattr(fw,'_read_region',read);monkeypatch.setattr(fw,'_run_command',command)
    return {'image':image,'writes':writes,'commands':commands,'corrupt':corrupt,'tmp':tmp_path,'build':build}


def plan_and_flash():
    plan=fw.prepare_flash('build-unit','usb-unit','update')
    task=fw.create_flash(plan['id'],plan['confirmation_token'])
    return plan,task


def test_plan_full_backups_read_only_and_update_single_sector(board):
    before=bytes(board['image']);plan,task=plan_and_flash()
    assert not board['writes'] and plan['backup_verified']
    assert set(plan['backup_manifest'])=={'partition.bin','prefix.bin','nvs.bin','otadata.bin','app0.bin','app1.bin','littlefs.bin'}
    result=fw.run_flash(task['id'])
    assert result['status']=='waiting_heartbeat' and result['target_slot']==1
    assert board['writes']==[(0x610000,board['build']['artifacts']['firmware.bin']['bytes']),(0xd000,4096)]
    after=bytes(board['image'])
    assert after[:0xd000]==before[:0xd000] and after[0xf000:0x610000]==before[0xf000:0x610000]
    assert after[0xc10000:]==before[0xc10000:]
    assert result['physical_display_confirmed'] is False
    for cmd in board['commands']:
        assert '--after' in cmd
        assert cmd[cmd.index('--after')+1]==('hard_reset' if cmd[-1]=='run' else 'no_reset')


def test_resume_releases_rom_mode_without_flash_writes(board):
    board['commands'].clear()
    usb._resume('/dev/cu.synthetic')
    assert len(board['commands'])==1
    command=board['commands'][0]
    assert command[-1]=='run' and command[command.index('--after')+1]=='hard_reset'
    assert not board['writes']


def test_resume_never_resets_an_occupied_port(board,monkeypatch):
    monkeypatch.setattr(fw,'_port_owner',lambda _port:'serial monitor')
    board['commands'].clear()
    with pytest.raises(RuntimeError,match='串口被占用'):usb._resume('/dev/cu.synthetic')
    assert not board['commands'] and not board['writes']


def test_resume_command_failure_is_not_success(board,monkeypatch):
    monkeypatch.setattr(fw,'_run_command',lambda args,**_kw:subprocess.CompletedProcess(args,1,'','reset failed'))
    with pytest.raises(RuntimeError,match='复位失败'):usb._resume('/dev/cu.synthetic')


def test_current_app1_selects_app0_dynamically(board):
    image=board['image'];image[0x610000:0xc10000]=image[0x10000:0x610000]
    ota,_target=fw._next_ota_data(bytes(image[0xd000:0xf000]));image[0xd000:0xf000]=ota
    _plan,task=plan_and_flash();result=fw.run_flash(task['id'])
    assert result['target_slot']==0 and board['writes'][0][0]==0x10000
    assert board['writes'][1][1]==4096


@pytest.mark.parametrize('offset',[0,0x9000,0x610000,0x1000000])
def test_fresh_rejects_data_anywhere_without_erasing(board,offset):
    board['image'][:]=b'\xff'*fw.FLASH_SIZE_BYTES;board['image'][offset]=0
    with pytest.raises(RuntimeError,match='仍有数据'):fw.prepare_flash('build-unit','usb-unit','fresh')
    assert not board['writes']


def test_true_blank_fresh_only_fixed_artifacts(board):
    board['image'][:]=b'\xff'*fw.FLASH_SIZE_BYTES
    plan=fw.prepare_flash('build-unit','usb-unit','fresh');assert plan['backup_verified']
    task=fw.create_flash(plan['id'],plan['confirmation_token']);result=fw.run_flash(task['id'])
    assert result['status']=='waiting_heartbeat'
    assert [address for address,_size in board['writes']]==[0,0x8000,0xe000,0x10000]
    assert not any('erase_flash' in cmd for cmd in board['commands'])


def test_factory_firmware_same_partition_is_not_project(board):
    board['image'][0x10000:0x610000]=(b'\xe9'+b'unknown'*(0x600000//7))[:0x600000].ljust(0x600000,b'\xff')
    # Normalize length without touching other partitions.
    with pytest.raises(RuntimeError,match='兼容固件'):fw.prepare_flash('build-unit','usb-unit','update')
    assert not board['writes']


def test_old_configuration_schema_refused(board):
    board['image'][0x9000:0xd000]=synthetic_nvs(1)
    with pytest.raises(RuntimeError,match='配置版本'):fw.prepare_flash('build-unit','usb-unit','update')
    assert not board['writes']


def test_bad_app_readback_never_switches_selector(board):
    old_selector=bytes(board['image'][0xd000:0xf000]);_plan,task=plan_and_flash()
    # Corrupt the next read of app1 after the fresh backup has run.
    original=usb._write_verified
    def damaged(port,address,path,logs,label,directory):
        if label=='application':board['corrupt'].add(address)
        return original(port,address,path,logs,label,directory)
    from unittest.mock import patch
    with patch.object(usb,'_write_verified',damaged):result=fw.run_flash(task['id'])
    assert result['status']=='failed' and bytes(board['image'][0xd000:0xf000])==old_selector
    assert [offset for offset,_ in board['writes']]==[0x610000]


def test_changed_identity_or_config_before_write(board,monkeypatch):
    _plan,task=plan_and_flash()
    monkeypatch.setattr(fw,'_identify',lambda _port:(':'.join(['11','22','33','44','55','66']),'32MB'))
    assert fw.run_flash(task['id'])['status']=='failed' and not board['writes']


def test_confirmation_expiry_replay_and_corrupt_backup(board):
    plan=fw.prepare_flash('build-unit','usb-unit','update')
    with pytest.raises(RuntimeError):fw.create_flash(plan['id'],'wrong')
    task=fw.create_flash(plan['id'],plan['confirmation_token'])
    with pytest.raises(RuntimeError):fw.create_flash(plan['id'],plan['confirmation_token'])
    path=fw._PRIVATE/plan['id']/'backup/prefix.bin';path.write_bytes(b'bad')
    assert fw.run_flash(task['id'])['status']=='failed' and not board['writes']


def test_nvs_crc_and_runtime_fingerprint():
    raw=synthetic_nvs();assert nvs.decode(raw)['inksight']['cfg_version']==2
    assert len(nvs.protected(raw))==64
    bad=bytearray(raw);bad[28]^=1
    with pytest.raises(ValueError,match='校验'):nvs.protected(bad)
    with pytest.raises(ValueError,match='配置版本'):nvs.protected(synthetic_nvs(9))


def test_pin_profile_and_preservation_are_in_supported_target():
    text=(fw._FIRMWARE/'platformio.ini').read_text().split('[env:epd_426_ssd1677_s3_n32r16]')[1]
    for flag in ['-DPIN_EPD_DC=13','-DPIN_EPD_RST=14','-DPIN_EPD_BUSY=4','-DPIN_EPD_PWR=5','-DINKSIGHT_PRESERVE_PROVISIONED_WIFI=1']:
        assert flag in text


def test_web_update_backup_is_accepted_by_formal_rollback(board):
    before=bytes(board['image']);_plan,task=plan_and_flash()
    assert fw.run_flash(task['id'])['status']=='waiting_heartbeat'
    count=len(board['writes'])
    plan=fw.prepare_rollback(task['id'],'usb-unit')
    rollback=fw.create_rollback(plan['id'],plan['confirmation_token'])
    result=fw.run_rollback(rollback['id'])
    assert result['status']=='written_unconfirmed'
    assert len(board['writes'])==count+1 and board['writes'][-1][1]==4096
    assert bytes(board['image'][0xd000:0xf000])==before[0xd000:0xf000]
    assert bytes(board['image'][0x9000:0xd000])==before[0x9000:0xd000]


@pytest.mark.parametrize('name,size,offset',[('firmware.bin',0x600001,'0x10000'),
    ('bootloader.bin',0x8001,'0x0'),('boot_app0.bin',0x2001,'0xe000'),('firmware.bin',32,'0x9000')])
def test_oversize_or_wrong_artifact_offset_refused_before_usb(board,name,size,offset):
    build=board['build'];path=Path(build['private_dir'])/name
    data=b'\xe9'+b'\0'*(size-1);path.write_bytes(data)
    build['artifacts'][name].update(bytes=size,sha256=hashlib.sha256(data).hexdigest(),offset=offset)
    fw._update(lambda state:state['builds'].update({'build-unit':build}))
    with pytest.raises(RuntimeError,match='范围'):fw.prepare_flash('build-unit','usb-unit','update')
    assert not board['commands'] and not board['writes']


def test_secure_or_encrypted_target_fails_closed(board,monkeypatch):
    old=fw._run_command
    def secured(args,**kwargs):
        if 'get_security_info' in args:return subprocess.CompletedProcess(args,0,'Secure Boot: Enabled\nFlash Encryption: Disabled','')
        return old(args,**kwargs)
    monkeypatch.setattr(fw,'_run_command',secured)
    with pytest.raises(RuntimeError,match='安全启动'):fw.prepare_flash('build-unit','usb-unit','update')
    assert not board['writes']


def test_change_during_backup_is_rejected_before_write(board,monkeypatch):
    _plan,task=plan_and_flash();count=0
    def stale(_id):
        nonlocal count
        count+=1;return {'stale':count>1}
    monkeypatch.setattr(fw,'get_build',stale)
    assert fw.run_flash(task['id'])['status']=='failed' and not board['writes']


def test_failure_switching_selector_restores_only_affected_sector(board,monkeypatch):
    before=bytes(board['image'][0xd000:0xf000]);_plan,task=plan_and_flash()
    old=usb._write_verified
    def broken(port,address,path,logs,label,directory):
        if label=='selector':
            old(port,address,path,logs,label,directory)
            raise RuntimeError('injected selector verification failure')
        return old(port,address,path,logs,label,directory)
    monkeypatch.setattr(usb,'_write_verified',broken)
    result=fw.run_flash(task['id'])
    assert result['status']=='failed' and result['selector_restored']
    assert bytes(board['image'][0xd000:0xf000])==before
    assert [size for _address,size in board['writes'][1:]]==[4096,4096]


def test_expired_confirmation_never_creates_flash_job(board):
    plan=fw.prepare_flash('build-unit','usb-unit','update')
    fw._set('plans',plan['id'],expires_at=time.time()-1)
    with pytest.raises(RuntimeError,match='expired'):fw.create_flash(plan['id'],plan['confirmation_token'])
    assert not fw._read()['flashes'] and not board['writes']


def test_interrupted_fresh_holds_rom_without_erasing_or_rebooting(board,monkeypatch):
    board['image'][:]=b'\xff'*fw.FLASH_SIZE_BYTES
    plan=fw.prepare_flash('build-unit','usb-unit','fresh')
    task=fw.create_flash(plan['id'],plan['confirmation_token'])
    board['commands'].clear()
    original=usb._write_verified
    def fail_first(*args,**kwargs):
        original(*args,**kwargs)
        raise OSError('synthetic power interruption')
    monkeypatch.setattr(usb,'_write_verified',fail_first)
    result=fw.run_flash(task['id'])
    assert result['status']=='failed' and result['needs_recovery']
    assert len(board['writes'])==1
    assert not any('run' in args or 'erase_flash' in args for args in board['commands'])
    with pytest.raises(RuntimeError):fw.prepare_flash('build-unit','usb-unit','fresh')


@pytest.mark.parametrize('address',[0x10000,0xc10000,0xd000])
def test_failed_protected_region_check_never_reboots_uncertain_old_app(board,monkeypatch,address):
    _plan,task=plan_and_flash();board['commands'].clear()
    original=usb._write_verified
    def disturb(*args,**kwargs):
        result=original(*args,**kwargs)
        if args[4]=='application':board['image'][address]^=1
        return result
    monkeypatch.setattr(usb,'_write_verified',disturb)
    result=fw.run_flash(task['id'])
    assert result['status']=='failed' and result['needs_recovery']
    assert len(board['writes'])==1
    assert not any('run' in args for args in board['commands'])


def test_backup_disconnect_fails_before_any_write(board,monkeypatch):
    _plan,task=plan_and_flash()
    original=fw._read_region
    def disconnected(port,offset,size,path,**kwargs):
        if int(offset,16)==0x610000:raise OSError('disconnected during backup')
        return original(port,offset,size,path,**kwargs)
    monkeypatch.setattr(fw,'_read_region',disconnected)
    result=fw.run_flash(task['id'])
    assert result['status']=='failed' and not board['writes']


def test_quit_barrier_active_even_after_write_status_finishes(board,monkeypatch):
    from core.desktop_service import gate
    _plan,task=plan_and_flash()
    original=usb.run_flash
    def observe(ident):
        gate.pause()
        assert gate.active>0
        return original(ident)
    monkeypatch.setattr(usb,'run_flash',observe)
    assert fw.run_flash(task['id'])['status']=='waiting_heartbeat'
    assert gate.active==0


def test_settings_fingerprint_runtime_marker_not_wifi_or_credentials(monkeypatch):
    data={'inksight':{'cfg_version':2,'wifi_n':1,'w_s0':'synthetic-network','w_p0':'synthetic-secret',
                      'live_boot_mk':'old-build'}}
    monkeypatch.setattr(nvs,'decode',lambda _raw:data)
    baseline=nvs.protected(b'synthetic')
    data['inksight']['live_boot_mk']='new-build'
    assert nvs.protected(b'synthetic')==baseline
    data['inksight']['w_p0']='changed-synthetic'
    assert nvs.protected(b'synthetic')!=baseline
    del data['inksight']['wifi_n']
    with pytest.raises(ValueError,match='Wi-Fi列表'):nvs.protected(b'synthetic')
    data['inksight']['wifi_n']=0;data['inksight']['unknown']={'blob_index':'same-size-not-content'}
    with pytest.raises(ValueError,match='二进制'):nvs.protected(b'synthetic')


@pytest.mark.parametrize('adc,busy,expected_reads',[(4,4,0),(0,4,16)])
def test_real_battery_function_compiles_and_never_samples_busy_pin(tmp_path,adc,busy,expected_reads):
    # Compile the actual function, not a reimplemented condition. The shared
    # BUSY profile must compile AND must not call analogRead at runtime.
    root=Path(__file__).resolve().parents[3]
    source=(root/'shared/firmware/src/network.cpp').read_text()
    body=source.split('float readBatteryVoltage() {',1)[1].split('// ── Stream helper',1)[0]
    function='float readBatteryVoltage() {'+body
    shim=f'''#include <cassert>
#include <cstdint>
#define PIN_BAT_ADC {adc}
#define PIN_EPD_BUSY {busy}
int reads=0;
int analogRead(int pin){{assert(pin!=PIN_EPD_BUSY);++reads;return 2048;}}
void delayMicroseconds(int){{}}
struct SerialShim{{template<class... T> void printf(const char*,T...){{}}}} Serial;
'''
    main=f'''int main(){{float value=readBatteryVoltage();assert(reads=={expected_reads});
        assert({'value==0.0f' if expected_reads==0 else 'value>3.2f && value<3.4f'});}}'''
    cpp=tmp_path/'battery.cpp';cpp.write_text(shim+function+main)
    target=tmp_path/'battery'
    subprocess.run(['clang++','-std=c++17',str(cpp),'-o',str(target)],check=True,capture_output=True)
    subprocess.run([str(target)],check=True,capture_output=True)
