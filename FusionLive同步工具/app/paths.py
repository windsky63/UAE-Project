"""All persisted local paths are relative to the portable tool directory."""

from pathlib import Path, PureWindowsPath
import os
import sys

CODE = Path(__file__).resolve().parent
HOME = CODE.parent
if str(HOME) not in sys.path:
    sys.path.append(str(HOME))


def exports_dir(base=None):
    """Generated ledger/inventory exports live outside the program root."""
    base = Path(base or HOME).resolve()
    folder = check_output_directory(base / 'exports', base_dir=base)
    folder.mkdir(parents=True, exist_ok=True)
    return folder


def plain_path(value):
    text = str(value)
    if text.startswith('\\\\?\\UNC\\'):
        return '\\\\' + text[8:]
    if text.startswith('\\\\?\\'):
        return text[4:]
    return text


def native_path(value):
    path = Path(plain_path(value)).resolve()
    text = str(path)
    if os.name == 'nt' and len(text) >= 240:
        return Path('\\\\?\\UNC\\' + text[2:] if text.startswith('\\\\') else '\\\\?\\' + text)
    return path


def relative_path(value, base=None):
    if not value:
        return None
    base = Path(plain_path(base or HOME)).resolve()
    text = plain_path(value)
    win = PureWindowsPath(text)
    path = Path(text)
    if path.is_absolute():
        try:
            return path.resolve().relative_to(base).as_posix()
        except ValueError:
            pass
    if path.is_absolute() or win.is_absolute():
        # Relocate older databases after the whole tool directory was copied.
        parts = win.parts if win.is_absolute() else path.parts
        indices = [i for i, part in enumerate(parts) if part.lower() in ('downloads', 'history', 'runtime')]
        if not indices:
            raise ValueError('旧绝对路径无法定位到工具目录：请保留 downloads 和 runtime 的目录结构。')
        text = '/'.join(parts[indices[0]:])
    else:
        text = text.replace('\\', '/')
    result = (base / text).resolve()
    if not result.is_relative_to(base):
        raise ValueError('本地文件路径超出工具目录。')
    return result.relative_to(base).as_posix()


def resolve_path(value, base=None):
    if not value:
        raise ValueError('本地文件路径为空。')
    base = Path(plain_path(base or HOME)).resolve()
    return native_path(base / relative_path(value, base))


def check_output_directory(output, *, database=None, source_root=None, base_dir=None):
    output = Path(output).resolve()
    protected = {(HOME / 'downloads').resolve()}
    if base_dir:
        protected.add((Path(base_dir) / 'downloads').resolve())
    if database:
        parent = Path(database).resolve().parent
        tool = parent.parent if parent.name.casefold() == 'runtime' else parent
        protected.add((tool / 'downloads').resolve())
    if source_root:
        source = Path(source_root).resolve()
        protected.add(source)
        protected.update(p for p in (source, *source.parents) if p.name.casefold() == 'downloads')
    if any(output.is_relative_to(p) for p in protected):
        raise ValueError('输出目录不能位于downloads下载目录或输入目录内；请使用独立的runtime或logs目录。')
    return output
