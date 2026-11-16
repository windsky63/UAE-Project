"""Build or verify a complete deployment, including all tools and resources."""
import argparse
import hashlib
import json
from pathlib import Path, PurePosixPath
import zipfile

import build_deployment
from project_integrity import MANIFEST


def build():
    return build_deployment.build(overlay=True)


def verify_archive(path, target_root=None):
    path = Path(path)
    with zipfile.ZipFile(path) as archive:
        names = archive.namelist()
        assert len(names) == len(set(names)), 'Duplicate ZIP entries'
        assert archive.testzip() is None, 'ZIP CRC failure'
        manifest = json.loads(archive.read(MANIFEST))
        assert manifest['existing_server_overlay']
        assert not manifest['runtime_data_included']
        assert manifest['full_deployment']
        assert set(names) == set(manifest['files']) | {MANIFEST}
        for name, entry in manifest['files'].items():
            parts = PurePosixPath(name).parts
            assert not PurePosixPath(name).is_absolute() and '..' not in parts and ':' not in name and chr(92) not in name
            assert parts[0] not in {'releases', 'backups', '其他'}
            if len(parts) > 3 and parts[0] == 'tools':
                assert parts[2] not in {'runtime', 'logs', 'downloads', 'history', 'exports', 'releases', 'backups', '待同步'}
            data = archive.read(name)
            assert len(data) == entry['bytes'], name
            assert hashlib.sha256(data).hexdigest() == entry['sha256'], name
        build_deployment.verify_sync_startup({name: archive.read(name) for name in names})
    checksum = hashlib.sha256(path.read_bytes()).hexdigest()
    sidecar = path.with_suffix('.zip.sha256')
    if sidecar.is_file():
        assert sidecar.read_text(encoding='utf-8').split()[0] == checksum
    # The current archive contains every runtime module; no old partial-update
    # base-file matching or migration instructions are needed.
    return dict(archive=str(path.resolve()), release=manifest['release'],
                files=len(names), bytes=path.stat().st_size, sha256=checksum, verified=True,
                sync_startup_integrity_verified=True, full_deployment=True)


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--verify', type=Path)
    parser.add_argument('--target-root', type=Path, help='Compatibility option; the overlay contains all runtime modules')
    args = parser.parse_args()
    if args.target_root and not args.verify:
        parser.error('--target-root requires --verify')
    if args.verify:
        print(json.dumps(verify_archive(args.verify, args.target_root), ensure_ascii=False))
    else:
        build()


if __name__ == '__main__':
    main()
