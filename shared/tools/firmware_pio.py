#!/usr/bin/env python3
"""Fixed PlatformIO entry with fail-closed archive traversal guards.

Only invoked by firmware preparation/build, never accepts a remote script.
"""
import os
from pathlib import Path
import runpy
import subprocess
import sys
import tarfile
import zipfile


def checked_path(base, name):
    root = Path(base).resolve()
    path = Path(name)
    if path.is_absolute() or '..' in path.parts or '\\' in str(name):
        raise ValueError('unsafe dependency archive path')
    destination = (root / path).resolve()
    if destination != root and root not in destination.parents:
        raise ValueError('dependency archive escapes destination')
    return destination


_tar_extract = tarfile.TarFile.extract
_tar_extractall = tarfile.TarFile.extractall
_zip_extract = zipfile.ZipFile.extract
_zip_extractall = zipfile.ZipFile.extractall


def check_tar_member(self, member, path):
    item = self.getmember(member) if isinstance(member, str) else member
    destination = checked_path(path or os.curdir, item.name)
    if item.isdev() or item.isfifo():
        raise ValueError('special dependency archive entry rejected')
    if item.issym() or item.islnk():
        link_base = destination.parent if item.issym() else Path(path or os.curdir)
        target = (link_base / item.linkname).resolve()
        root = Path(path or os.curdir).resolve()
        if target != root and root not in target.parents:
            raise ValueError('dependency archive link escapes destination')


def tar_extract(self, member, path='', **kwargs):
    check_tar_member(self, member, path)
    return _tar_extract(self, member, path, **kwargs)


def tar_extractall(self, path='.', members=None, **kwargs):
    members=list(self.getmembers() if members is None else members)
    for member in members:check_tar_member(self,member,path)
    return _tar_extractall(self,path,members,**kwargs)


def check_zip_member(self, member, path):
    item = self.getinfo(member) if isinstance(member, str) else member
    checked_path(path or os.curdir, item.filename)
    if (item.external_attr >> 16) & 0o170000 == 0o120000:
        raise ValueError('dependency archive symbolic link rejected')


def zip_extract(self, member, path=None, pwd=None):
    check_zip_member(self,member,path)
    return _zip_extract(self, member, path, pwd)


def zip_extractall(self,path=None,members=None,pwd=None):
    members=list(self.namelist() if members is None else members)
    for member in members:check_zip_member(self,member,path)
    return _zip_extractall(self,path,members,pwd)


def controlled_pkg_script(manager,pkg,event):
    """Replace esptool's loose nested pip download with our hashed lock.

    No arbitrary package lifecycle shell is admitted by this fixed target.
    The original downloaded tool files/licenses are left unchanged.
    """
    manifest=manager.load_manifest(pkg)
    command=(manifest.get('scripts') or {}).get(event)
    if not command:return
    expected=Path(os.environ['PLATFORMIO_CORE_DIR'])/'packages/tool-esptoolpy'
    if (event!='postinstall' or manifest.get('name')!='tool-esptoolpy'
        or manifest.get('version')!='2.41100.260830'
        or command!='package-postinstall.py' or Path(pkg.path).resolve()!=expected.resolve()
        or expected.is_symlink()):
        raise RuntimeError('Unapproved dependency lifecycle script; preparation stopped')
    lock=Path(__file__).resolve().parents[2]/'requirements-esptool-py311-macos-arm64.lock'
    subprocess.run([sys.executable,'-m','pip','install','--no-compile','--only-binary=:all:',
        '--require-hashes','--index-url','https://pypi.org/simple','--target',str(expected/'_contrib'),
        '-r',str(lock)],cwd=pkg.path,env=os.environ,check=True)


if __name__ == '__main__':
    tarfile.TarFile.extract = tar_extract
    tarfile.TarFile.extractall = tar_extractall
    zipfile.ZipFile.extract = zip_extract
    zipfile.ZipFile.extractall = zip_extractall
    from platformio.package.manager.base import BasePackageManager
    BasePackageManager.call_pkg_script = controlled_pkg_script
    runpy.run_module('platformio', run_name='__main__')
