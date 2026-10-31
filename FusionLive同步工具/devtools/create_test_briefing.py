"""Create portable, visibly simulated send-test artifacts. Never sends or downloads."""
import hashlib
from pathlib import Path
import sys
import zipfile
from datetime import datetime

HOME = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(HOME/'app'))
from briefing import LOCAL, METRICS, atomic_json, create_workbook, render
from openpyxl import load_workbook


def main():
    at = datetime.now(LOCAL)
    day = at.date().isoformat()
    folder = HOME / 'logs' / 'test_push'
    folder.mkdir(parents=True, exist_ok=True)
    key = ('模拟项目', 'H4-262')
    groups = {}
    for source, values in (
        ('单线图', {'管线数': 12, '图纸数': 20}),
        ('PCF', {'管线数': 12, '焊缝数': 48, '寸口量': 186,
                 '预制焊缝数': 30, '预制寸口量': 120, '安装焊缝数': 18, '安装寸口量': 66}),
        ('SHA', {'图纸数': 20}),
    ):
        groups[(*key, source)] = {**dict.fromkeys(METRICS), **values,
                                  '依据': '人工构造的发送测试样例，不是真实业务数据', '状态': '模拟测试数据'}
    workbook = folder / '管道简报-模拟测试.xlsx'
    file_rows = [dict(project=key[0], unit=key[1], origin=f'模拟/{kind}/{i}', kind=kind,
                      basis='人工构造模拟数据', state='模拟')
                 for kind, count in (('PCF', 10), ('IDF', 0), ('单线图', 20), ('SHA', 2)) for i in range(count)]
    create_workbook(workbook, groups, [], '管道资料同步简报 · 模拟测试', day,
                    dict(file_inventory=file_rows, files=[], ledger_inventory=[], welds=[]))
    book = load_workbook(workbook)
    sheet = book['简报']
    sheet.cell(sheet.max_row, 1).value = '备注：本页全部为模拟测试数据，仅验证微信图片发送，不用于业务统计。'
    book.save(workbook)
    book.close()
    images = render(workbook, folder)
    text = ('【同步工具测试推送文】\n' + day + '\n\n'
            '这是一条发送功能测试消息，不是真实项目日报。\n'
            '随后发送管道简报图片；图中所有数量均为模拟测试数据。\n'
            '预期顺序：本条推送文 → 简报图片。\n'
            '本次仅生成测试文件，没有连接平台、登录账号或下载文件。\n')
    text_file = folder / '微信推送文.txt'
    text_file.write_text(text, encoding='utf-8-sig')
    sha = lambda path:hashlib.sha256(path.read_bytes()).hexdigest()
    # Marker-relative paths remain valid regardless of server path or cwd.
    relative = lambda path:path.relative_to(HOME).as_posix()
    atomic_json(folder / '推送状态.json', dict(date=day, generated_at=at.isoformat(),
        ready=False, demo=True, text_file=text_file.name, text_sha256=sha(text_file)))
    atomic_json(folder / '简报推送状态.json', dict(date=day, generated_at=at.isoformat(),
        ready=False, demo=True, analysis_complete=False, sent=False, network_accessed=False,
        workbook=workbook.name, workbook_sha256=sha(workbook),
        images=[dict(path=path.name, sha256=sha(path)) for path in images]))
    package = HOME / 'releases' / '微信推送测试文件.zip'
    package.parent.mkdir(parents=True, exist_ok=True)
    with zipfile.ZipFile(package, 'w', zipfile.ZIP_DEFLATED) as archive:
        for path in [workbook, text_file, *images, folder / '推送状态.json', folder / '简报推送状态.json']:
            archive.write(path, relative(path))
    print(ascii(dict(image=relative(images[0]), package=relative(package), pages=len(images))))


if __name__ == '__main__':
    main()
