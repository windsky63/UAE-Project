"""Build a verified code-only patch for an existing project-tools server."""
import ast
import hashlib
import json
from pathlib import Path
import zipfile


ROOT = Path(__file__).resolve().parents[2]
RELEASE = '2026.10.30-multi-file-cleanup.1'
FILES = [
    'codex-wechat/wechat_local/config.py',
    'codex-wechat/wechat_local/file_cleanup.py',
    'codex-wechat/wechat_local/file_transfer.py',
    'codex-wechat/wechat_local/listener.py',
    'codex-wechat/wechat_local/codex_runner.py',
    'codex-wechat/wechat_local/shared_backend.py',
    'codex-wechat/wechat_local/wechat_uia.py',
    'codex-wechat/wechat.codex.example.json',
    'codex-wechat/README.md',
    'codex-wechat/tests/test_file_transfer.py',
    'codex-wechat/tests/test_upload_limit.py',
    'codex-wechat/tests/test_file_cleanup.py',
    'codex-wechat/tests/test_shared_backend.py',
    'codex-wechat/scripts/set_upload_limit.py',
    'codex-wechat/scripts/build_file_send_update.py',
    '管理中心/manager.py',
    '管理中心/tests/test_manager.py',
    '管理中心/web/src/ConfigPanel.vue',
    '管理中心/web/check-cleanup-ui.cjs',
]
GUIDE = '''# 微信多文件发送：服务器增量更新

版本：2026.10.30-multi-file-cleanup.1

适用于已安装 2026.10.30 完整版、已支持 file_action 的服务器。本包合入文件 ID、微信附件提交和共享后端 UTF-8 修复，无需单独安装此前两个微信补丁。不是首次安装包。

## 本次变化

- 新增发送暂存文件定时清理：默认每两天执行一次，仅删除已保留满两天的 ZIP 和发送快照副本。首次启动会检查已有过期暂存文件，之后按持久化计划执行；重启不会重置清理时间。监听停止期间不执行，恢复运行后补检查。
- 前端“微信与 Codex 设置”新增“发送暂存文件清理间隔”，可设 1～365 天，0 关闭。间隔同时作为最低保留天数，修改后安全重启监听生效；未写入此字段的旧配置自动使用两天默认值。
- 清理仅覆盖聊天工作目录的 outgoing_files 下由程序生成的任务目录。待处理任务、待续发和发送结果不明的文件保留；即使主任务过期，只要发送队列仍不确定就保留。拒绝通过符号链接或目录联接访问和删除外部目录。源下载文件、台账、聊天记录、发送去重记录和用户配置不删除。
- 1～3 份文件逐个发送，超过 3 份自动打包；用户明确要求压缩包时也会打包。大小上限按每个实际上传的文件计算，不限制所有附件的合计大小。
- ZIP 过大时按完整文件边界拆成多个可独立解压的 ZIP，每个都不超过上限。单个原文件超限时，打包请求仍可尝试压缩；若它单独压缩后仍超限，则本次不提交附件，并明确提示微信上传大小限制。
- 每次最多发送 3 个附件。拆出的 ZIP 超过 3 个时先发送前三个，再提示剩余数量；只有同一聊天的原请求人明确要求“继续发送”才处理下一批。待续记录有效期 15 分钟，超过后需重新选择；只询问剩余文件或其他成员的指示不会续发。
- 超限、发送关闭、文件校验失败或微信明确未提交且重试耗尽时，回复能确认的本地原文件完整路径和所在目录；失效或未授权的路径不编造、不披露。实际结果不明时等待消息库确认，不盲目重发。
- 默认及最大上限为 900 MiB，即 943,718,400 字节（约 944 MB），低于 1GB 参考值。2026-10-30 未能从微信官方帮助页面核验个人微信聊天的统一文件上限声明，不能将网上常见的 1GB 数字称为已核实官方上限。本配置为保守值，服务器微信版本的实际限制仍然适用；本次未进行 900 MiB 真实微信上传测试。
- 打包只读原文件并复制到独立发送快照，所有拆包、重建和清理均在发送暂存目录进行，不移动、覆盖、截断或删除下载原文件。同名文件在 ZIP 内使用编号目录保留；每个 ZIP 验证 CRC。每个附件有独立持久化状态，重试跳过已确认文件。

## 更新步骤

1. 核对 ZIP 的 SHA-256 与旁边的 .zip.sha256 一致：

   ```powershell
   Get-FileHash .\\Codex_Wechat_2026.10.30-multi-file-cleanup.1.zip -Algorithm SHA256
   Get-Content .\\Codex_Wechat_2026.10.30-multi-file-cleanup.1.zip.sha256
   ```

2. 在管理中心安全停止微信监听，等待现有任务完成；结束网页 Codex 对话后退出管理中心，并等待共享后端退出。不需要退出微信。
3. 先备份服务器原有的包内同名文件，特别是 wechat_local 下运行文件、管理中心/manager.py 和管理中心/web/dist；另备份原 wechat.codex.json。
4. ZIP 解压到独立暂存目录，将 codex-wechat 和管理中心文件夹按相同层级合并覆盖至服务器项目根目录。已包含构建完成的 web/dist，无需在服务器安装 Node 或重建前端。保留原有目录，不要删除整个 codex-wechat 或管理中心。
5. 从服务器项目根目录执行以下命令，将已有配置中的文件上限设置为 900 MiB。脚本只改 file_send_max_mib，其他字段保持原值；原配置按原始字节备份为旁边的 .before-upload-limit-时间.bak。配置不存在或格式错误时停止，不自动创建配置：

   ```powershell
   .\\.venv-analysis\\Scripts\\python.exe -X utf8 .\\codex-wechat\\scripts\\set_upload_limit.py
   ```

   必须执行这一步；只覆盖代码无法改变服务器配置中显式设置的旧上限。需要更低上限时，可在执行后手动将 file_send_max_mib 调低。

6. 重启管理中心与微信监听，刷新网页后可在微信配置中调整清理间隔（默认两天）。保留服务器已有的聊天授权、运行开关、数据库、下载文件、登录信息和 Python 环境；本包不含这些内容，不需要安装新依赖。不自动重放旧任务。
7. 在现有授权测试聊天中检查三份直发、四份自动打包、拆包后暂停续发及失败时的本地目录提示，并核对实际上传结果。

## 离线检查与回退

本包附 update-manifest.json，记录每个文件的大小与 SHA-256。打包检查 Python 3.12 语法、ZIP CRC 和逐文件校验值。回归使用模拟发送，不会实际发送微信。

可选测试（已安装 pytest）：

```powershell
Set-Location .\\codex-wechat
..\\.venv-analysis\\Scripts\\python.exe -X utf8 -m pytest tests/test_file_transfer.py tests/test_upload_limit.py tests/test_shared_backend.py tests/test_desktop_guard.py -q
```

回退：安全停止监听和管理中心，恢复之前备份的程序文件及原 wechat.codex.json，再重启。保留运行数据，不删除队列或发送记录。
'''


def digest(data):
    return hashlib.sha256(data).hexdigest()


def build():
    entries = {name: (ROOT / name).read_bytes() for name in FILES}
    for path in (ROOT / '管理中心/web/dist').rglob('*'):
        if path.is_file():
            assert not path.is_symlink() and path.resolve().is_relative_to(ROOT.resolve())
            entries[path.relative_to(ROOT).as_posix()] = path.read_bytes()
    assert '管理中心/web/dist/index.html' in entries
    entries['README-UPDATE.md'] = GUIDE.encode('utf-8')
    for name, data in entries.items():
        assert name not in {'codex-wechat/wechat.codex.json'}
        assert not {'data', 'runtime', '.venv', '.venv-analysis', '__pycache__'}.intersection(Path(name).parts)
        if name.endswith('.py'):
            ast.parse(data.decode('utf-8-sig'), filename=name, feature_version=(3, 12))
    manifest = dict(release=RELEASE, base_installation_required=True,
        file_send_max_mib=900, upload_limit_bytes=900 * 1024**2, file_send_cleanup_days=2,
        runtime_included=False, credentials_included=False,
        config_update_command=r'.\.venv-analysis\Scripts\python.exe -X utf8 .\codex-wechat\scripts\set_upload_limit.py',
        preserve=['codex-wechat/wechat.codex.json', 'codex-wechat/data', '.venv-analysis'],
        files={name: dict(bytes=len(data), sha256=digest(data)) for name, data in entries.items()})
    entries['update-manifest.json'] = json.dumps(manifest, ensure_ascii=False, indent=2).encode('utf-8')
    output = ROOT / '管理中心/releases' / f'Codex_Wechat_{RELEASE}.zip'
    output.parent.mkdir(parents=True, exist_ok=True)
    with zipfile.ZipFile(output, 'w', zipfile.ZIP_DEFLATED) as archive:
        for name, data in sorted(entries.items()):
            archive.writestr(name, data)
    with zipfile.ZipFile(output) as archive:
        assert archive.testzip() is None
        assert len(archive.namelist()) == len(set(archive.namelist())) == len(entries)
        for name, data in entries.items():
            assert archive.read(name) == data, name
    checksum = digest(output.read_bytes())
    output.with_suffix('.zip.sha256').write_text(checksum + '  ' + output.name + '\n', encoding='utf-8')
    output.with_suffix('.README.md').write_text(GUIDE, encoding='utf-8')
    report = dict(release=RELEASE, archive=str(output), bytes=output.stat().st_size,
        entries=len(entries), sha256=checksum, crc_verified=True,
        per_file_hashes_verified=True, python312_syntax_checked=True,
        runtime_included=False, credentials_included=False)
    output.with_suffix('.verification.json').write_text(json.dumps(report, ensure_ascii=False, indent=2), encoding='utf-8')
    print(json.dumps(report, ensure_ascii=False, indent=2))


if __name__ == '__main__':
    build()
