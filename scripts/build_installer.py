"""NSIS wraps an already verified folder. No runtime downloads or system install."""
import argparse
import hashlib
import json
import subprocess
from pathlib import Path, PureWindowsPath


def uninstall_listing(files):
    """Only shipped files and bytecode derived from shipped .py files are owned.

    Never recursively delete an installation: clients may save a data project
    inside it. Check ancestors before any deletion, including junctions.
    """
    directories = set()
    caches = set()
    deletions = []
    for relative in files:
        # These are Windows target paths, even when CI checks the builder on
        # Linux. Reject drives/root-relative paths and NTFS alternate streams.
        path = PureWindowsPath(relative)
        if path.drive or path.root or '..' in path.parts or any(c in relative for c in ':$\"\r\n*?'):
            raise ValueError('Unsafe uninstall member')
        native = str(path).replace('/', '\\')
        deletions.append('Delete "$INSTDIR\\' + native + '"')
        directories.update(str(p).replace('/', '\\') for p in path.parents if str(p) != '.')
        if path.suffix == '.py':
            cache = str(path.parent / '__pycache__').replace('/', '\\')
            caches.add(cache)
            deletions.append('Delete "$INSTDIR\\' + cache + '\\' + path.stem + '.*.pyc"')
    directories.update(caches)
    directories.add('logs')
    checks = ['Push "$INSTDIR"', 'Call un.CheckDirectory']
    for directory in sorted(directories):
        checks += ['Push "$INSTDIR\\' + directory + '"', 'Call un.CheckDirectory']
    deletions += ['Delete "$INSTDIR\\logs\\annotator.log"', 'Delete "$INSTDIR\\COWMATA.update-lock"']
    checks += ['ClearErrors'] + deletions + ['Call un.CheckDeleteErrors']
    checks += ['RMDir "$INSTDIR\\' + p + '"' for p in sorted(
        directories, key=lambda x: (x.count('\\'), len(x)), reverse=True)]
    # Non-empty directories contain unowned content and are deliberately kept.
    return checks


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument('--package', type=Path, required=True)
    parser.add_argument('--compiler', type=Path, required=True)
    parser.add_argument('--out', type=Path, required=True)
    parser.add_argument('--version', required=True)
    parser.add_argument('--compression', choices=('zlib', 'lzma'), default='lzma',
                        help='Independent LZMA blocks reduce size without solid temporary extraction; zlib is the fast baseline')
    args = parser.parse_args()
    root, output = args.package.resolve(), args.out.resolve()
    if output.exists():
        raise ValueError('Installer output exists')
    if not all(c.isalnum() or c in '.-' for c in args.version):
        raise ValueError('Invalid release version')
    manifest = json.loads((root / 'package-manifest.json').read_text(encoding='utf-8'))
    files = [row['path'] for row in manifest['files']] + ['package-manifest.json']
    for row in manifest['files']:
        path = root / row['path']
        if not path.resolve().is_relative_to(root):
            raise ValueError('Unsafe manifest member')
        with path.open('rb') as stream:
            if hashlib.file_digest(stream, 'sha256').hexdigest() != row['sha256']:
                raise ValueError('Portable input changed: ' + row['path'])
    install = []
    previous_parent = None
    for relative in files:
        path = root / relative
        if not path.resolve().is_relative_to(root) or any(c in relative for c in '$"\r\n'):
            raise ValueError('Unsafe package member')
        # NSIS cannot preserve arbitrarily long Windows destination paths.
        if len(relative) > 155:
            raise ValueError('Package member too long for supported install path: ' + relative)
        if any(c in str(path) for c in '$"\r\n'):
            raise ValueError('Unsafe NSIS input path')
        parent = str(Path(relative).parent).replace('/', '\\')
        if parent != previous_parent:
            install.append('SetOutPath "$INSTDIR' + ('\\' + parent if parent != '.' else '') + '"')
            previous_parent = parent
        install.append('File "' + str(path) + '"')
    lines = uninstall_listing(files)
    listing = output.with_suffix('.uninstall.nsh')
    with listing.open('x', encoding='utf-8-sig') as stream:
        stream.write('\n'.join(lines) + '\n')
    install_list = output.with_suffix('.install.nsh')
    with install_list.open('x', encoding='utf-8-sig') as stream:
        stream.write('\n'.join(install) + '\n')
    script = Path(__file__).resolve().parents[1] / 'packaging/installer.nsi'
    result = subprocess.run([str(args.compiler.resolve()), '/INPUTCHARSET', 'UTF8', '/WX', '/V2',
                             '/DCOMPRESSION=' + args.compression, '/DVERSION=' + args.version,
                             '/DPACKAGE=' + str(root), '/DOUTPUT=' + str(output),
                             '/DUNINSTALL_LIST=' + str(listing), '/DINSTALL_LIST=' + str(install_list), str(script)], creationflags=0x08000000)
    if result.returncode:
        raise SystemExit(result.returncode)
    with output.open('rb') as stream:
        digest = hashlib.file_digest(stream, 'sha256').hexdigest()
    with output.with_suffix('.exe.sha256').open('x', encoding='utf-8') as stream:
        stream.write(digest + '  ' + output.name + '\n')
    descriptor = output.parent / 'cowmata-update.json'
    with descriptor.open('x', encoding='utf-8') as stream:
        json.dump({'schema': 1, 'product': 'cowmata-annotator', 'version': args.version,
                   'installer': output.name, 'size': output.stat().st_size, 'sha256': digest,
                   'package_sha256': hashlib.sha256((root / 'package-manifest.json').read_bytes()).hexdigest(),
                   'unpacked_size': sum(row['size'] for row in manifest['files'])}, stream, indent=2)
    print(json.dumps({'file': str(output), 'bytes': output.stat().st_size, 'sha256': digest}), flush=True)


if __name__ == '__main__':
    main()
