import json
import os
from pathlib import Path
import sys
from unittest.mock import Mock, patch

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from file_snapshot_cache import FileSnapshotCache
from ima_control import report_snapshot, REPORT_CACHE


def test_unchanged_file_loads_once_and_same_size_rewrite_invalidates(tmp_path):
    path = tmp_path / 'report.json'
    path.write_text('{"a":1}', encoding='utf-8')
    cache = FileSnapshotCache()
    load = Mock(side_effect=lambda: json.loads(path.read_text(encoding='utf-8')))
    assert cache.get(path, load) == {'a': 1}
    assert cache.get(path, load) == {'a': 1}
    assert load.call_count == 1
    stamp = path.stat()
    other = tmp_path / 'replacement'
    other.write_text('{"a":2}', encoding='utf-8')
    os.utime(other, ns=(stamp.st_atime_ns, stamp.st_mtime_ns))
    other.replace(path)
    assert cache.get(path, load) == {'a': 2}
    assert load.call_count == 2


def test_cache_is_bounded_and_failed_load_is_not_cached(tmp_path):
    cache = FileSnapshotCache(entries=2, bytes_limit=8)
    for name in ('one', 'two', 'three'):
        path = tmp_path / name
        path.write_text('1234')
        cache.get(path, path.read_text)
    assert len(cache.values) == 2 and cache.bytes == 8
    path.write_text('{invalid')
    import pytest
    with pytest.raises(json.JSONDecodeError):
        cache.get(path, lambda: json.loads(path.read_text()))
    path.write_text('{}')
    assert cache.get(path, lambda: json.loads(path.read_text())) == {}


def test_ima_summary_and_paging_share_one_report_parse(tmp_path):
    path = tmp_path / 'latest-plan.json'
    path.write_text(json.dumps({'files': [{'status': 'done'}] * 5000}), encoding='utf-8')
    with patch('ima_control.json.loads', wraps=json.loads) as parse:
        raw, summary = report_snapshot(path)
        again, _ = report_snapshot(path)
        assert len(raw['files']) == 5000 and again is raw
        assert summary['counts'] == {'done': 5000}
        assert len(summary['files']) == 20
        assert parse.call_count == 1
