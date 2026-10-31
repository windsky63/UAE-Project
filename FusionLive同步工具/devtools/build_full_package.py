"""Build a standalone source distribution with templates, tools and offline checks."""
from pathlib import Path
import argparse
import json
import shutil
import subprocess
import sys
import tempfile
import zipfile

HOME = Path(__file__).resolve().parents[1]
sys.path[:0] = [str(HOME / 'app'), str(HOME / 'devtools')]
import preflight
import briefing
import create_test_briefing
from wechat_test_send import latest_artifacts


def main(output_dir=None):
    manifest = json.loads((HOME / 'release_manifest.json').read_text(encoding='utf-8'))
    check = preflight.run_checks(runtime=False)
    if not check['ok']:
        raise RuntimeError(check['errors'])
    output = Path(output_dir or HOME / 'releases')
    output.mkdir(parents=True, exist_ok=True)
    archive_path = output / ('FusionLive_Full_' + manifest['release'] + '.zip')
    with tempfile.TemporaryDirectory(prefix='fusion-full-build-') as temporary:
        root = Path(temporary) / HOME.name
        root.mkdir()
        names = set(manifest['files']) | {'release_manifest.json'}
        for directory in ('app', 'wechat_push', 'maintenance', 'docs', 'devtools', 'tests'):
            for source in (HOME / directory).rglob('*'):
                if (source.is_file() and '__pycache__' not in source.parts and
                        '.pytest_cache' not in source.parts and source.suffix not in ('.pyc', '.pyo')):
                    names.add(source.relative_to(HOME).as_posix())
        for name in sorted(names):
            target = root / name
            target.parent.mkdir(parents=True, exist_ok=True)
            shutil.copy2(HOME / name, target)
        for directory in ('runtime', 'downloads', 'history', 'exports', 'templates'):
            (root / directory).mkdir(exist_ok=True)
        template = root / 'templates' / '管道简报模板.xlsx'
        briefing.write_layout_template(template)
        briefing.write_template(root / 'templates' / '简报数据补充模板.xlsx', {('项目编号', '单元编号')})
        previous = create_test_briefing.HOME
        try:
            create_test_briefing.HOME = root
            create_test_briefing.main()
        finally:
            create_test_briefing.HOME = previous
        # The generated sample archive is redundant: the complete samples are included directly.
        (root / 'releases' / '微信推送测试文件.zip').unlink()
        (root / 'releases').rmdir()
        readme = '''FusionLive同步工具 完整程序包
版本：{version}

这是可独立解压使用的完整程序包，包含全部程序源码、启动入口、依赖清单和安装脚本、
简报模板、模拟测试样例、迁移工具、开发打包工具和回归测试。
本包需要电脑已安装Python 3.10或更高版本（含pip及Tcl/Tk），不内置Python和第三方依赖。

新电脑安装：
1. 将整个FusionLive同步工具文件夹解压到较短的本地路径。
2. 运行maintenance/安装依赖.cmd，需要网络下载requirements.txt中的依赖。
3. 运行maintenance/检查程序完整性.cmd。
4. 双击启动正式下载.cmd，填写并保存接收人及定时设置。

已有数据迁移：
1. 停止旧工具，保留原runtime、downloads、history、logs和自定义配置。
2. 将本完整包中的程序文件合并覆盖到原工具目录；不要删除原数据目录。
3. 先运行预览路径迁移.cmd，再运行迁移已有文件.cmd，完成后重新启动。
4. 具体规则见docs/平台目录与已有数据迁移说明.txt。

目录：
app、wechat_push：全部运行代码；maintenance：依赖安装和维护入口。
templates：简报版式和补充数据模板；logs/test_push：明确标记的模拟发送测试样例。
devtools、tests：开发打包工具和测试源码，不是启动依赖。
runtime、downloads、history、exports：新安装的空数据目录，运行时自动写入。

本包不携带开发电脑的业务数据库、下载文件、分析缓存、日常日志或个人推送配置。
覆盖已有目录时不会通过本包主动删除这些数据；请保留它们以便续传和迁移。
'''.format(version=manifest['release'])
        (root / '完整包使用说明.txt').write_text(readme, encoding='utf-8-sig')
        with zipfile.ZipFile(archive_path, 'w', zipfile.ZIP_DEFLATED) as archive:
            for path in sorted(root.rglob('*')):
                archive.write(path, path.relative_to(root.parent).as_posix() + ('/' if path.is_dir() else ''))
    with tempfile.TemporaryDirectory(prefix='fusion-full-check-') as temporary:
        with zipfile.ZipFile(archive_path) as archive:
            if archive.testzip() is not None:
                raise RuntimeError('ZIP integrity failure')
            archive.extractall(temporary)
        root = Path(temporary) / HOME.name
        result = preflight.run_checks(root, runtime=False)
        if not result['ok']:
            raise RuntimeError(result['errors'])
        text, report = latest_artifacts(root)
        assert text['fallback'] and report['demo'] and report['images']
        for name in ('templates/管道简报模板.xlsx', 'templates/简报数据补充模板.xlsx',
                     '迁移已有文件.cmd', '预览路径迁移.cmd', 'devtools/build_full_package.py'):
            assert (root / name).is_file(), name
        assert not list((root / 'runtime').iterdir())
        assert not list((root / 'downloads').iterdir())
        assert not list(root.rglob('*.sqlite3'))
        command = subprocess.run([sys.executable, '-B', str(root / 'main.py'), 'scheduler', '--check'],
            cwd=root, capture_output=True, timeout=20)
        assert command.returncode == 0, command.stderr
    print('Full package: extracted integrity, templates, samples and launcher verified.')
    print(ascii(str(archive_path)))
    print('Archive bytes:', archive_path.stat().st_size)


if __name__ == '__main__':
    parser = argparse.ArgumentParser()
    parser.add_argument('--output-dir', type=Path)
    main(parser.parse_args().output_dir)
