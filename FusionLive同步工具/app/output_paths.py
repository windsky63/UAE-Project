"""Keep derived artifacts outside immutable download and input directories."""

from app_paths import HOME, CODE
from pathlib import Path



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
