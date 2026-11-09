"""Selection and failure propagation, without launching project services."""
import ast
import importlib.util
import io
import json
from pathlib import Path
import subprocess
import sys
import tempfile
import unittest
from unittest.mock import patch

SOURCE = Path(__file__).resolve().parents[1] / 'run_tests.py'
spec = importlib.util.spec_from_file_location('scoped_test_runner', SOURCE)
runner = importlib.util.module_from_spec(spec)
spec.loader.exec_module(runner)


class RunnerTests(unittest.TestCase):
    def setUp(self):
        temp = tempfile.TemporaryDirectory()
        self.addCleanup(temp.cleanup)
        self.root = Path(temp.name)
        for name in ('part/tests/test_one.py','part/tests/test_two.py','other/tests/test_other.py'):
            path = self.root / name
            path.parent.mkdir(parents=True, exist_ok=True)
            path.write_text('def test_a(): pass\n', encoding='utf-8')
        self.config = dict(components={
            'one':dict(directory='part', full=['tests/test_*.py']),
            'two':dict(directory='other', full=['tests/test_*.py'])}, areas={
            'quick':dict(tests={'one':['tests/test_one.py']}),
            'feature':dict(sources=['part/core.py'],tests={'one':['tests/test_one.py::test_a']}),
            'overlap':dict(sources=['part/*.py'],tests={'one':['tests/test_one.py']})})

    def test_default_is_explicitly_scoped_quick_checks(self):
        plan=runner.select_plan(self.config,root=self.root)
        self.assertEqual(plan['scope'],'scoped')
        self.assertEqual(plan['areas'],['quick'])
        self.assertEqual(plan['components'],{'one':['tests/test_one.py']})

    def test_overlapping_area_selections_do_not_run_nodes_twice(self):
        plan=runner.select_plan(self.config,root=self.root,changed=['part/core.py'])
        self.assertEqual(plan['areas'],['feature','overlap'])
        self.assertEqual(plan['components']['one'],['tests/test_one.py'])

    def test_unknown_production_code_falls_back_to_full_component(self):
        plan=runner.select_plan(self.config,root=self.root,changed=['other/new.py'])
        self.assertEqual(plan['components'],{'two':['tests/test_other.py']})
        self.assertIn('no scoped mapping',plan['notes'][0])

    def test_unknown_shared_code_falls_back_to_all_components(self):
        plan=runner.select_plan(self.config,root=self.root,changed=['root_launcher.py'])
        self.assertEqual(set(plan['components']),{'one','two'})

    def test_dependency_files_are_not_mistaken_for_documentation(self):
        plan=runner.select_plan(self.config,root=self.root,changed=['part/requirements.txt'])
        self.assertEqual(plan['components']['one'],['tests/test_one.py','tests/test_two.py'])

    def test_test_change_selects_itself_and_component_filter_is_explicit(self):
        plan=runner.select_plan(self.config,root=self.root,changed=['part/tests/test_two.py'])
        self.assertEqual(plan['components'],{'one':['tests/test_two.py']})
        full=runner.select_plan(self.config,root=self.root,full=True,components=['two'])
        self.assertEqual(full['scope'],'full')
        self.assertEqual(full['components'],{'two':['tests/test_other.py']})

    def test_missing_targets_and_outside_paths_fail_before_execution(self):
        with self.assertRaises(ValueError):
            runner.expand_targets(self.root,['missing/test_*.py'])
        with self.assertRaises(ValueError):
            runner.normalize_changed(self.root,['../outside.py'])

    def test_failure_exit_code_is_preserved_with_utf8_output(self):
        output=self.root/'reports';output.mkdir()
        def fail(command, **kwargs):
            kwargs['stdout'].write('明确失败原因\n1 failed in 0.01s\n')
            self.assertEqual(kwargs['cwd'],self.root/'part')
            self.assertEqual(kwargs['env']['PYTHONIOENCODING'],'utf-8')
            return subprocess.CompletedProcess(command,1)
        with patch.object(runner.subprocess,'run',side_effect=fail):
            result=runner.execute_component('one',['tests/test_one.py'],self.config['components']['one'],
                                            Path(sys.executable),output,5,root=self.root)
        self.assertEqual(result['exit_code'],1)
        self.assertIn('1 failed',result['summary'])
        self.assertIn('明确失败原因',Path(result['log']).read_text(encoding='utf-8'))

    def test_junit_report_lists_expensive_checks(self):
        path=self.root/'suite.xml'
        path.write_text('<testsuites><testsuite><testcase classname="suite" name="slow" time="2.5"/></testsuite></testsuites>')
        report=runner.junit_details(path)
        self.assertEqual(report['junit_cases'],1)
        self.assertEqual(report['slowest'][0]['seconds'],2.5)

    def test_legacy_log_tools_delegates_to_the_same_quick_or_full_runner(self):
        config=runner.load_config()
        source=runner.ROOT/config['components']['manager']['directory']/'log_tools.py'
        spec=importlib.util.spec_from_file_location('legacy_test_log_tools',source)
        module=importlib.util.module_from_spec(spec)
        spec.loader.exec_module(module)
        class Child:
            stdout=io.StringIO('fixture passed\n')
            def wait(self, **kwargs):return 0
            def poll(self):return 0
        for full in (False,True):
            Child.stdout=io.StringIO('fixture passed\n')
            with patch.object(module.subprocess,'Popen',return_value=Child()) as launch, \
                 patch.object(module.sys,'stdout',io.StringIO()):
                result=module.run_tests('manager',self.root/'legacy.log',full=full)
            self.assertEqual(result,0)
            command=launch.call_args.args[0]
            self.assertIn('run_tests.py',str(command[1]))
            self.assertEqual('--full' in command,full)
            self.assertEqual(command[2:4],['--component','manager'])

    def test_every_real_profile_and_node_points_to_an_existing_test(self):
        config=runner.load_config()
        for name in config['areas']:
            plan=runner.select_plan(config,areas=[name])
            self.assertTrue(plan['components'],name)
            for key,targets in plan['components'].items():
                base=runner.ROOT/config['components'][key]['directory']
                for target in targets:
                    filename,*nodes=target.split('::')
                    body=ast.parse((base/filename).read_bytes()).body
                    for node in nodes:
                        match=next((item for item in body if getattr(item,'name',None)==node),None)
                        self.assertIsNotNone(match,target)
                        body=match.body
        for definition in config['components'].values():
            self.assertTrue(runner.expand_targets(runner.ROOT/definition['directory'],definition['full']))


if __name__=='__main__':
    unittest.main()
