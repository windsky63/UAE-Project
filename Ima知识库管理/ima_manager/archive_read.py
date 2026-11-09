"""Read indexed ZIP members, including nested ZIPs, without extracting to source."""
from contextlib import ExitStack
from pathlib import PurePosixPath
import tempfile
import zipfile

from .api import ImaError


MEMBER_LIMIT = 200 * 1024 * 1024
MAX_DEPTH = 8


def copy_member(original_stream, member, buffer, runtime):
    parts = member.replace('\\', '/').split('!/')
    if len(parts) > MAX_DEPTH:
        raise ImaError('ZIP 嵌套层数超过 8 层，未自动提取')
    for part in parts:
        path = PurePosixPath(part)
        if (not part or path.is_absolute() or '..' in path.parts
                or any(':' in item for item in path.parts)):
            raise ImaError('ZIP 成员路径不安全')
    with ExitStack() as stack:
        current = original_stream
        for index, part in enumerate(parts):
            archive = stack.enter_context(zipfile.ZipFile(current))
            matches = [info for info in archive.infolist()
                       if info.filename.replace('\\', '/').strip() == part]
            if len(matches) != 1 or matches[0].is_dir():
                raise ImaError('ZIP 中没有唯一的对应成员')
            if matches[0].file_size > MEMBER_LIMIT:
                raise ImaError('ZIP 成员超过 200 MiB，未自动提取')
            last = index == len(parts) - 1
            if not last and PurePosixPath(part).suffix.lower() != '.zip':
                raise ImaError('仅支持从嵌套 ZIP 读取图纸，不支持其他嵌套压缩格式')
            output = buffer if last else stack.enter_context(tempfile.TemporaryFile(dir=runtime))
            with archive.open(matches[0]) as reader:
                copied = 0
                while chunk := reader.read(1024 * 1024):
                    copied += len(chunk)
                    if copied > MEMBER_LIMIT:
                        raise ImaError('ZIP 成员超过 200 MiB，未自动提取')
                    output.write(chunk)
            output.seek(0)
            current = output
