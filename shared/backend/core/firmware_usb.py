"""One guarded USB implementation used by the formal management workflow.

Reuses firmware_tasks' opaque identities, immutable builds, OTA codec and
durable jobs. This module never borrows an engineer's handoff or backups.
"""
from __future__ import annotations
import contextlib
import hashlib
import hmac
import os
from pathlib import Path
import re
import shutil
import threading
import time

from . import firmware_tasks as fw, firmware_setup, firmware_nvs, state_store
from .desktop_service import critical_entry

FS_OFFSET='0xc10000'; FS_SIZE='0x1f0000'
_SESSION=threading.local()


def mark_touched():_SESSION.touched=True


@contextlib.contextmanager
def usb_session():
    if not fw._FLASH_LOCK.acquire(blocking=False):raise RuntimeError('已有 USB 操作，请等待完成')
    _SESSION.touched=False
    try:
        with fw._exclusive_lock('flash'):yield
    finally:fw._FLASH_LOCK.release()


def _resume(port):
    if fw._port_owner(port):raise RuntimeError('串口被占用，未发送复位命令，请正常释放后重新插拔')
    # In pinned esptool 4.11, run() defaults to reboot=False. Ending with
    # no_reset leaves the ESP32-S3 in ROM even when the command succeeds.
    result=fw._run_command(fw._esptool(port,'run',after='hard_reset'),timeout=30,cwd=fw._ROOT)
    if result.returncode:raise RuntimeError('只读操作结束，但设备复位失败，请正常重新插拔USB')


def _security(port):
    result=fw._run_command(fw._esptool(port,'get_security_info'),timeout=30,cwd=fw._ROOT)
    output=(result.stdout or '')+'\n'+(result.stderr or '')
    if (result.returncode or not re.search(r'Flash Encryption:\s*Disabled',output,re.I)
        or not re.search(r'Secure Boot:\s*Disabled',output,re.I)):
        raise RuntimeError('无法确认安全启动与加密状态为已支持的关闭状态，拒绝写入')


def _read(port,offset,size,directory,name):
    return fw._read_region(port,hex(offset) if isinstance(offset,int) else offset,
        hex(size) if isinstance(size,int) else size,directory/name,timeout=12*60)


def _check_partition(build,port,directory,mode):
    raw=_read(port,fw.PARTITION_OFFSET,fw.PARTITION_SIZE,directory,'partition.bin')
    expected=fw._artifact_path(build,'partitions.bin').read_bytes()
    if len(expected)!=int(fw.PARTITION_SIZE,16):raise RuntimeError('构建分区表大小不符')
    state=fw._partition_state(raw,expected)
    if mode=='fresh' and state!='blank':raise RuntimeError('不是空白开发板；未知或厂家预置板不会自动清空')
    if mode=='update' and state!='matching':raise RuntimeError('分区不匹配，未知或厂家预置板不能按兼容更新写入')
    return state


def _project_image(raw):
    # New builds have an explicit compatibility marker. The legacy project
    # profile additionally requires its three independent fixed log markers.
    return len(raw)>=24 and raw[:1]==b'\xe9' and int.from_bytes(raw[12:14],'little')==9 and (b'INKSIGHT_COMPAT_V1_SSD1677_N32R16' in raw or
        all(marker in raw for marker in [b'[P3][PLAN]',b'[PANEL] v2 page=',b'[CACHE] structured']))


def validate_artifacts(build):
    limits={'bootloader.bin':(0,0x8000),'partitions.bin':(0x8000,0xc00),
            'boot_app0.bin':(0xe000,0x2000),'firmware.bin':(0x10000,0x600000)}
    if build.get('target')!=fw.TARGET:raise RuntimeError('构建硬件目标不符')
    for name,(offset,limit) in limits.items():
        path=fw._artifact_path(build,name);metadata=build['artifacts'][name]
        if int(metadata['offset'],16)!=offset or not 0<path.stat().st_size<=limit:
            raise RuntimeError('镜像大小或写入范围超出固定硬件布局')
        if name in {'bootloader.bin','firmware.bin'}:
            header=path.read_bytes()[:24]
            if len(header)<24 or header[0]!=0xe9 or int.from_bytes(header[12:14],'little')!=9:
                raise RuntimeError('应用或引导镜像不是 ESP32-S3 格式')
        if name=='partitions.bin' and path.stat().st_size!=limit:raise RuntimeError('分区表长度不符')


def backup_device(build,port,directory,mode,progress=lambda _text:None):
    directory.mkdir(mode=0o700,parents=True,exist_ok=True)
    progress('正在核对分区与安全状态，未执行写入')
    _security(port);partition=_check_partition(build,port,directory,mode)
    files=['partition.bin'];target=None;selector=None
    if mode=='fresh':
        progress('正在读取全部32MiB Flash，确认真正空白；不会擦除厂家数据')
        raw=_read(port,0,fw.FLASH_SIZE_BYTES,directory,'blank-full.bin')
        if any(value!=255 for value in raw):raise RuntimeError('Flash 其他区域仍有数据，拒绝当作空白板安装')
        files.append('blank-full.bin')
    else:
        progress('正在备份引导、NVS、启动项、双应用与文件系统；请保持USB连接')
        prefix=_read(port,0,0x10000,directory,'prefix.bin')
        nvs=prefix[0x9000:0xd000];ota=prefix[0xd000:0xf000]
        try:protected=firmware_nvs.protected(nvs)
        except ValueError as error:raise RuntimeError(str(error)) from None
        (directory/'nvs.bin').write_bytes(nvs);(directory/'otadata.bin').write_bytes(ota)
        if not fw._valid_ota_records(ota) and any(value!=255 for value in ota):raise RuntimeError('OTA启动记录异常，拒绝更新')
        selector,target=fw._next_ota_data(ota)
        for slot in [0,1]:_read(port,0x10000+slot*0x600000,0x600000,directory,f'app{slot}.bin')
        if not _project_image((directory/f'app{1-target}.bin').read_bytes()):
            raise RuntimeError('未识别为本项目兼容固件，不能仅凭相同分区更新未知设备')
        _read(port,FS_OFFSET,FS_SIZE,directory,'littlefs.bin')
        files+=['prefix.bin','nvs.bin','otadata.bin','app0.bin','app1.bin','littlefs.bin']
    manifest={name:firmware_setup._hash(directory/name) for name in files}
    state_store.write_json(directory/'manifest.json',{'files':manifest,'install_mode':mode,
        'partition_state':partition,'target_slot':target},meta=False)
    return {'backup_manifest':manifest,'target_slot':target,'partition_state':partition},selector


def _prepare(build_id,device_id,mode,ident):
    directory=fw._PRIVATE/ident/'backup'
    device=fw._DEVICE_CACHE.get(device_id);build=fw._read()['builds'].get(build_id)
    if mode not in {'fresh','update'} or not device or not build or build.get('status')!='completed':raise RuntimeError('构建、设备或模式无效')
    if fw.get_build(build_id).get('stale'):raise RuntimeError('构建已过期，请重新构建')
    if not firmware_setup.storage_status()['ready']:raise RuntimeError('备份空间不足，请至少保留4GiB')
    validate_artifacts(build)
    with usb_session():
        port=device['port']
        try:
            mac,capacity=fw._identify(port)
            evidence,_selector=backup_device(build,port,directory,mode,
                lambda text:fw._set('plans',ident,progress=text))
        finally:
            if getattr(_SESSION,'touched',False):_resume(port)
    now=int(time.time());token=os.urandom(24).hex()
    plan={'id':ident,'build_id':build_id,'device_id':device_id,'status':'ready',
        'target':fw.TARGET,'chip':'ESP32-S3','flash_size':capacity,'install_mode':mode,
        'device_fingerprint':hashlib.sha256(mac.encode()).hexdigest()[:12],
        'artifact_sha256':build['artifacts']['firmware.bin']['sha256'],
        'artifact_manifest':build['artifacts'],'source_hash':build['source_hash'],
        'port':port,'mac':mac,'confirmation_token':token,'created_at':now,'expires_at':now+300,
        **evidence,'backup_verified':True,
        'progress':'只读核对与备份已完成；尚未写入设备',
        'scope':'仅写未运行的应用槽和一个替代OTA扇区；保留原活动应用、Wi-Fi、NVS、文件系统与身份。' if mode=='update' else
                '已确认全部Flash空白，仅写引导、分区表、OTA初始化和app0；不执行全片擦除。'}
    fw._update(lambda data:data['plans'].update({ident:plan}))
    return public_plan(plan)


def public_plan(plan):
    public=fw._public_job(plan)
    if plan.get('status')=='ready':public['confirmation_token']=plan['confirmation_token']
    return public


def get_plan(ident):
    plan=fw._read()['plans'].get(ident)
    if not plan:raise KeyError(ident)
    if plan['status'] in {'queued','running'}:
        thread=fw._THREADS.get(ident)
        if not thread or not thread.is_alive():
            fw._set('plans',ident,status='interrupted',error='核对中断，未获写入确认；重新识别即可')
            plan=fw._read()['plans'][ident]
    return public_plan(plan)


@critical_entry
def start_plan(build_id,device_id,mode):
    with fw._THREADS_LOCK:
        if any(row.get('status') in {'queued','running'} for row in fw._read()['plans'].values()):
            raise RuntimeError('已有核对任务，请等待完成')
        ident=fw._new_id('flash-plan')
        fw._update(lambda data:data['plans'].update({ident:{'id':ident,'status':'queued','created_at':int(time.time()),
            'progress':'准备只读核对和备份，不写入设备'}}))
        def worker():
            try:
                fw._set('plans',ident,status='running')
                _prepare(build_id,device_id,mode,ident)
            except Exception as error:
                fw._set('plans',ident,status='failed',error=str(error) if isinstance(error,RuntimeError) else '核对失败，未执行写入')
        thread=threading.Thread(target=worker,daemon=True,name='usb-'+ident)
        fw._THREADS[ident]=thread;thread.start()
        return public_plan(fw._read()['plans'][ident])


def prepare_sync(build_id,device_id,mode):
    ident=fw._new_id('flash-plan')
    fw._update(lambda data:data['plans'].update({ident:{'id':ident,'status':'running'}}))
    try:return _prepare(build_id,device_id,mode,ident)
    except Exception:
        fw._set('plans',ident,status='failed');raise


def _selector_sector(old,new):
    changed=[start for start in [0,4096] if old[start:start+4096]!=new[start:start+4096]]
    if len(changed)!=1:raise RuntimeError('OTA切换不是单扇区变动，拒绝写入')
    return changed[0]


def _write_verified(port,offset,path,logs,label,directory):
    fw._step(fw._esptool(port,'--baud',fw.BAUD,'write_flash',hex(offset),str(path)),logs,label,12*60)
    raw=_read(port,offset,path.stat().st_size,directory,label+'-readback.bin')
    if raw!=path.read_bytes():raise RuntimeError(label+'回读不一致')


def run_flash(ident):
    state=fw._read();job=state['flashes'].get(ident)
    if not job:raise KeyError(ident)
    build=state['builds'].get(job['build_id']);plan=state['plans'].get(job['plan_id'])
    with usb_session():
        directory=fw._PRIVATE/ident;backup=directory/'backup';directory.mkdir(parents=True,exist_ok=True,mode=0o700)
        logs=[];selector_attempted=False;write_attempted=False;verified=False;port=job['port'];old=None;sector=None
        fw._set('flashes',ident,status='running',progress='重新识别并创建写入前最新备份')
        try:
            if not build or not plan or fw.get_build(job['build_id']).get('stale'):raise RuntimeError('构建快照已变化')
            validate_artifacts(build)
            if plan['expires_at']<time.time():raise RuntimeError('写入确认已过期，请重新核对')
            for name,digest in plan['backup_manifest'].items():
                if firmware_setup._hash(fw._PRIVATE/job['plan_id']/'backup'/name)!=digest:raise RuntimeError('确认前备份校验不符')
            mac,_capacity=fw._identify(port)
            if not hmac.compare_digest(mac,job['mac']):raise RuntimeError('检测到设备已更换')
            evidence,next_ota=backup_device(build,port,backup,job['install_mode'],
                lambda text:fw._set('flashes',ident,progress=text))
            if fw.get_build(job['build_id']).get('stale'):raise RuntimeError('备份期间配置或构建条件改变，未执行写入')
            fw._set('flashes',ident,**evidence,backup_available=True,backup_verified=True)
            if evidence['target_slot']!=plan.get('target_slot'):raise RuntimeError('当前活动槽已变化，需重新确认')
            if job['install_mode']=='update':
                old=(backup/'otadata.bin').read_bytes();sector=_selector_sector(old,next_ota)
                if hashlib.sha256(old).hexdigest()!=plan['backup_manifest']['otadata.bin']:
                    raise RuntimeError('启动项自确认前已改变，拒绝写入')
                target=evidence['target_slot'];app=fw._artifact_path(build,'firmware.bin')
                fw._set('flashes',ident,progress='写入非活动应用并完整回读；尚未切换启动项')
                write_attempted=True
                _write_verified(port,0x10000+target*0x600000,app,logs,'application',directory)
                expected_prefix=(backup/'prefix.bin').read_bytes()
                prefix=_read(port,0,0x10000,directory,'prefix-before-selector.bin')
                if prefix!=expected_prefix:raise RuntimeError('保护区域改变，停止切换启动项')
                for slot in [1-target]:
                    live=_read(port,0x10000+slot*0x600000,0x600000,directory,'old-active-after.bin')
                    if live!=(backup/f'app{slot}.bin').read_bytes():raise RuntimeError('原活动应用改变，停止切换')
                if _read(port,FS_OFFSET,FS_SIZE,directory,'fs-after.bin')!=(backup/'littlefs.bin').read_bytes():
                    raise RuntimeError('文件系统改变，停止切换')
                (directory/'otadata-next.bin').write_bytes(next_ota)
                sector_path=directory/'selector-sector.bin';sector_path.write_bytes(next_ota[sector:sector+4096])
                fw._set('flashes',ident,next_ota_sha256=hashlib.sha256(next_ota).hexdigest(),progress='保护区域校验通过，切换单个OTA扇区')
                selector_attempted=True
                _write_verified(port,0xd000+sector,sector_path,logs,'selector',directory)
                prefix=_read(port,0,0x10000,directory,'prefix-final.bin')
                if prefix[:0xd000]!=expected_prefix[:0xd000] or prefix[0xf000:]!=expected_prefix[0xf000:] or prefix[0xd000:0xf000]!=next_ota:
                    raise RuntimeError('最终启动项或保护区域回读不符')
            else:
                for name in ['bootloader.bin','partitions.bin','boot_app0.bin','firmware.bin']:
                    artifact=fw._artifact_path(build,name)
                    fw._set('flashes',ident,progress='首次安装：'+name+'，写入并完整回读')
                    write_attempted=True
                    _write_verified(port,int(build['artifacts'][name]['offset'],16),artifact,logs,name,directory)
            verified=True;now=int(time.time())
            fw._set('flashes',ident,status='waiting_heartbeat',verified_at=now,heartbeat_deadline=now+600,
                verification='flash-verified; awaiting-device-receipt',progress='写入已校验，联网与实屏仍需确认',
                network_verified=False,physical_display_confirmed=False,log='\n'.join(logs)[-12000:])
            _resume(port)
            paths=firmware_setup.tool_paths()
            if paths:
                result=fw._run_command([str(paths['python']),str(fw._ROOT/'shared/tools/firmware_receipt.py'),
                    '--port',port,'--build-id',job['build_id'],'--seconds','70'],timeout=80,cwd=fw._ROOT)
                import json
                try:receipt=json.loads(result.stdout or '{}')
                except ValueError:receipt={}
                if result.returncode==0 and receipt.get('network_verified') is True:
                    settings_checked=False
                    if job['install_mode']=='update':
                        try:
                            after=_read(port,fw.NVS_OFFSET,fw.NVS_SIZE,directory,'nvs-after-boot.bin')
                            settings_checked=firmware_nvs.protected(after)==firmware_nvs.protected((backup/'nvs.bin').read_bytes())
                        finally:_resume(port)
                        if not settings_checked:
                            return fw._set('flashes',ident,status='settings_changed',network_verified=True,
                                error='启动后受保护设置发生变化，未自动覆盖或恢复旧NVS，请检查备份',
                                device_receipt=receipt,protected_settings_verified=False)
                    return fw._set('flashes',ident,status='completed',finished_at=int(time.time()),
                        network_verified=True,device_receipt=receipt,verification='flash-verified; USB-network-receipt',
                        protected_settings_verified=settings_checked,
                        progress='写入与设备联网回执已确认；请单独观察实体屏')
            return fw.get_flash(ident)
        except Exception as error:
            if verified:
                return fw._set('flashes',ident,status='waiting_heartbeat',
                    progress='写入已校验；USB联网回执未取得，请继续检查设备联网',network_verified=False)
            restored=False
            if selector_attempted and not verified and old is not None:
                try:
                    path=directory/'restore-selector-sector.bin';path.write_bytes(old[sector:sector+4096])
                    _write_verified(port,0xd000+sector,path,logs,'restore-selector',directory)
                    restored=True
                except Exception:pass
            # A partly written fresh board has no verified old application to
            # resume. Hold ROM; never boot partial firmware or silently erase.
            if job['install_mode']=='fresh' and write_attempted:
                can_resume=False
            elif not selector_attempted or restored:
                can_resume=True
                if (backup/'prefix.bin').is_file():
                    try:
                        before=(backup/'prefix.bin').read_bytes()
                        after=_read(port,0,0x10000,directory,'failure-prefix.bin')
                        can_resume=after==before
                        if can_resume and write_attempted:
                            slot=1-evidence['target_slot']
                            live=_read(port,0x10000+slot*0x600000,0x600000,directory,'failure-old-active.bin')
                            fs=_read(port,FS_OFFSET,FS_SIZE,directory,'failure-fs.bin')
                            can_resume=(live==(backup/f'app{slot}.bin').read_bytes()
                                        and fs==(backup/'littlefs.bin').read_bytes())
                    except Exception:can_resume=False
                if can_resume:
                    try:_resume(port)
                    except Exception:pass
            else:can_resume=False
            return fw._set('flashes',ident,status='failed',error=str(error) if isinstance(error,RuntimeError) else '受控写入失败，私有备份保留',
                selector_restored=restored,needs_recovery=not can_resume,
                backup_available=backup.exists(),log='\n'.join(logs)[-12000:])
