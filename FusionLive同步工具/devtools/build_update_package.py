"""Package program files and fixed test samples without server runtime data."""
from pathlib import Path
import argparse
import json
import sys
import tempfile
import zipfile

HOME = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(HOME/'app'))
import preflight
import create_test_briefing
from wechat_test_send import latest_artifacts
from paths import check_output_directory


def main(output_dir=None):
    manifest = json.loads((HOME / 'release_manifest.json').read_text(encoding='utf-8'))
    report = preflight.run_checks(runtime=False)
    if not report['ok']:
        raise RuntimeError(report['errors'])
    names = set(manifest['files']) | {'release_manifest.json'}
    for directory in ('docs', 'templates'):
        names.update(path.relative_to(HOME).as_posix() for path in (HOME / directory).rglob('*') if path.is_file())
    if (HOME/'data/管道简报模板.xlsx').is_file():
        names.add('data/管道简报模板.xlsx')
    if (HOME / 'runtime/login.paused').is_file():
        names.add('runtime/login.paused')
    output_dir = check_output_directory(output_dir or HOME / 'releases', base_dir=HOME)
    output_dir.mkdir(parents=True, exist_ok=True)
    archive_path = output_dir / ('FusionLive_Update_' + manifest['release'] + '.zip')
    # Always build current-layout fallback samples in isolation. Do not reuse
    # old or Excel-locked samples from a developer/server runtime directory.
    with tempfile.TemporaryDirectory(prefix='fusion-demo-build-') as temporary:
        previous = create_test_briefing.HOME
        try:
            create_test_briefing.HOME = Path(temporary)
            create_test_briefing.main()
        finally:
            create_test_briefing.HOME = previous
        with zipfile.ZipFile(archive_path, 'w', zipfile.ZIP_DEFLATED) as archive:
            for name in sorted(names):
                if name.startswith('downloads/') or (name.startswith('runtime/') and name != 'runtime/login.paused'):
                    raise ValueError('Runtime data must not enter update package')
                archive.write(HOME / name, name)
            for sample in (Path(temporary) / 'logs/test_push').rglob('*'):
                if sample.is_file():
                    archive.write(sample, sample.relative_to(temporary).as_posix())
    with tempfile.TemporaryDirectory(prefix='fusion-release-check-') as temporary:
        target = Path(temporary)
        with zipfile.ZipFile(archive_path) as archive:
            assert archive.testzip() is None
            assert all(not Path(name).is_absolute() and '..' not in Path(name).parts for name in archive.namelist())
            archive.extractall(target)
        result = preflight.run_checks(target, runtime=False)
        assert result['ok'], result['errors']
        text, report = latest_artifacts(target)
        assert text['fallback'] and report['fallback'] and report['demo']
        assert all(Path(image['path']).is_relative_to(target) for image in report['images'])
        print('package integrity and fixed fallback selection verified after extraction')
    print(ascii(str(archive_path)))


if __name__ == '__main__':
    parser = argparse.ArgumentParser()
    parser.add_argument('--output-dir', type=Path)
    main(parser.parse_args().output_dir)
