"""Bounded ZIP/7z extraction with explicit collision handling and member audit."""

from app_paths import HOME, CODE
from collections import defaultdict, deque
import hashlib
import csv
import os
from pathlib import Path, PurePosixPath, PureWindowsPath
import re
import stat
import tempfile
import zipfile
from output_paths import check_output_directory
import textwrap
import parse_pcf_to_json as pcf
import argparse
from contextlib import closing
from datetime import datetime, timedelta, timezone
import json
import math
import sqlite3
import parse_idf_to_json as idf


# Shared by ZIP, 7z and all nested archives under one source file.
MAX_ARCHIVE_FILE_BYTES = 16 * 1024**3
MAX_ARCHIVE_EXPANDED_BYTES = 64 * 1024**3
MAX_ARCHIVE_ENTRIES = 20000


def permanent_analysis_error(exc):
    # Invalid content, unsupported inputs and parser errors do not improve on retry.
    return isinstance(exc, (ValueError, RuntimeError, NotImplementedError,
                            KeyError, IndexError, TypeError, zipfile.BadZipFile, MemoryError))


def _parts(name):
    text = name.replace('\\', '/')
    parts = PurePosixPath(text).parts
    if not parts or text.startswith('/') or PureWindowsPath(text).drive or '..' in parts:
        raise ValueError('压缩包包含越界路径：'+name)
    for part in parts:
        if part.endswith((' ', '.')) or re.search(r'[<>:"|?*\x00-\x1f]', part) or part.split('.')[0].upper() in {
                'CON', 'PRN', 'AUX', 'NUL', *(f'COM{i}' for i in range(1, 10)), *(f'LPT{i}' for i in range(1, 10))}:
            raise ValueError('压缩包包含不安全的文件名：'+name)
    return parts


class _Names:
    def __init__(self):
        self.used, self.directories = set(), {}

    def claim(self, parts, directory, index):
        parent = ()
        for i, name in enumerate(parts):
            is_dir = directory or i < len(parts)-1
            original = tuple(p.casefold() for p in parts[:i+1])
            if is_dir and original in self.directories:
                parent = self.directories[original]
                continue
            target = (*parent, name)
            suffix = 0
            while '/'.join(target).casefold() in self.used:
                suffix += 1
                leaf = PurePosixPath(name)
                alternative = f'{leaf.stem}__entry_{index:06d}_{suffix}{leaf.suffix}'
                target = (*parent, alternative)
            self.used.add('/'.join(target).casefold())
            if is_dir:
                self.directories[original] = target
            parent = target
        return Path(*parent)


def _plan(members, budget, max_bytes, max_files, max_file_bytes, archive_size=None):
    names, entries, total = _Names(), [], 0
    for index, member in enumerate(members, 1):
        parts = _parts(member['member'])
        directory, size = member['directory'], member['size']
        if not directory:
            if size < 0 or size > max_file_bytes:
                raise ValueError(f'压缩包内单文件超过{max_file_bytes}字节限制：{member["member"]}（{size}字节）')
            compressed = member.get('compressed')
            if compressed is not None and size > max(1, compressed)*1000:
                raise ValueError('压缩包内单文件压缩比超过1000倍：'+member['member'])
            total += size
        entries.append({**member, 'entry': index, 'output': names.claim(parts, directory, index),
                        '_key': '/'.join(parts).casefold()})
    if archive_size is not None and total > max(1, archive_size)*1000:
        raise ValueError('7z总展开压缩比超过1000倍。')
    budget = budget if budget is not None else {'bytes': max_bytes, 'files': max_files}
    if total > budget['bytes'] or len(entries) > budget['files']:
        raise ValueError('压缩包展开总大小或文件数超过本次分析上限。')
    budget['bytes'] -= total
    budget['files'] -= len(entries)
    return entries


def _publish(staging, destination, entries, audit):
    files, by_original, records = [], defaultdict(dict), []
    for entry in entries:
        path = staging / entry['output']
        record = {k: entry[k] for k in ('entry', 'member', 'size')}
        record['output'] = entry['output'].as_posix()
        if entry['directory']:
            record['state'] = '目录已合并'
        else:
            if not path.is_file() or path.stat().st_size != entry['size']:
                raise ValueError('压缩包文件大小校验失败：'+entry['member'])
            digest = hashlib.sha256()
            with path.open('rb') as stream:
                for block in iter(lambda: stream.read(1024*1024), b''):
                    digest.update(block)
            record['sha256'] = digest.hexdigest()
            prior = by_original[entry['_key']]
            if record['sha256'] in prior:
                # Only this unpublished staging copy is removed; source archives stay untouched.
                path.unlink()
                record.update(state='同路径同内容，去重', duplicate_of=prior[record['sha256']])
            else:
                record['state'] = '同路径异内容，分别保留' if prior else '已解压'
                prior[record['sha256']] = record['output']
                files.append(destination / entry['output'])
        records.append(record)
    staging.rename(destination)
    if audit is not None:
        audit.extend(records)
    return files


def _destination(archive, destination):
    destination = check_output_directory(destination, source_root=archive)
    if destination.exists():
        raise ValueError('解压目标必须为新目录，不能覆盖已有目录。')
    return destination


def extract_zip(archive, destination, *, budget=None, max_files=MAX_ARCHIVE_ENTRIES,
                max_bytes=MAX_ARCHIVE_EXPANDED_BYTES, max_file_bytes=MAX_ARCHIVE_FILE_BYTES, audit=None):
    archive = Path(archive)
    destination = _destination(archive, destination)
    with zipfile.ZipFile(archive) as source:
        members = []
        for info in source.infolist():
            mode = info.external_attr >> 16
            if stat.S_ISLNK(mode) or stat.S_IFMT(mode) not in (0, stat.S_IFREG, stat.S_IFDIR):
                raise ValueError('ZIP包含链接或特殊文件。')
            if info.flag_bits & 1:
                raise ValueError('加密ZIP暂不支持；不会尝试密码。')
            members.append(dict(member=info.filename, directory=info.is_dir(), size=info.file_size,
                                compressed=info.compress_size, info=info))
        entries = _plan(members, budget, max_bytes, max_files, max_file_bytes)
        destination.parent.mkdir(parents=True, exist_ok=True)
        with tempfile.TemporaryDirectory(prefix='.extract-', dir=destination.parent) as temp:
            staging = Path(temp)
            for entry in entries:
                target = staging / entry['output']
                if entry['directory']:
                    target.mkdir(parents=True, exist_ok=True)
                    continue
                target.parent.mkdir(parents=True, exist_ok=True)
                size = 0
                with source.open(entry['info']) as stream, target.open('xb') as out:
                    for block in iter(lambda: stream.read(1024*1024), b''):
                        size += len(block)
                        if size > entry['size']:
                            raise ValueError('ZIP实际展开大小超过声明值：'+entry['member'])
                        out.write(block)
            return _publish(staging, destination, entries, audit)


def extract_7z(archive, destination, *, budget=None, max_files=MAX_ARCHIVE_ENTRIES,
               max_bytes=MAX_ARCHIVE_EXPANDED_BYTES, max_file_bytes=MAX_ARCHIVE_FILE_BYTES, audit=None):
    try:
        import py7zr
        from py7zr.io import Py7zIO, WriterFactory
    except ImportError as exc:
        raise RuntimeError('7z解析缺少py7zr依赖，请运行maintenance/安装依赖.cmd。') from exc
    archive = Path(archive)
    destination = _destination(archive, destination)
    try:
        # Passing a stream also keeps extraction sequential for duplicate-name ordering.
        with archive.open('rb') as stream, py7zr.SevenZipFile(stream, max_extract_size=max_bytes) as source:
            if source.needs_password():
                raise ValueError('加密7z需要密码，暂不自动解压。')
            members = []
            for info in source.list():
                if info.is_symlink or not (info.is_file or info.is_directory):
                    raise ValueError('7z包含链接或特殊文件：'+info.filename)
                members.append(dict(member=info.filename, directory=info.is_directory,
                                    size=info.uncompressed, compressed=None))
            entries = _plan(members, budget, max_bytes, max_files, max_file_bytes,
                            archive_size=archive.stat().st_size)
            destination.parent.mkdir(parents=True, exist_ok=True)
            with tempfile.TemporaryDirectory(prefix='.extract-', dir=destination.parent) as temp:
                staging = Path(temp)
                routes, counts = defaultdict(deque), defaultdict(int)
                for entry in entries:
                    name = entry['member']
                    output_name = name if not counts[name] else f'{name}_{counts[name]-1}'
                    counts[name] += 1
                    if entry['directory']:
                        (staging/entry['output']).mkdir(parents=True, exist_ok=True)
                    else:
                        routes[(staging/output_name).as_posix()].append(entry)

                class DiskWriter(Py7zIO):
                    def __init__(self, entry):
                        self.path = staging/entry['output']
                        self.path.parent.mkdir(parents=True, exist_ok=True)
                        self.file = self.path.open('x+b')
                        self.limit = entry['size']
                    def write(self, data):
                        if self.file.tell()+len(data) > self.limit:
                            raise ValueError('7z实际展开大小超过声明值。')
                        return self.file.write(data)
                    def read(self, size=None):
                        return self.file.read(-1 if size is None else size)
                    def seek(self, offset, whence=0):
                        return self.file.seek(offset, whence)
                    def flush(self):
                        if not self.file.closed:self.file.flush()
                    def size(self):
                        self.flush()
                        return self.path.stat().st_size
                    def close(self):
                        self.file.close()

                class Factory(WriterFactory):
                    def __init__(self):self.writers = []
                    def create(self, filename):
                        if filename not in routes or not routes[filename]:
                            raise ValueError('7z解压条目未通过路径检查：'+filename)
                        writer = DiskWriter(routes[filename].popleft())
                        self.writers.append(writer)
                        return writer

                factory = Factory()
                try:
                    source.extractall(path=staging, factory=factory)
                finally:
                    for writer in factory.writers:writer.close()
                if any(routes.values()):
                    raise ValueError('7z存在未完成解压的条目。')
                return _publish(staging, destination, entries, audit)
    except (ValueError, RuntimeError, OSError):
        raise
    except Exception as exc:
        raise ValueError('7z解压或校验失败：'+str(exc)) from exc


REFERENCE = re.compile(r'^PIPELINE-REFERENCE(?:[ \t]+(.*))?$', re.I)


def read_pcf(path):
    data = Path(path).read_bytes()
    if data.startswith((b'\xff\xfe\x00\x00', b'\x00\x00\xfe\xff')):
        return data.decode('utf-32')
    if data.startswith((b'\xff\xfe', b'\xfe\xff')):
        return data.decode('utf-16')
    if b'\x00' in data[:2048]:
        for encoding in ('utf-16-le', 'utf-16-be'):
            try:
                text = data.decode(encoding)
                if 'PIPELINE-REFERENCE' in text.upper() or 'UNITS-BORE' in text.upper():
                    return text
            except UnicodeDecodeError:
                pass
    return pcf.read_text(path)


def _component(line):
    word = line.strip().upper()
    return bool(word and word != 'MATERIALS' and pcf.is_component_start(line)
                and word not in pcf.INFORMATION_COMPONENT_TYPES)


def pipeline_sections(text):
    lines = textwrap.dedent(text.expandtabs(4)).splitlines()
    prefix, sections, current, reference = [], [], [], None
    has_components, duplicates = False, 0
    for line_number, line in enumerate(lines, 1):
        if line.lstrip().startswith(('#', '//')):
            continue
        # A reference in the header may be indented. Within a component, an
        # indented field is an attribute, not a new global pipeline declaration.
        match = REFERENCE.fullmatch(line.strip()) if not has_components or not line.startswith(' ') else None
        if match:
            name = (match[1] or '').strip()
            if len(name) >= 2 and name[0] == name[-1] and name[0] in ('"', "'"):
                name = name[1:-1].strip()
            if not name:
                raise ValueError(f'PCF第{line_number}行PIPELINE-REFERENCE的管线号为空。')
            canonical = 'PIPELINE-REFERENCE '+name
            if reference is None:
                if any(_component(item) for item in prefix):
                    raise ValueError('PCF管线声明前已出现元件，无法确定其管线归属。')
                reference, current = name, [canonical]
            elif not has_components:
                if name.casefold() != reference.casefold():
                    raise ValueError('PCF头部存在不同管线号但没有可分段的元件边界，待核对。')
                duplicates += 1
            else:
                carry = []
                while current:
                    tail = current[-1].strip().upper()
                    if not tail or (not current[-1].startswith(' ') and tail.startswith(('UNITS-', 'ISOGEN-FILES'))):
                        carry.insert(0, current.pop())
                    else:
                        break
                sections.append((reference, prefix+current))
                reference, current, has_components = name, carry+[canonical], False
        elif reference is None:
            prefix.append(line)
        else:
            current.append(line)
            has_components = has_components or _component(line)
    if reference is None:
        raise ValueError('PCF头部未识别到有效PIPELINE-REFERENCE管线号；未使用文件名猜测管线号。')
    sections.append((reference, prefix+current))
    return sections, duplicates


def parse_models(path, output):
    path = Path(path)
    output = check_output_directory(output, source_root=path)
    sections, duplicates = pipeline_sections(read_pcf(path))
    output.mkdir(parents=True, exist_ok=True)
    result = []
    for index, (reference, lines) in enumerate(sections, 1):
        # Concatenated complete PCFs repeat the global units before each section.
        # Keep the effective (last) declaration so identical sections also have
        # identical cache identities, while explicit unit changes remain distinct.
        last_units = {}
        header_end = len(lines)
        for line_index, line in enumerate(lines):
            if _component(line):
                header_end = line_index
                break
            key = line.strip().split(None,1)[0].upper() if line.strip() else ''
            if key.startswith('UNITS-') or key == 'ISOGEN-FILES':
                last_units[key] = line_index
        lines = [line for line_index,line in enumerate(lines)
                 if line_index >= header_end or not line.strip()
                 or last_units.get(line.strip().split(None,1)[0].upper(),line_index) == line_index]
        normalized = '\n'.join(lines)+'\n'
        folder = output / f'pcf-section-{index:04d}'
        folder.mkdir(exist_ok=True)
        target = folder / path.name
        target.write_text(normalized, encoding='utf-8')
        structure = pcf.load_pcf_structure(target)
        blocks = pcf.parse_upper_components(structure)
        if not blocks:
            raise ValueError(f'PCF管线{reference}未识别到元件，不能作为有效管线。')
        model = pcf.parse_pcf(target)
        if not model.get('components'):
            raise ValueError(f'PCF管线{reference}没有可解析的几何元件。')
        locations = {v['pcfBlockIndex']: v for v in blocks}
        for component in model['components']:
            attributes = locations.get(component.get('pcfBlockIndex'), {}).get('attributes', {})
            location = (normalize_weld_location(attributes.get('WELD-ATTRIBUTE3'))
                        if component.get('type') == 'weld' else None)
            location = location or normalize_weld_location(attributes.get('WELD-LOCATION'))
            if location:
                component['weldLocation'] = location
        model['_analysis_sha256'] = hashlib.sha256(normalized.encode('utf-8')).hexdigest()
        model['_pcf_path'] = str(target)
        model['_input_notes'] = f'明确分段管线{index}/{len(sections)}；头部同号重复声明合并{duplicates}次'
        result.append(('PCF', model))
    return result


UNIT = re.compile(r'(?<![A-Z0-9])((?:H\d+|GG)-\d{2,3})(?=[_\W]|$)', re.I)
SUPPORTED = {'.zip', '.pcf', '.idf', '.sha', '.7z', '.rar', '.pdf'}
MAX_ZIP_LAYERS = 5
METRICS = ('管线数', '图纸数', '焊缝数', '寸口量', '预制焊缝数', '预制寸口量', '安装焊缝数', '安装寸口量')


def digest(path):
    h = hashlib.sha256()
    with Path(path).open('rb') as stream:
        for block in iter(lambda: stream.read(1024 * 1024), b''):
            h.update(block)
    return h.hexdigest()


def unit_from(*texts):
    from business_type import pdf_unit
    for text in texts:
        # A member's own construction number takes precedence over its package's
        # directory context. pdf_unit also validates original short unit numbers.
        name = str(text).replace('\\', '/').rsplit('!/', 1)[-1].rsplit('/', 1)[-1]
        numbered_unit = pdf_unit(name, None)
        if numbered_unit:
            return numbered_unit
        matches = {m.upper() for m in UNIT.findall(str(text))}
        if len(matches) == 1:
            return matches.pop()
    return '未识别单元'


def ledger_sources(database, base_dir=None):
    """Use recorded local paths only; a done status alone proves no local presence."""
    database = Path(database).resolve()
    base = Path(base_dir).resolve() if base_dir else database.parent.parent
    sources, inventory = [], []
    with closing(sqlite3.connect(database.as_uri() + '?mode=ro', uri=True)) as db:
        db.row_factory = sqlite3.Row
        columns = {r[1] for r in db.execute('PRAGMA table_info(files)')}
        sha = 'sha256' if 'sha256' in columns else 'NULL AS sha256'
        for row in db.execute(f'SELECT project,id,filename,path,status,bytes,{sha} FROM files WHERE active=1'):
            if Path(row['filename']).suffix.lower() not in SUPPORTED:
                continue
            item = dict(row)
            item['state'] = '待服务器本地文件'
            value = row['path']
            path = None
            if value:
                value = str(value).replace('\\', '/')
                candidate = Path(value)
                if candidate.is_absolute() or PureWindowsPath(value).is_absolute():
                    parts = PureWindowsPath(value).parts if PureWindowsPath(value).is_absolute() else candidate.parts
                    start = next((i for i, v in enumerate(parts) if v.lower() == 'downloads'), None)
                    candidate = base.joinpath(*parts[start:]) if start is not None else candidate
                else:
                    candidate = base / candidate
                path = candidate.resolve()
                item['resolved_path'] = str(path)
                if not path.is_relative_to(base):
                    item['state'] = '台账路径超出指定工具目录，未读取'
                    path = None
            if path and path.is_file() and not path.is_symlink():
                if row['status'] == 'done' and row['bytes'] == path.stat().st_size:
                    item['state'] = '本地文件可分析'
                    sources.append({'path': path, 'project': row['project'], 'origin': row['filename'] + ' / ' + str(path), 'item': item})
                else:
                    item['state'] = '本地文件未完成或大小不符，未分析'
            inventory.append(item)
    return sources, inventory


def model_weld_rows(model, source, unit=None):
    """Same material filter as the supplied PCF parser; exclude screw joints."""
    if source == 'PCF':
        # Match the standalone PCF entry point before deriving classification
        # inputs. Catalogue codes identify types, not individual components.
        unit = unit or model.get('unitName') or unit_from(model.get('pipelineName'), model.get('_pcf_path'))
        idf.apply_model_unit_context(model, unit)
        idf.assign_material_unique_codes(model)
    rows = pcf.build_pcf_weld_table_rows(model) if source == 'PCF' else idf.build_weld_table_rows(model)
    locations = {(idf.format_weld_no_for_csv(c.get('weldNo')), idf.format_weld_coordinate(c)): c.get('weldLocation')
                 for c in model.get('components', []) if c.get('type') == 'weld'}
    by_point = defaultdict(set)
    for component in model.get('components', []):
        if component.get('type') == 'weld' and component.get('weldLocation'):
            by_point[idf.format_weld_coordinate(component)].add(component['weldLocation'])
    rows = [{**row, '_location': locations.get((row.get('焊口号'), row.get('焊点坐标'))) or
             (next(iter(by_point[row.get('焊点坐标')])) if len(by_point[row.get('焊点坐标')]) == 1 else None)}
            for row in rows if row.get('焊接类型') != 'scw']
    if source == 'PCF':
        from weld_classification import classify_pcf
        try:
            rows = classify_pcf(model, model['_pcf_path'], rows)
        except Exception as exc:
            raise ValueError(f'PCF预制率算法执行失败：{type(exc).__name__}：{exc}') from exc
    return rows


def normalize_weld_location(value):
    """Normalize explicit location labels without matching unrelated words."""
    label = re.sub(r'\bweld\b', '', str(value or '').casefold()).strip()
    if label in ('shop', 'frefab', 'prefab', 'pre-fab'):
        return 'shop'
    if label in ('field', 'site', 'install'):
        return 'field'
    return None


def classify(row, rule):
    if '_classification_engine' in row:
        return row.get('_classification')
    # Extracted from build_prefab_spools.classify_weld; opt-in estimates only.
    location = normalize_weld_location(row.get('_location'))
    if location:
        return location
    if rule == 'none':
        return None
    dn = idf.safe_float(row.get('公称直径'))
    if rule == 'no-small' and 0 < dn < 50:
        return 'field'
    if row.get('焊接类型') in {'olet', 'seton'}:
        return 'field'
    return 'shop'


def parse_file(path, split_dir):
    split_dir = check_output_directory(split_dir, source_root=path)
    suffix = path.suffix.lower()
    if suffix == '.pcf':
        return parse_models(path, split_dir)
    text = idf.read_text(path)
    if not re.search(r'^\s*-6\s', text, re.M):
        raise ValueError('IDF未识别到-6管线记录。')
    try:
        split = idf.split_multi_pipeline_idf(path, split_dir)
    except SystemExit as exc:
        raise ValueError(str(exc)) from exc
    paths = [Path(v['path']) for v in split['files']] if split else [path]
    models = [idf.parse_idf(p) for p in paths]
    if any(m.get('pipelineName') in ('', 'UNKNOWN', None) or not m.get('components') for m in models):
        raise ValueError('IDF管线号或元件未能解析。')
    return [('IDF', model) for model in models]


def analyze(*, database=None, root=None, project='本地项目', output=None, base_dir=None, prefab_rule='none',
            sources_override=None, cached_reports=None, inventory_override=None, progress=None, extraction_cache=None):
    """Returns report groups plus durable inventory and audit, never changes ledger."""
    if prefab_rule not in ('none', 'no-small', 'all'):
        raise ValueError('无效预制分类规则。')
    output = check_output_directory(output or HOME / 'runtime/file_analysis',
                                    database=database, source_root=root, base_dir=base_dir)
    output.mkdir(parents=True, exist_ok=True)
    extraction_cache = Path(extraction_cache) if extraction_cache else output / 'extraction_cache'
    run = output / datetime.now(timezone(timedelta(hours=8))).strftime('analysis-%Y%m%d-%H%M%S-%f')
    run.mkdir()
    def note(message):
        if progress is not None:
            progress(message)
    sources, inventory = ledger_sources(database, base_dir) if database and not root and sources_override is None else ([], [])
    if sources_override is not None:
        sources = sources_override
    if inventory_override is not None:
        inventory = inventory_override
    if root:
        root = Path(root).resolve()
        if not root.exists():
            raise FileNotFoundError('本地分析目录不存在：' + str(root))
        paths = [root] if root.is_file() else sorted(root.rglob('*'))
        sources = [{'path': p, 'project': project, 'origin': str(p)} for p in paths
                   if p.is_file() and not p.is_symlink() and p.suffix.lower() in SUPPORTED and not p.resolve().is_relative_to(output)]
    audit, pipeline_models, seen_files, groups, weld_details, archive_audit = [], {}, set(), {}, [], []
    parsed_file_units = {}
    parsed_file_units = {}
    incomplete = set()
    for cached in cached_reports or []:
        audit.extend(cached['files'])
        archive_audit.extend(cached.get('archive_members', []))
        incomplete.update(tuple(v) for v in cached.get('incomplete', []))
        for item in cached.get('pipeline_results', []):
            key = item['project'], item['unit'], item['source'], item['pipeline'].casefold()
            if item['conflict']:
                incomplete.add(key[:3])
            if key in pipeline_models:
                if pipeline_models[key]['sha256'] != item['sha256'] or item['conflict']:
                    pipeline_models[key]['conflict'] = True
                    incomplete.add(key[:3])
                continue
            pipeline_models[key] = dict(item)
    def emit(path, project, origin, depth=0, budget=None):
        suffix = path.suffix.lower()
        unit = unit_from(origin)
        record = {'project': project, 'origin': origin, 'path': str(path), 'unit': unit, 'format': suffix}
        audit.append(record)
        try:
            note(f'校验文件内容：{origin}；包内层级 {depth}')
            file_hash = digest(path)
            record['sha256'] = file_hash
            if suffix in ('.rar', '.sha'):
                record.update(state='暂不支持解析', detail='SHA需格式样例或解析代码；rar请先转ZIP/7z或手动解压。')
                return
            if suffix == '.pdf':
                record.update(state='仅登记文件，内容未解析')
                return
            identity = (project, unit, suffix, file_hash)
            if identity in seen_files and suffix not in ('.zip', '.7z'):
                record['state'] = '内容重复，跳过'
                record['unit'] = parsed_file_units.get((project, suffix, file_hash), unit)
                record['unit'] = parsed_file_units.get((project, suffix, file_hash), unit)
                return
            seen_files.add(identity)
            if suffix in ('.zip', '.7z'):
                if depth >= MAX_ZIP_LAYERS:
                    raise ValueError(f'嵌套ZIP/7z超过{MAX_ZIP_LAYERS}层，本分支未分析。')
                extract_dir = run / 'extracted' / f'{len(audit):06d}-{file_hash[:12]}'
                members = []
                extractor = extract_zip if suffix == '.zip' else extract_7z
                note(f'检查 {suffix.upper()[1:]} 解压缓存：{origin}')
                from archive_cache import extract_cached
                files = extract_cached(path, extract_dir, cache=extraction_cache, sha=file_hash,
                    extractor=extractor, budget=budget, audit=members, progress=note)
                archive_audit.extend(dict(m, project=project, archive=origin, format=suffix,
                    extracted_path=str(extract_dir / (m.get('duplicate_of') or m['output']))) for m in members)
                record.update(state='已解压', files=len(files))
                candidates = [p for p in files if p.suffix.lower() in SUPPORTED]
                note(f'解压完成：{origin}；输出 {len(files)} 个文件；支持处理 {len(candidates)} 个；包内审计记录 {len(members)} 条')
                for index, extracted in enumerate(candidates, 1):
                    member_origin = origin + '!/' + extracted.relative_to(extract_dir).as_posix()
                    note(f'包内进度 {index}/{len(candidates)}：{member_origin}')
                    emit(extracted, project, member_origin, depth + 1, budget)
                return
            note(f'解析 {suffix.upper()[1:]} 管线与元件：{origin}')
            models = parse_file(path, run / 'split_idf' / file_hash[:16])
            note(f'管线元件解析完成：{origin}；识别 {len(models)} 条管线，随后核对重复和冲突')
            record['pipelines'] = [str(model['pipelineName']) for _, model in models]
            file_units = {unit_from(str(model['pipelineName']), origin) for _, model in models}
            record['unit'] = next(iter(file_units)) if len(file_units) == 1 else '跨单元文件'
            parsed_file_units[project, suffix, file_hash] = record['unit']
            parsed_file_units[project, suffix, file_hash] = record['unit']
            for source, model in models:
                model_hash = model.pop('_analysis_sha256', file_hash)
                if model.get('_input_notes'):
                    record['detail'] = model['_input_notes']
                pipe = str(model['pipelineName']).strip()
                actual_unit = unit_from(pipe, origin)
                key = (project, actual_unit, source, pipe.casefold())
                if key in pipeline_models:
                    prior = pipeline_models[key]
                    if prior['sha256'] == model_hash:
                        record.update(state='内容重复，跳过', pipeline=pipe)
                        continue
                    prior['conflict'] = True
                    record.update(state='同管线不同内容，待核对版本或分段', pipeline=pipe)
                    incomplete.add(key[:3])
                    continue
                parsed_rows = model_weld_rows(model, source, actual_unit)
                pipeline_models[key] = dict(model=model, rows=parsed_rows, origin=origin, conflict=False, sha256=model_hash)
                record.update(state='已解析', pipeline=pipe, source=source)
        except (OSError, ValueError, RuntimeError, zipfile.BadZipFile, NotImplementedError, KeyError, IndexError, TypeError) as exc:
            record.update(state='分析失败', detail=str(exc), retryable=not permanent_analysis_error(exc))
            # A failed file or archive can hide additional pipelines in this unit.
            affected = (suffix[1:].upper(),) if suffix in ('.pcf', '.idf') else ('PCF', 'IDF') if suffix in ('.zip', '.7z', '.rar') else ()
            for source in affected:
                incomplete.add((project, unit, source))
        finally:
            note(f'条目结束：{origin}；状态 {record.get("state", "未完成")}；{record.get("detail", "")}')
    for source in sources:
        emit(source['path'], source['project'], source['origin'],
             budget={'bytes': MAX_ARCHIVE_EXPANDED_BYTES, 'files': MAX_ARCHIVE_ENTRIES})
    unavailable = sum(r['state'] != '本地文件可分析' for r in inventory)
    by_group = defaultdict(list)
    note(f'开始生成焊口并分类汇总：管线候选 {len(pipeline_models)} 条；PCF使用设备连接v5及260818预制率算法；IDF分类规则 {prefab_rule}')
    for key, value in pipeline_models.items():
        by_group[key[:3]].append(value)
    for key, models in by_group.items():
        project, unit, source = key
        record = dict.fromkeys(METRICS)
        valid_models = [item for item in models if not item['conflict']]
        record['管线数'] = len(valid_models) if valid_models else None
        rows = []
        for item in models:
            if item['conflict']:
                continue
            note(f'生成/读取焊口并分类：{source} {item.get("origin", "")}；单元 {unit}')
            parsed_rows = item['rows'] if 'rows' in item else model_weld_rows(item['model'], source)
            for row in parsed_rows:
                # Unknown bore never contributes a false zero diameter-inch quantity.
                nps = row.get('寸径')
                nps = float(nps) if isinstance(nps, (float, int)) and math.isfinite(nps) and nps > 0 else None
                rows.append((row, nps, classify(row, prefab_rule)))
                weld_details.append({**row, '项目': project, '单元': unit, '来源格式': source,
                                     '原文件': item['origin'], '分类': classify(row, prefab_rule) or '未分类'})
        invalid = key in incomplete or (project, '未识别单元', source) in incomplete
        if valid_models:
            record['焊缝数'] = len(rows)
            known_bores = [v for _, v, _ in rows if v is not None]
            record['寸口量'] = round(sum(known_bores), 3) if known_bores or not rows else None
            record['未分类焊缝数'] = sum(kind is None for _, _, kind in rows)
            record['口径未知焊缝数'] = sum(nps is None for _, nps, _ in rows)
            for kind, count, amount in (('shop', '预制焊缝数', '预制寸口量'), ('field', '安装焊缝数', '安装寸口量')):
                chosen = [n for _, n, k in rows if k == kind]
                if chosen or record['未分类焊缝数'] == 0:
                    record[count] = len(chosen)
                    known = [n for n in chosen if n is not None]
                    record[amount] = round(sum(known), 3) if known or not chosen else None
        record['依据'] = f'{source}下载文件逐管线解析；分类规则={prefab_rule}；' + ('存在失败或版本冲突，仅统计成功解析且无冲突的管线' if invalid else '不含螺纹连接、不运行定尺切管算法；预制/安装规则估算' if prefab_rule != 'none' else '仅使用原文件明确分类，未知留空')
        record['状态'] = '有效解析小计；有失败或冲突待处理' if invalid else '已解析；含规则估算' if prefab_rule != 'none' else '已解析；已分类小计，部分焊口待分类' if any(k is None for _, _, k in rows) else '已解析；原文件分类'
        if source == 'PCF':
            record['依据'] = 'PCF逐管线几何焊口；设备连接读取v5 + 260818预制率算法；原PCF预制/安装标记不参与分类；不含螺纹连接'
            record['状态'] = '有效解析小计；有失败或冲突待处理' if invalid else '已解析；260818规则分类'
        if valid_models:
            record['依据'] += (f"；预制/安装为分类小计；未分类焊口{record['未分类焊缝数']}个"
                               f"；口径未知焊口{record['口径未知焊缝数']}个，寸口仅累加已知口径")
        if unavailable:
            record['状态'] = '局部结果；有台账文件不可用'
            record['依据'] += f'；仅本地可分析范围，另有{unavailable}个台账候选不可用'
        groups[key] = record
        note(f'分类小计：{project}/{unit}/{source}；有效管线 {len(valid_models)}；焊口 {len(rows)}；预制 {record.get("预制焊缝数")}；安装 {record.get("安装焊缝数")}；未分类 {record.get("未分类焊缝数")}；None 表示未知')
    for project, unit, source in incomplete:
        groups.setdefault((project, unit, source), {**dict.fromkeys(METRICS), '依据': '存在分析失败；汇总留空', '状态': '分析失败，待处理'})
    pipeline_results = [dict(project=key[0], unit=key[1], source=key[2], pipeline=key[3],
                             sha256=item['sha256'], origin=item['origin'], conflict=item['conflict'],
                             rows=item['rows'] if 'rows' in item else model_weld_rows(item['model'], key[2]))
                        for key, item in pipeline_models.items()]
    file_inventory = discovered_files(audit, inventory)
    report = {'network_accessed': False, 'prefab_rule': prefab_rule,
              'pcf_classification_engine': 'pcf-equipment-v5+prefab-260818-v1', 'unavailable_local_files': unavailable,
              'file_inventory': file_inventory,
              'pipeline_results': pipeline_results, 'incomplete': sorted(incomplete),
              'groups': [{'project': p, 'unit': u, 'source': s, **v} for (p, u, s), v in sorted(groups.items())],
              'ledger_inventory': inventory, 'files': audit, 'welds': weld_details, 'archive_members': archive_audit,
              'notes': ['IDF单独汇总，不混入PCF指标。', '仅统计本地可读取文件；台账可见的服务器ZIP不代表已检查包内文件。',
                        '单线图PDF和SHA内容解析尚未接入。', '按逐管线连接拓扑生成焊缝；PCF使用设备连接v5及260818预制率算法，IDF保留原分类口径；分类不是施工实绩。']}
    target = run / 'analysis.json'
    if sources_override is None:
        export_base = base_dir or (Path(database).resolve().parent.parent if database else output)
        export_weld_table(report, export_base)
    note(f'保存解析结果：{target}')
    target.write_text(json.dumps(report, ensure_ascii=False, indent=2), encoding='utf-8')
    return groups, report, target


def export_weld_table(report, base_dir):
    """Publish exactly the deduplicated, non-conflicting rows used by totals."""
    from paths import exports_dir
    target = exports_dir(base_dir) / '焊口表.csv'
    fields = ['项目', '单元', '来源格式', '原文件', *pcf.PCF_WELD_TABLE_FIELDS,
              '焊点类型', '分类', '分类规则', '分类引擎', '原始焊口位置', '规则执行轨迹', '汇总状态']
    complete = report.get('analysis_complete', not (
        report.get('incomplete') or report.get('unavailable_local_files')))
    state = '完整' if complete else '部分结果；待解析或核对'
    temporary = None
    try:
        with tempfile.NamedTemporaryFile(mode='w', encoding='utf-8-sig', newline='',
                                         dir=target.parent, suffix='.tmp', delete=False) as stream:
            temporary = Path(stream.name)
            writer = csv.DictWriter(stream, fieldnames=fields, extrasaction='ignore')
            writer.writeheader()
            for row in report.get('welds', []):
                writer.writerow({**row, '焊点类型': {'shop': 'S', 'field': 'F'}.get(row.get('分类'), ''),
                    '分类规则': row.get('_classification_rule', ''),
                    '分类引擎': row.get('_classification_engine', ''),
                    '原始焊口位置': row.get('_location_original', row.get('_location')),
                    '规则执行轨迹': json.dumps(row.get('_classification_trace', []), ensure_ascii=False),
                    '汇总状态': state})
        os.replace(temporary, target)
    finally:
        if temporary is not None:
            temporary.unlink(missing_ok=True)
    report['weld_table_file'] = str(target)
    return target


def discovered_files(audit, ledger_inventory=()):
    """One row per current source/member path, regardless of parsing success.

    Distinct paths count as separate files even when the parser reuses content.
    A duplicate ZIP member removed by extraction has no emitted path and counts once.
    """
    from business_type import classify_pdf, pdf_unit
    rows = {}
    for item in audit:
        suffix = item['format'].lower()
        if suffix not in ('.pcf', '.idf', '.pdf', '.sha'):
            continue
        name = item['origin'].split('!/')[-1].split(' / ')[0]
        kind, basis = classify_pdf(name) if suffix == '.pdf' else (suffix[1:].upper(), '文件扩展名')
        identity = (item['project'], item['origin'])
        file_unit = pdf_unit(name, item['unit']) if suffix == '.pdf' else item['unit']
        rows[identity] = dict(project=item['project'], unit=file_unit, origin=item['origin'],
                              kind=kind, basis=basis, sha256=item.get('sha256'), state=item['state'])
    # A downloaded loose file is countable before its content-analysis job runs.
    for item in ledger_inventory:
        origin = item['filename'] + ' / ' + str(item.get('resolved_path', item.get('path', '')))
        identity = (item['project'], origin)
        suffix = Path(item['filename']).suffix.lower()
        if (identity in rows or item['status'] != 'done' or item.get('state') != '本地文件可分析'
                or suffix not in ('.pcf', '.idf', '.pdf', '.sha')):
            continue
        kind, basis = classify_pdf(item['filename']) if suffix == '.pdf' else (suffix[1:].upper(), '文件扩展名')
        file_unit = unit_from(item['filename'], item.get('path'))
        if suffix == '.pdf':
            file_unit = pdf_unit(item['filename'], file_unit)
        rows[identity] = dict(project=item['project'], unit=file_unit,
            origin=origin, kind=kind, basis=basis, sha256=item.get('sha256'), state='台账下载完成；' + item.get('analysis_state', item['state']))
    return list(rows.values())


def main(argv=None):
    parser = argparse.ArgumentParser(description='离线分析本地ZIP/PCF/IDF，不下载、不登录')
    parser.add_argument('--database', type=Path, default=HOME / 'runtime/ledger.sqlite3')
    parser.add_argument('--root', type=Path, help='可选：分析工具外的独立样本目录；正常使用自动读取工具downloads目录内的台账文件，无需填写')
    parser.add_argument('--base-dir', type=Path, help='台账对应的工具根目录')
    parser.add_argument('--project', default='本地项目')
    parser.add_argument('--output', type=Path)
    parser.add_argument('--prefab-rule', choices=['none', 'no-small', 'all'], default='none')
    args = parser.parse_args(argv)
    groups, report, target = analyze(database=args.database, root=args.root, project=args.project,
                                     output=args.output, base_dir=args.base_dir, prefab_rule=args.prefab_rule)
    print('Local analysis: ' + str(target))
    print(f"Groups: {len(groups)}; inspected files: {len(report['files'])}; ledger candidates: {len(report['ledger_inventory'])}")


if __name__ == '__main__':
    main()
