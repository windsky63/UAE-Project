from pathlib import Path
import json
import sqlite3
from unittest.mock import patch
import pytest
from tests import test_analysis_worker as fixtures
from snapshot_service import cached_snapshot

@pytest.fixture
def case():
    value=fixtures.WorkerTests('test_unchanged_files_are_cached_and_report_never_reextracts')
    value.setUp()
    try:
        value.publish()
        value.queue.discover()
        value.queue.process_next()
        yield value
    finally:
        value.doCleanups()

def summary(case, folder='summary'):
    return cached_snapshot(case.database,output=case.base/folder,export_welds=False)


def test_rules_path_migration_reuses_completed_pre_migration_results():
    value = fixtures.WorkerTests('test_unchanged_files_are_cached_and_report_never_reextracts')
    value.setUp()
    try:
        # This is the identity used by servers before the data-home path fix.
        value.queue.version = '86322db74efe68880834a94e7b0125041df33b4c82a369be37d237ad1af4aced'
        value.publish()
        value.queue.discover()
        value.queue.process_next()
        with patch('local_analysis.parse_file', side_effect=AssertionError('reparsed source')):
            groups, report, _ = summary(value)
        assert report['completed_analysis_files'] == 1
        assert report['pending_analysis_files'] == 0
        assert report['analysis_complete']
        assert groups['p','H4-262','PCF']['焊缝数'] == 1
    finally:
        value.doCleanups()

def test_worker_stores_full_result_transactionally(case):
    row=case.queue.db.execute('SELECT j.state,j.updated,r.job_updated,r.payload FROM jobs j JOIN analysis_reports r ON r.job_key=j.key').fetchone()
    assert row['state']=='done'
    assert row['updated']==row['job_updated']
    assert json.loads(row['payload'])['pipeline_results']

def test_database_summary_never_opens_per_file_json(case):
    job=case.queue.db.execute('SELECT result_path FROM jobs').fetchone()
    path=(case.queue.cache/job[0]).resolve()
    read=Path.read_text
    def guarded(file,*args,**kwargs):
        assert file.resolve()!=path, 'opened per-file JSON instead of database'
        return read(file,*args,**kwargs)
    with patch.object(Path,'read_text',guarded),patch('local_analysis.parse_file',side_effect=AssertionError('reparsed source')):
        groups,report,_=summary(case)
    assert groups['p','H4-262','PCF']['焊缝数']==1
    assert report['analysis_complete']

def test_legacy_json_is_imported_once_and_survives_audit_file_removal(case):
    with case.queue.db:
        case.queue.db.execute('DELETE FROM analysis_reports')
    first=summary(case)
    assert case.queue.db.execute('SELECT COUNT(*) FROM analysis_reports').fetchone()[0]==1
    for row in case.queue.db.execute('SELECT result_path FROM jobs'):
        (case.queue.cache/row[0]).unlink()
    second=summary(case,'another-summary')
    assert first[0]==second[0]
    assert first[1]['welds']==second[1]['welds']
    assert second[1]['analysis_complete']

def test_each_generation_reads_database_and_aggregates_fresh(case):
    first=summary(case)
    from snapshot_service import analyze
    with patch('snapshot_service.analyze',wraps=analyze) as merge:
        second=summary(case)
    merge.assert_called_once()
    assert first[0]==second[0]
    assert first[2]!=second[2]
    assert not (case.base/'summary/.summary-cache.json').exists()


def test_new_file_is_pending_until_new_database_result_commits(case):
    summary(case)
    case.publish('two',fixtures.PCF.replace('L001','L002'))
    case.queue.discover()
    _,pending,_=summary(case)
    assert not pending['analysis_complete']
    assert pending['pending_analysis_files']==1
    case.queue.process_next()
    groups,complete,_=summary(case)
    assert complete['analysis_complete']
    assert groups['p','H4-262','PCF']['管线数']==2

def test_removed_source_is_excluded_even_if_database_result_exists(case):
    summary(case)
    (case.downloads/'one.zip').unlink()
    groups,report,_=summary(case)
    assert not groups
    assert report['unavailable_local_files']==1
    assert not report['analysis_complete']

def test_database_payload_corruption_cannot_be_published_as_complete(case):
    with case.queue.db:
        case.queue.db.execute("UPDATE analysis_reports SET payload='{}'")
    _,report,_=summary(case)
    assert not report['analysis_complete']
    assert report['failed_analysis_files']==1

def test_db_aggregation_preserves_original_result_semantics(case):
    from local_analysis import analyze
    row=case.queue.db.execute('SELECT result_path FROM jobs').fetchone()
    original=json.loads((case.queue.cache/row[0]).read_text(encoding='utf-8'))
    groups,report,_=summary(case)
    assert report['welds']==original['welds']
    assert report['pipeline_results']==original['pipeline_results']
    assert report['files']==original['files']
    assert report['file_inventory']==original['file_inventory']
    assert report['analysis_complete']


def test_explicit_migration_does_not_parse_sources_and_is_repeatable(case):
    from import_result_cache import migrate
    with case.queue.db:
        case.queue.db.execute('DELETE FROM analysis_reports')
    with patch('local_analysis.parse_file',side_effect=AssertionError('reparsed source')):
        first=migrate(case.base,lambda message:None)
        second=migrate(case.base,lambda message:None)
    assert first==dict(imported=1,failed=0)
    assert second==dict(imported=0,failed=0)
    assert summary(case)[1]['analysis_complete']


def test_legacy_import_cannot_overwrite_newer_worker_commit(case):
    from result_store import import_reports,encode_report
    job=case.queue.db.execute('SELECT * FROM jobs').fetchone()
    payload,sha=encode_report(dict(files=[],pipeline_results=[]))
    with case.queue.db:
        case.queue.db.execute('UPDATE jobs SET updated=updated+1')
    import_reports(case.queue.db,[(job['key'],job['result_path'],job['updated'],payload,sha)])
    row=case.queue.db.execute('SELECT payload FROM analysis_reports').fetchone()
    assert json.loads(row[0])['pipeline_results']


def test_malformed_legacy_report_is_not_imported_or_marked_complete(case):
    row=case.queue.db.execute('SELECT result_path FROM jobs').fetchone()
    with case.queue.db:
        case.queue.db.execute('DELETE FROM analysis_reports')
    (case.queue.cache/row[0]).write_text('{"files":"invalid","pipeline_results":[]}',encoding='utf-8')
    _,report,_=summary(case)
    assert not report['analysis_complete']
    assert report['failed_analysis_files']==1
    assert case.queue.db.execute('SELECT COUNT(*) FROM analysis_reports').fetchone()[0]==0
