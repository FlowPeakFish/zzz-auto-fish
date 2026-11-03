"""Build the Windows single-file distribution using the current interpreter."""
import hashlib
import os
from pathlib import Path
import shutil
import subprocess
import sys


def main():
    root = Path(__file__).resolve().parents[1]
    if sys.platform != 'win32':
        raise SystemExit('Build the Windows EXE on Windows.')
    os.environ['PYINSTALLER_CONFIG_DIR'] = str(root / 'envs' / '.cache' / 'pyinstaller')
    subprocess.run([sys.executable, '-B', str(root / 'fish.py'), '--self-check'], check=True)
    subprocess.run([
        sys.executable, '-m', 'PyInstaller', '--noconfirm', '--clean', '--onefile',
        '--console', '--uac-admin', '--name', 'ZZZAutoFish',
        '--add-data', str(root / 'image') + os.pathsep + 'image',
        '--add-data', str(root / 'LICENSE') + os.pathsep + '.',
        '--add-data', str(root / 'NOTICE.md') + os.pathsep + '.',
        '--distpath', str(root / 'dist'), '--workpath', str(root / 'build'),
        '--specpath', str(root / 'build'), str(root / 'fish.py'),
    ], cwd=root, check=True)
    executable = root / 'dist' / 'ZZZAutoFish.exe'
    with executable.open('rb') as stream:
        digest = hashlib.file_digest(stream, 'sha256').hexdigest()
    (root / 'dist' / 'ZZZAutoFish.exe.sha256').write_text(digest + '  ZZZAutoFish.exe\n', encoding='ascii')
    for name in ('README.md', 'LICENSE', 'NOTICE.md'):
        shutil.copy2(root / name, root / 'dist' / name)
    shutil.copytree(root / 'docs', root / 'dist' / 'docs', dirs_exist_ok=True)
    print('Built:', executable)


if __name__ == '__main__':
    main()
