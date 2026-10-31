"""Console menu and durable diagnostics for the ASCII Windows launcher."""
import subprocess
import sys

from app_paths import HOME
from runtime_logging import BoundedLog


def main(argv=None):
    arguments = list(sys.argv[1:] if argv is None else argv)
    log = BoundedLog(HOME/'runtime/path_maintenance_launcher.log')

    def message(text):
        print(text, flush=True)
        try:
            log.write(text+'\n')
        except OSError as exc:
            print('启动日志无法写入：'+str(exc), file=sys.stderr, flush=True)

    message('文件目录整理；Python：'+sys.executable)
    message('启动日志：'+str(log.path))
    try:
        if not arguments:
            message('执行整理前，请先安全退出同步、调度和后台解析。')
            message('1. 只预览，不移动文件\n2. 执行整理，自动备份台账\n0. 退出')
            while True:
                try:
                    choice = input('请选择 [1/2/0]，然后回车：').strip()
                except EOFError:
                    message('无法读取菜单选择，本次未执行整理。请从终端运行，或使用命令行参数。')
                    return 2
                if choice == '0':
                    message('已退出，本次未执行整理。')
                    return 0
                if choice in ('1','2'):
                    arguments = ['--apply'] if choice == '2' else []
                    break
                message('无效选择，请输入1、2或0。')
        launcher = HOME/'main.py'
        if not launcher.is_file():
            raise FileNotFoundError('程序不完整，缺少：'+str(launcher))
        message('开始执行：'+('执行整理' if '--apply' in arguments else '预览或命令行检查'))
        with subprocess.Popen([sys.executable,'-u',str(launcher),'path_maintenance',*arguments],
                cwd=HOME,stdout=subprocess.PIPE,stderr=subprocess.STDOUT,
                text=True,encoding='utf-8',errors='replace') as process:
            try:
                for line in process.stdout:
                    message(line.rstrip('\r\n'))
            except KeyboardInterrupt:
                message('收到中断，等待整理进程保存恢复记录并退出。')
            result = process.wait()
        message(f'目录整理进程退出码：{result}；'+('已完成。' if result == 0 else '未完成，请查看上方错误和启动日志。'))
        return result
    except (OSError, ValueError) as exc:
        message('目录整理启动失败：'+str(exc))
        return 2


if __name__ == '__main__':
    raise SystemExit(main())
