#!/usr/bin/env python3
"""Build an offline, universal2 .app, ZIP and DMG without user/runtime data."""
import argparse
import hashlib
import importlib.metadata
import os
import plistlib
import shutil
import subprocess
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
VERSION = '1.1.0'

def run(*args, **kwargs):
    subprocess.run([str(x) for x in args], check=True, **kwargs)


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument('--work-dir', required=True, type=Path)
    parser.add_argument('--output-dir', required=True, type=Path)
    args = parser.parse_args()
    work = args.work_dir.resolve()
    output = args.output_dir.resolve()
    work.mkdir(parents=True, exist_ok=True)
    output.mkdir(parents=True, exist_ok=True)
    import certifi
    certs = work/'certs'
    certs.mkdir(exist_ok=True)
    shutil.copy2(certifi.where(), certs/'cacert.pem')
    run(sys.executable, '-m', 'PyInstaller', '--noconfirm', '--clean', '--onedir', '--console',
        '--target-arch', 'universal2', '--name', 'sjtu-mail-engine', '--distpath', work/'engine-dist',
        '--workpath', work/'engine-build', '--specpath', work,
        '--add-data', str(ROOT/'static')+':static', '--add-data', str(certs)+':certs',
        '--paths', ROOT, ROOT/'app.py', cwd=ROOT)
    for arch in ('arm64', 'x86_64'):
        run('xcrun', 'swiftc', '-swift-version', '5', '-O', '-target', arch+'-apple-macosx13.0',
            '-framework', 'AppKit', ROOT/'macos/Launcher.swift', '-o', work/('launcher-'+arch))
    app = output/'交邮.app'
    if app.exists():
        if not (app/'Contents/Info.plist').exists():
            raise RuntimeError('Refusing to replace an unknown directory')
        shutil.rmtree(app)
    contents = app/'Contents'
    resources = contents/'Resources'
    executable = contents/'MacOS'
    resources.mkdir(parents=True)
    executable.mkdir()
    run('lipo', '-create', work/'launcher-arm64', work/'launcher-x86_64', '-output', executable/'交邮')
    shutil.copytree(work/'engine-dist/sjtu-mail-engine', resources/'engine', symlinks=True)
    shutil.copy2(ROOT/'macos/Mac应用使用说明.txt', resources/'Mac应用使用说明.txt')
    run('xcrun', 'swiftc', ROOT/'macos/GenerateIcon.swift', '-o', work/'generate-icon')
    run(work/'generate-icon', work/'MailDesk.iconset')
    run('iconutil', '-c', 'icns', work/'MailDesk.iconset', '-o', resources/'MailDesk.icns')
    info = {
        'CFBundleName': '交邮', 'CFBundleDisplayName': '交邮',
        'CFBundleIdentifier': 'io.github.turtleneck1109-design.sjtumaildesk',
        'CFBundleExecutable': '交邮', 'CFBundlePackageType': 'APPL',
        'CFBundleShortVersionString': VERSION, 'CFBundleVersion': VERSION,
        'CFBundleIconFile': 'MailDesk.icns', 'LSMinimumSystemVersion': '13.0',
        'NSHighResolutionCapable': True,
        'NSHumanReadableCopyright': '交邮 · SJTU Mail Desk',
    }
    with (contents/'Info.plist').open('wb') as file:
        plistlib.dump(info, file)
    notices = resources/'ThirdPartyNotices'
    notices.mkdir()
    for package in ('pyinstaller', 'certifi'):
        dist = importlib.metadata.distribution(package)
        for file in dist.files or []:
            if any(part.lower().startswith(('license', 'copying')) for part in file.parts) and Path(dist.locate_file(file)).is_file():
                shutil.copy2(dist.locate_file(file), notices/(package+'-'+file.name))
    for license_file in (ROOT/'macos/licenses').glob('*.txt'):
        shutil.copy2(license_file, notices/license_file.name)
    python_license = Path(sys.base_prefix)/'Resources/English.lproj/License.rtf'
    if python_license.exists():
        shutil.copy2(python_license, notices/'Python-License.rtf')
    else:
        candidate = Path(sys.base_prefix)/'lib'/('python'+sys.version[:4])/'LICENSE.txt'
        if candidate.exists(): shutil.copy2(candidate, notices/'Python-LICENSE.txt')
    (notices/'说明.txt').write_text(
        '本应用内置 Python 标准库及其二进制依赖，使用 PyInstaller bootloader（GPL 许可附分发例外）。\n'
        'TLS 根证书由 certifi 提供（Mozilla Public License 2.0）。\n'
        'Python: https://docs.python.org/3/license.html\n'
        'PyInstaller: https://pyinstaller.org/en/stable/license.html\n'
        'certifi: https://github.com/certifi/python-certifi\n', encoding='utf-8')
    # Runtime files were ad-hoc signed by PyInstaller. Seal the native bundle last.
    run('codesign', '--force', '--deep', '--sign', '-', '--timestamp=none', app)
    run('codesign', '--verify', '--deep', '--strict', '--verbose=2', app)
    archive = output/'SJTU-Mail-v1.1-universal2.zip'
    if archive.exists(): archive.unlink()
    run('ditto', '-c', '-k', '--sequesterRsrc', '--keepParent', app, archive)
    staging = work/'dmg-staging'
    if staging.exists(): shutil.rmtree(staging)
    staging.mkdir()
    # ditto preserves framework symlinks and code signatures.
    run('ditto', app, staging/'交邮.app')
    (staging/'Applications').symlink_to('/Applications')
    shutil.copy2(ROOT/'macos/Mac应用使用说明.txt', staging/'安装与使用说明.txt')
    image = output/'SJTU-Mail-v1.1-universal2.dmg'
    run('hdiutil', 'create', '-volname', '交邮 v1.1', '-srcfolder', staging, '-ov', '-format', 'UDZO', image)
    checksum = output/'SJTU-Mail-v1.1-SHA256.txt'
    checksum.write_text(''.join(hashlib.sha256(p.read_bytes()).hexdigest()+'  '+p.name+'\n' for p in (archive, image)))
    print('Built:', app, archive, image, checksum, sep='\n', flush=True)

if __name__ == '__main__':
    main()
