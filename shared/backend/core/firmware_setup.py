"""Opt-in, private, atomic firmware prerequisites. Never writes USB/cloud.

No environment is borrowed from ~/.platformio, a developer checkout, Homebrew,
or system Python. The running interpreter creates a private venv. Successful
generations are switched by one pointer only after verification.
"""
from __future__ import annotations

import contextlib
import fcntl
import hashlib
import importlib.util
import json
import os
from pathlib import Path
import platform
import shutil
import subprocess
import sys
import tempfile
import threading
import time

from . import state_store
from .desktop_service import critical_entry, gate

ROOT = Path(__file__).resolve().parents[3]
TARGET = 'epd_426_ssd1677_s3_n32r16'
CORE_VERSION = '6.1.19'
MIN_FREE = 4 * 1024**3
FONT_MAX = 32 * 1024**2
FONT_NAMES = [f'fonts_gen/misans_{style}_{size}.h' for style, sizes in
              (('reg', (16,21,24,30,62,82)), ('bold', (24,30,62,82))) for size in sizes]
FONT_NAMES += ['fonts_misans_16.h', 'fonts_misans_24.h']
_WORKERS = {}
_LOCK = threading.RLock()
FONT_UPLOAD_LOCK = threading.Lock()
_MANIFEST_CACHE = {}


def home():
    raw = os.environ.get('INKSIGHT_FIRMWARE_HOME')
    return Path(raw) if raw and Path(raw).is_absolute() else ROOT / '.local/firmware'


def _state_file():
    return home() / 'preparation.json'


def _read():
    data, error = state_store.read_json(_state_file())
    if error == 'missing':return {'jobs': {}}
    if error or not isinstance(data,dict):raise RuntimeError('准备状态无法读取，保留原文件供检查')
    return data


def _set(ident, **fields):
    def update(data):
        row = data.setdefault('jobs',{}).setdefault(ident, {'id':ident})
        row.update(fields, updated_at=int(time.time()))
        return row.copy()
    return state_store.update_json(_state_file(),update,default={'jobs':{}})


def _pointer(kind):
    pointer = home() / (kind+'-current.json')
    try:
        data=json.loads(pointer.read_text())
        ident=data['generation']
        if not isinstance(ident,str) or not ident.startswith(kind+'-') or Path(ident).name!=ident:return None
        path=home()/'generations'/ident
        if not path.is_dir() or path.is_symlink():return None
        return path
    except (OSError,ValueError,KeyError,TypeError):return None


def font_source():
    path=_pointer('fonts')
    return path/'src' if path else None


def tool_paths():
    path=_pointer('tools')
    if path is None:return None
    core=path/'pio-core'
    return {'generation':path,'python':path/'venv/bin/python', 'core':core,
            'esptool':core/'packages/tool-esptoolpy/esptool.py',
            'boot_app0':core/'packages/framework-arduinoespressif32/tools/partitions/boot_app0.bin'}


def _hash(path):
    digest=hashlib.sha256()
    with path.open('rb') as stream:
        for chunk in iter(lambda:stream.read(1024**2),b''):digest.update(chunk)
    return digest.hexdigest()


def _manifest(directory, names):
    result={}
    for name in names:
        path=directory/name
        if path.is_symlink() or not path.is_file() or not path.stat().st_size:
            raise RuntimeError('所需文件缺失或不是普通文件')
        result[name]={'bytes':path.stat().st_size,'sha256':_hash(path)}
    return result


def _valid_manifest(directory, name, names=None):
    try:
        data=json.loads((directory/name).read_text())
        rows=data['files']
        if names is not None and set(rows)!=set(names):return False
        if not rows:return False
        # Verify unchanged payload once, then re-hash on any inode/mtime/size
        # change. The full stat key also detects removed or replaced files.
        stamps=[]
        for key,row in rows.items():
            path=directory/key
            if Path(key).is_absolute() or '..' in Path(key).parts or path.is_symlink() or not path.is_file():return False
            info=path.stat()
            if info.st_size!=row['bytes']:return False
            stamps.append((key,info.st_ino,info.st_size,info.st_mtime_ns))
        stamp=(_hash(directory/name),tuple(stamps))
        cachekey=str(directory/name)
        if _MANIFEST_CACHE.get(cachekey)==stamp:return True
        if not all(_hash(directory/key)==row['sha256'] for key,row in rows.items()):return False
        _MANIFEST_CACHE[cachekey]=stamp
        return True
    except (OSError,ValueError,KeyError,TypeError):return False


def fonts_status():
    source=font_source()
    if source and _valid_manifest(source,'manifest.json',FONT_NAMES):
        return {'ready':True,'state':'ready','detail':'Regular / Bold 与 12 份字库已校验','sha256':_hash(source/'manifest.json')}
    return {'ready':False,'state':'missing' if source is None else 'invalid',
            'detail':'请选择你从小米官方取得的 MiSans Regular 和 Bold 单独 OTF/TTF；不支持 TTC 或可变字体'}


def tools_status():
    if sys.version_info[:2]!=(3,11):
        return {'ready':False,'state':'unsupported_runtime','detail':'图形准备使用 Python 3.11。App 已内置；源码模式请使用3.11环境'}
    paths=tool_paths()
    if not paths:return {'ready':False,'state':'not_installed','detail':'点击准备后安装到本机私有目录，不修改系统环境'}
    folder=paths['generation']
    try:
        manifest=json.loads((folder/'manifest.json').read_text())
        ready=manifest['platformio']==CORE_VERSION and all(paths[k].is_file() for k in ['python','esptool','boot_app0'])
        ready=ready and manifest.get('python_lock_sha256')==_hash(ROOT/'requirements-firmware-py311-macos-arm64.lock')
        ready=ready and manifest.get('esptool_lock_sha256')==_hash(ROOT/'requirements-esptool-py311-macos-arm64.lock')
        ready=ready and _valid_manifest(folder,'manifest.json')
    except (OSError,ValueError,KeyError,TypeError):ready=False
    return {'ready':ready,'state':'ready' if ready else 'invalid',
            'detail':'固定版本构建工具已准备' if ready else '工具文件缺失或校验不符，请重新准备',
            'platformio':CORE_VERSION,'sha256':_hash(folder/'manifest.json') if ready else None}


def host_status():
    system,machine=platform.system(),platform.machine()
    version=platform.mac_ver()[0]
    try:supported=system=='Darwin' and machine=='arm64' and tuple(map(int,version.split('.')[:2]))>=(26,5)
    except ValueError:supported=False
    return {'ready':supported,'state':'supported' if supported else 'unsupported',
            'detail':'Apple Silicon / macOS 26.5 或更新' if supported else '当前只支持 Apple Silicon Mac 与 macOS 26.5 或更新',
            'system':system,'architecture':machine,'minimum_macos':'26.5'}


def storage_status():
    path=home()
    try:
        path.mkdir(mode=0o700,parents=True,exist_ok=True);path.chmod(0o700)
        with tempfile.TemporaryFile(dir=path) as stream:stream.write(b'test')
        free=shutil.disk_usage(path).free
        return {'ready':free>=MIN_FREE,'state':'ready' if free>=MIN_FREE else 'low_space',
                'detail':'至少需要 4 GiB 可用空间','free_bytes':free,'required_bytes':MIN_FREE}
    except OSError:return {'ready':False,'state':'unwritable','detail':'私有准备目录不可写，请检查用户目录权限'}


def status():
    data=_read();jobs=[]
    for ident,row in data.get('jobs',{}).items():
        if row.get('status') in {'queued','running'} and ident not in _WORKERS:
            row=_set(ident,status='interrupted',detail='上次准备未完成，旧可用环境保留；请重新准备')
        jobs.append(row)
    return {'host':host_status(),'fonts':fonts_status(),'tools':tools_status(),
            'storage':storage_status(),'jobs':sorted(jobs,key=lambda r:r.get('updated_at',0),reverse=True)[:8],
            'network':{'state':'on_demand','detail':'仅点击准备时访问 PyPI 与 PlatformIO 官方注册源，失败不绕过 TLS'},
            'mode':'app' if os.environ.get('INKSIGHT_DESKTOP_MODE')=='1' else 'source',
            'writes_device':False,'calls_paid_model':False}


def active_jobs():
    return sum(row.get('status') in {'queued','running'} for row in _read().get('jobs',{}).values())


@contextlib.contextmanager
def operation_lock(blocking=False):
    home().mkdir(mode=0o700,parents=True,exist_ok=True)
    with (home()/'.operation.lock').open('a+b') as lock:
        try:fcntl.flock(lock,fcntl.LOCK_EX | (0 if blocking else fcntl.LOCK_NB))
        except BlockingIOError:raise RuntimeError('已有字体、工具准备或构建任务，请等待完成') from None
        try:yield
        finally:fcntl.flock(lock,fcntl.LOCK_UN)


def _check_cancel(ident):
    if _read()['jobs'][ident].get('cancel_requested'):raise RuntimeError('cancelled')


def _failure_detail(log):
    with log.open('rb') as stream:
        stream.seek(0,os.SEEK_END);stream.seek(max(0,stream.tell()-65536))
        tail=stream.read().decode('utf-8',errors='replace').lower()
    if any(value in tail for value in ['no space left','disk quota exceeded']):return '下载或解包时空间不足，请释放空间后重试；旧环境保留'
    if any(value in tail for value in ['certificate_verify_failed','certificate verify failed','sslerror']):return 'TLS证书校验失败；未绕过安全校验，请检查系统时间或网络后重试'
    if any(value in tail for value in ['do not match the hashes','hash mismatch','checksum mismatch']):return '下载摘要校验不符，未启用；请从可信网络重新准备'
    if any(value in tail for value in ['no matching distribution','could not find a version','unknownpackageerror']):return '固定版本依赖暂不可用或不匹配当前主机，未启用新环境'
    if any(value in tail for value in ['connectionerror','nameresolutionerror','network is unreachable','failed to establish a new connection','temporary failure in name resolution']):return '无法连接官方依赖源，可能离线；检查网络后可重新准备'
    return '工具准备失败，旧环境未替换；请检查网络或依赖后重试'


def _command(ident,args, *, env, timeout, cwd):
    _check_cancel(ident)
    # Only fixed commands, no shell. A preparation cancellation may terminate
    # its own process group; no USB/paid workers ever run in this group.
    log=home()/'generations'/ident/'command-private.log'
    import signal
    with log.open('ab') as stream:
        log.chmod(0o600)
        process=subprocess.Popen(args,cwd=cwd,env=env,stdout=stream,stderr=subprocess.STDOUT,start_new_session=True)
        deadline=time.monotonic()+timeout
        try:
            while process.poll() is None:
                _check_cancel(ident)
                if time.monotonic()>deadline:raise RuntimeError('准备超时，请检查网络后重试')
                threading.Event().wait(.25)
        except BaseException:
            try:os.killpg(process.pid,signal.SIGTERM)
            except ProcessLookupError:pass
            try:process.wait(timeout=8)
            except subprocess.TimeoutExpired:
                try:os.killpg(process.pid,signal.SIGKILL)
                except ProcessLookupError:pass
                process.wait()
            raise
        if process.returncode:
            stream.flush()
            raise RuntimeError(_failure_detail(log))


def _env(directory):
    # Do not inherit PIP_INDEX_URL, PYTHONPATH, TLS-disable flags, proxy injection,
    # or the developer PlatformIO home. Keep system trust validation enabled.
    return {'HOME':str(directory/'user-home'),'PATH':'/usr/bin:/bin:/usr/sbin:/sbin',
            'LANG':'en_US.UTF-8','PYTHONDONTWRITEBYTECODE':'1','PYTHONNOUSERSITE':'1',
            'PIP_CONFIG_FILE':os.devnull,'PIP_DISABLE_PIP_VERSION_CHECK':'1','PIP_NO_CACHE_DIR':'1',
            'PLATFORMIO_CORE_DIR':str(directory/'pio-core'),
            'PLATFORMIO_GLOBALLIB_DIR':str(directory/'global-libraries'),
            'PLATFORMIO_SETTING_ENABLE_TELEMETRY':'no','PLATFORMIO_SETTING_CHECK_PLATFORMIO_INTERVAL':'0'}


def _write_pointer(kind,directory):
    state_store.write_json(home()/(kind+'-current.json'),{'generation':directory.name},meta=False)


def _importer():
    spec=importlib.util.spec_from_file_location('inksight_font_import',ROOT/'shared/tools/import_misans.py')
    module=importlib.util.module_from_spec(spec);spec.loader.exec_module(module)
    return module


def _package_info(folder):
    for name in ['package.json','platform.json','library.json']:
        path=folder/name
        if path.is_file():
            data=json.loads(path.read_text())
            return str(data.get('name') or folder.name),str(data.get('version') or '')
    properties=folder/'library.properties'
    if properties.is_file():
        # Arduino libraries legitimately use this format rather than JSON.
        values={}
        for line in properties.read_text(encoding='utf-8').splitlines():
            if line.lstrip().startswith('#') or '=' not in line:continue
            key,value=line.split('=',1);values[key.strip()]=value.strip()
        return values.get('name') or folder.name,values.get('version','')
    return None


def _fonts_job(ident,directory):
    importer=_importer()
    charset=(ROOT/'shared/firmware/fonts_full_charset.txt').read_text()
    metadata={role:importer.validate_font(directory/(role+'.font'+suffix),label,charset)
              for role,label,suffix in json.loads((directory/'upload.json').read_text())}
    _check_cancel(ident)
    _set(ident,detail='正在生成完整面板字库，原有字库保持可用')
    # Generator subprocesses have bounded per-file timeouts. They never write
    # into the signed application or public source.
    uploads=json.loads((directory/'upload.json').read_text())
    by_role={role:directory/(role+'.font'+suffix) for role,_label,suffix in uploads}
    importer.generate(ROOT,by_role['regular'],by_role['bold'],directory/'src',checkpoint=lambda:_check_cancel(ident))
    files=_manifest(directory/'src',FONT_NAMES)
    state_store.write_json(directory/'src/manifest.json',{'files':files,'fonts':metadata},meta=False)
    _check_cancel(ident)
    _write_pointer('fonts',directory)


def _tools_job(ident,directory):
    env=_env(directory);(directory/'user-home').mkdir(mode=0o700)
    python=directory/'venv/bin/python'
    _set(ident,detail='正在创建独立 Python 工具环境')
    _command(ident,[sys.executable,'-m','venv',str(directory/'venv')],env=env,timeout=120,cwd=directory)
    lock=ROOT/'requirements-firmware-py311-macos-arm64.lock'
    if sys.version_info[:2]!=(3,11):raise RuntimeError('图形工具准备需要 App 自带 Python 3.11；源码模式请使用 3.11 环境')
    _set(ident,detail='正在从 PyPI 下载固定版本 PlatformIO，逐项校验 SHA256')
    _command(ident,[str(python),'-m','pip','install','--only-binary=:all:','--require-hashes',
        '--index-url','https://pypi.org/simple','-r',str(lock)],env=env,timeout=600,cwd=directory)
    project=directory/'project';project.mkdir()
    shutil.copy2(ROOT/'shared/firmware/platformio.ini',project/'platformio.ini')
    _set(ident,detail='正在准备 ESP32-S3 编译器、框架和固定版本依赖，首次下载可能较慢')
    _command(ident,[str(python),str(ROOT/'shared/tools/firmware_pio.py'),'pkg','install',
        '-d',str(project),'-e',TARGET],env=env,timeout=1800,cwd=directory)
    _command(ident,[str(python),str(ROOT/'shared/tools/firmware_pio.py'),'pkg','install','--global',
        '--tool','platformio/tool-scons@4.40801.0'],env=env,timeout=300,cwd=directory)
    # Record the immutable package payload digests, not volatile PIO state.
    files={};packages={}
    for base in [directory/'pio-core/platforms',directory/'pio-core/packages',project/'.pio/libdeps'/TARGET,
                 directory/'venv/lib/python3.11/site-packages']:
        if not base.is_dir():raise RuntimeError('依赖下载未完成')
        for folder in base.iterdir():
            if folder.is_file() and not folder.is_symlink():
                files[str(folder.relative_to(directory))]={'bytes':folder.stat().st_size,'sha256':_hash(folder)}
                continue
            metadata=_package_info(folder)
            if metadata:
                name,version=metadata
                if name in packages and packages[name]!=version:
                    raise RuntimeError('依赖包含同名的多个版本，拒绝启用；请重新准备')
                packages[name]=version
            for path in folder.rglob('*'):
                if path.is_symlink() and directory.resolve() not in path.resolve().parents:
                    raise RuntimeError('依赖包含越界链接，已拒绝启用')
                if path.is_file() and not path.is_symlink() and '__pycache__' not in path.parts and path.name!='.piopm':
                    files[str(path.relative_to(directory))]={'bytes':path.stat().st_size,'sha256':_hash(path)}
    required={'espressif32':'7.1.0','framework-arduinoespressif32':'3.20017.241212+sha.dcc1105b',
              'tool-esptoolpy':'2.41100.260830','toolchain-xtensa-esp32s3':'8.4.0+2021r2-patch5',
              'toolchain-riscv32-esp':'8.4.0+2021r2-patch5','tool-scons':'4.40801.0',
              'GxEPD2':'1.6.9','Adafruit GFX Library':'1.12.6','Adafruit BusIO':'1.17.4',
              'WebSockets':'2.7.3','ArduinoJson':'7.4.3','NimBLE-Arduino':'2.5.1'}
    if any(packages.get(k)!=v for k,v in required.items()):raise RuntimeError('工具版本与已验证基线不一致，未启用')
    metadata={'platformio':CORE_VERSION,'packages':packages,'files':files,
              'python_lock_sha256':_hash(lock),'sources':['https://pypi.org/simple','https://registry.platformio.org'],
              'esptool_lock_sha256':_hash(ROOT/'requirements-esptool-py311-macos-arm64.lock'),
              'licenses':'Tools downloaded by user action; not bundled or relicensed by InkSight'}
    state_store.write_json(directory/'manifest.json',metadata,meta=False)
    _check_cancel(ident);_write_pointer('tools',directory)


def _run(ident,kind,directory):
    # The job was admitted and durably queued before pause. Finish it safely;
    # the durable task barrier protects it even when pause raced this thread.
    with gate.work():
        try:
            with operation_lock():
                _set(ident,status='running')
                (_fonts_job if kind=='fonts' else _tools_job)(ident,directory)
                _set(ident,status='completed',detail='已完成并启用，未写入 USB 或发布数据')
        except Exception as error:
            message=str(error) if isinstance(error,(RuntimeError,ValueError)) else '准备失败，旧可用环境保持不变'
            cancelled=message=='cancelled'
            _set(ident,status='cancelled' if cancelled else 'failed',
                 detail='准备已取消，旧可用环境保持不变' if cancelled else message[:500])
        finally:
            with _LOCK:_WORKERS.pop(ident,None)


@critical_entry
def start(kind,uploads=None):
    if kind not in {'fonts','tools'}:raise ValueError('准备类型无效')
    if kind=='tools' and sys.version_info[:2]!=(3,11):raise RuntimeError(tools_status()['detail'])
    if not host_status()['ready']:raise RuntimeError(host_status()['detail'])
    if not storage_status()['ready']:raise RuntimeError(storage_status()['detail'])
    with _LOCK:
        if _WORKERS:raise RuntimeError('已有准备任务，请等待完成')
        ident=kind+'-'+str(int(time.time()))+'-'+os.urandom(4).hex()
        directory=home()/'generations'/ident;directory.mkdir(mode=0o700,parents=True)
        if kind=='fonts':
            rows=[]
            if not isinstance(uploads,dict) or set(uploads)!={'regular','bold'}:raise ValueError('请选择两个字体文件')
            for role,label in [('regular','常规'),('bold','粗体')]:
                name,data=uploads[role];suffix=Path(name).suffix.lower()
                if suffix not in {'.otf','.ttf'} or not 0<len(data)<=FONT_MAX:raise ValueError('字体需为不超过32MiB的独立OTF/TTF')
                path=directory/(role+'.font'+suffix);path.write_bytes(data);path.chmod(0o600)
                rows.append((role,label,suffix))
            (directory/'upload.json').write_text(json.dumps(rows))
        job=_set(ident,kind=kind,status='queued',detail='等待准备',created_at=int(time.time()))
        worker=threading.Thread(target=_run,args=(ident,kind,directory),daemon=True,name='firmware-'+ident)
        _WORKERS[ident]=worker;worker.start()
        return job


def cancel(ident):
    # Pausing denies new work, not cancellation of an admitted preparation.
    # Durable queued/running state continues to protect safe quit until the
    # worker observes this request and exits at its own safe boundary.
    with _LOCK:
        row=_read().get('jobs',{}).get(ident)
        if not row:raise KeyError(ident)
        if row.get('status') in {'queued','running'}:return _set(ident,cancel_requested=True,detail='将在安全边界取消；旧环境保持不变')
        return row
