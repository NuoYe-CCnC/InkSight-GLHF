"""Read-only previews of exact, GET-verified successful publications.

No account/market/news cache composition. The publisher atomically stores the
complete sanitized document only after upload and SHA readback succeed. This
receipt is not a device display acknowledgement. Frames use their own clock.
"""
from __future__ import annotations

import asyncio
import copy
import hashlib
import hmac
import importlib.util
import json
import os
import threading
import time
from pathlib import Path

from PIL import Image

from .config_store import get_user_devices

_ROOT = Path(__file__).resolve().parents[3]
_RENDERER_PATH = _ROOT / 'shared/tools/misans_panel_render.py'
STATE_FILE = _ROOT / 'shared/tools/.cloud_publish_state.json'
_DEVICE_ID_SECRET = os.urandom(32)
_RENDERER = None
_FONTS = None
_FONT_KEY = None
_RENDER_LOCK = threading.RLock()
FONT_NAMES = [f'misans_{style}_{size}.h' for style, sizes in
              (('reg', (16, 21, 24, 30, 62, 82)), ('bold', (24, 30, 62, 82)))
              for size in sizes]


class PreviewError(RuntimeError):
    def __init__(self, code: str, message: str, status: int = 503):
        super().__init__(message)
        self.code, self.status = code, status


def _renderer():
    global _RENDERER
    if _RENDERER is None:
        spec = importlib.util.spec_from_file_location('inksight_misans_panel_render', _RENDERER_PATH)
        if spec is None or spec.loader is None:
            raise PreviewError('renderer_missing', '本机渲染器缺失，请重新安装完整 App。')
        module = importlib.util.module_from_spec(spec)
        try:
            spec.loader.exec_module(module)
        except (OSError, ImportError):
            raise PreviewError('renderer_missing', '本机渲染器或依赖缺失，请重新安装完整 App。') from None
        _RENDERER = module
    return _RENDERER


def _fonts(renderer):
    global _FONTS, _FONT_KEY
    from .firmware_setup import font_source
    imported=font_source()
    if imported is not None:
        renderer.FONT_DIR=imported/'fonts_gen'
    paths = [renderer.FONT_DIR / name for name in FONT_NAMES]
    try:
        key = tuple((str(path), path.stat().st_mtime_ns, path.stat().st_size) for path in paths)
    except OSError:
        raise PreviewError('fonts_missing',
            '缺少本机 MiSans 字库。请按项目字体说明准备有授权的字体，使用固件字体生成工具生成 '
            'fonts_gen 字库后重试；公开 App 不附带私有字库，也不会自动下载或替换字体。') from None
    if key != _FONT_KEY:
        try:
            fonts = renderer.load_fonts()
        except (OSError, ValueError, AssertionError):
            raise PreviewError('fonts_invalid', '本机 MiSans 字库损坏或版本不兼容，请重新生成固件字库。') from None
        _FONTS, _FONT_KEY = fonts, key
        if hasattr(renderer, '_F21_CACHE'):
            renderer._F21_CACHE.clear()
    return _FONTS


def _device_id(user_id: int, mac: str) -> str:
    body = f'{user_id}:{mac.upper()}'.encode()
    return 'screen-' + hmac.new(_DEVICE_ID_SECRET, body, hashlib.sha256).hexdigest()[:18]


def _mac(value: str) -> str:
    return str(value).upper().replace(':', '').replace('-', '')


async def _operator_devices(user_id: int) -> list[dict]:
    from .desktop_browser import enabled
    if user_id == 0 and enabled():
        from .db import get_main_db
        db = await get_main_db()
        cursor = await db.execute('''SELECT dm.mac, MAX(dm.nickname),
            (SELECT MAX(dh.created_at) FROM device_heartbeats dh WHERE dh.mac = dm.mac)
            FROM device_memberships dm WHERE dm.status = 'active' GROUP BY dm.mac''')
        rows=[{'mac': row[0], 'nickname': row[1], 'last_seen': row[2]}
              for row in await cursor.fetchall()]
        from . import state_store
        binding,error=state_store.read_json(state_store.state_path('desktop_device_binding.json'))
        if not error and isinstance(binding,dict) and len(_mac(binding.get('mac') or ''))==12:
            if not any(_mac(row['mac'])==_mac(binding['mac']) for row in rows):rows.append(binding)
        return rows
    return await get_user_devices(user_id)


def _publications() -> dict:
    try:
        if STATE_FILE.stat().st_size > 8_000_000:
            raise ValueError('oversize')
        value = json.loads(STATE_FILE.read_text(encoding='utf-8'))
        rows = value.get('_desktop_publications')
        if not isinstance(rows, dict):
            return {}
        return {_mac(key): row for key, row in rows.items() if isinstance(row, dict)}
    except FileNotFoundError:
        return {}
    except (OSError, ValueError, TypeError, AttributeError):
        raise PreviewError('publication_unreadable', '无法读取发布记录。请检查本机磁盘和文件权限，不会改用测试数据。') from None


def _document(row: dict) -> dict:
    document = row.get('document')
    if not isinstance(document, dict):
        raise PreviewError('snapshot_missing', '这台设备尚无完整发布快照。旧版本只记录了发布时间，请等待发布器下一次正常成功发布；预览不会主动发布。', 404)
    stamp = document.get('ts')
    screen = document.get('screen')
    if (isinstance(stamp, bool) or not isinstance(stamp, (int, float)) or stamp < 1_700_000_000
            or not isinstance(screen, dict) or not isinstance(screen.get('pages'), dict)
            or not screen['pages']):
        raise PreviewError('snapshot_invalid', '发布快照格式不完整，不生成可能误导的画面。')
    digest = hashlib.sha256(json.dumps(document, ensure_ascii=False).encode()).hexdigest()
    if (digest != row.get('document_sha256') or row.get('last_success_at') != stamp
            or row.get('publish_id') != (screen.get('publish_id') or screen.get('payload_id'))):
        raise PreviewError('snapshot_invalid', '发布快照校验不一致，请等待下一次正常成功发布。')
    return copy.deepcopy(document)


async def list_devices(user_id: int) -> list[dict]:
    rows, publications = await _operator_devices(user_id), _publications()
    devices = []
    seen = set()
    for row in rows:
        mac = str(row.get('mac') or '').upper()
        if not mac or _mac(mac) in seen:
            continue
        seen.add(_mac(mac))
        record = publications.get(_mac(mac), {})
        try:
            _document(record)
            state = 'published'
        except PreviewError as exc:
            state = exc.code if record else 'unpublished'
        devices.append({'id': _device_id(user_id, mac),
            'label': str(row.get('nickname') or f'绑定设备 {len(devices)+1}')[:80],
            'last_seen': row.get('last_seen'), 'published_at': record.get('last_success_at'),
            'state': state, 'has_snapshot': state == 'published',
            'publication_kind': record.get('publication_kind','regular')})
    devices.sort(key=lambda item: (item['has_snapshot'], item.get('published_at') or 0), reverse=True)
    return devices


async def _resolve_device(user_id: int, opaque_id: str | None) -> str:
    if not opaque_id:
        choices = await list_devices(user_id)
        if not choices:
            raise PreviewError('no_devices', '尚未绑定设备。请先在 App 中选择实际设备，再等待正常发布。', 404)
        opaque_id = choices[0]['id']
    for row in await _operator_devices(user_id):
        mac = str(row.get('mac') or '').upper()
        if mac and hmac.compare_digest(_device_id(user_id, mac), opaque_id):
            return mac
    raise PreviewError('device_unavailable', '该来源已失效或不属于当前用户，请刷新设备列表。', 404)


async def build_document(user_id: int, device_id: str | None = None) -> tuple[dict, dict]:
    mac = await _resolve_device(user_id, device_id)
    record = _publications().get(_mac(mac))
    if not record:
        raise PreviewError('unpublished', '这台绑定设备没有成功发布记录，不会借用其他设备的数据。', 404)
    doc = _document(record)
    return doc, {'device_bound': True, 'device_id': _device_id(user_id, mac),
        'data_policy': 'verified-publication-snapshot', 'published_at': record['last_success_at'],
        'document_timestamp': doc['ts'], 'render_at': doc['ts'],
        'snapshot_sha256': record['document_sha256'], 'device_display_confirmed': False,
        'publication_kind': record.get('publication_kind','regular'),
        'current_page': None,
        'default_page': ((doc['screen'].get('device_policy') or {}).get('page_switch') or {}).get('default_page', 'ai'),
        'stale': time.time() - doc['ts'] > 1800}


def _render_document(document: dict, metadata: dict, page: str) -> tuple[Image.Image, dict]:
    with _RENDER_LOCK:
        renderer = _renderer()
        fonts = _fonts(renderer)
        panel = renderer.Panel()
        if page not in (document.get('screen', {}).get('pages') or {}):
            raise PreviewError('page_missing', '这份发布快照没有所选页面，不使用示例值代替。', 404)
        draw = renderer.render_ai_panel if page == 'ai' else renderer.render_news_gold_panel
        draw(panel, fonts, document, int(metadata['render_at']))
        if panel.missing:
            raise PreviewError('glyphs_missing', '当前字库缺少快照中的文字，无法完整预览。请重新生成与固件一致的 MiSans 字库。')
        image = Image.frombytes('1', (renderer.W, renderer.H), bytes(panel.fb))
        return image, {**metadata, 'page': page}


async def render(user_id: int, *, device_id: str | None = None,
                 page: str = 'ai', source: str = 'published') -> tuple[Image.Image, dict]:
    if page not in {'ai', 'news_gold'} or source not in {'published', 'unbound'}:
        raise ValueError('unsupported preview page or source')
    if source == 'unbound':
        now = int(time.time())
        doc = {'ts': now, 'screen': {'pages': {'ai': {'membership_display': '套餐信息未知'}, 'news_gold': {}}}}
        meta = {'device_bound': False, 'data_policy': 'unbound-diagnostic', 'published_at': None,
                'document_timestamp': 0, 'render_at': now, 'device_display_confirmed': False, 'stale': False}
    else:
        doc, meta = await build_document(user_id, device_id)
    return await asyncio.to_thread(_render_document, doc, meta, page)
