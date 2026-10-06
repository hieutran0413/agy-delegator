import unittest
import tempfile
import threading
import http.client
from pathlib import Path
from unittest.mock import patch
from agy_stream import normalize
import agy_report as report
import agy_live as live
import agy_agent as agent

class AdapterTests(unittest.TestCase):
    def test_quota_fallback_order_is_bounded_and_only_for_quota(self):
        error = 'Individual quota reached. Resets in 4h.'
        self.assertEqual(live.next_quota_model('claude-opus-4-6-thinking', error), 'claude-sonnet-4-6')
        self.assertEqual(live.next_quota_model('claude-sonnet-4-6', error), 'gemini-3.8-flash-high')
        self.assertIsNone(live.next_quota_model('gemini-3.8-flash-high', error))
        self.assertIsNone(live.next_quota_model('claude-opus-4-6-thinking', 'Permission denied'))

    def test_quota_fallback_continues_and_never_launches_fresh_session(self):
        with tempfile.TemporaryDirectory() as temporary, patch.object(live, 'ROOT', Path(temporary)):
            directory = live.job_dir('job-quota');directory.mkdir(parents=True)
            metadata = {'autoQuotaFallback':True, 'model':'claude-opus-4-6-thinking'}
            live.write_json(directory/'job.json', metadata)
            status = {'state':'failed','done':True,'error':'Individual quota reached'}
            with patch.object(live, 'continue_job', return_value={'jobId':'job-next'}) as continuation, patch.object(live, 'launch', side_effect=AssertionError('fresh launch')):
                self.assertEqual(live.auto_quota_fallback('job-quota', status)['jobId'], 'job-next')
            self.assertEqual(continuation.call_args.kwargs['fallback_model'], 'claude-sonnet-4-6')
            self.assertEqual(continuation.call_args.args[2], 'auto-quota-fallback')
            metadata.pop('autoQuotaFallback');live.write_json(directory/'job.json', metadata)
            with patch.object(live, 'continue_job') as continuation:
                self.assertIsNone(live.auto_quota_fallback('job-quota', status))
                continuation.assert_not_called()

    def test_failed_resume_stops_automatic_retries(self):
        with tempfile.TemporaryDirectory() as temporary, patch.object(live, 'ROOT', Path(temporary)):
            directory = live.job_dir('job-quota');directory.mkdir(parents=True)
            live.write_json(directory/'job.json', {'autoQuotaFallback':True,'model':'claude-sonnet-4-6'})
            status = {'state':'failed','done':True,'error':'Individual quota reached'}
            with patch.object(live, 'continue_job', side_effect=RuntimeError('Session unavailable')) as continuation:
                self.assertIsNone(live.auto_quota_fallback('job-quota', status))
                self.assertIsNone(live.auto_quota_fallback('job-quota', status))
                self.assertEqual(continuation.call_count, 1)
            self.assertEqual(live.read_json(directory/'job.json')['autoFallbackError'], 'Session unavailable')

    def test_model_switch_keeps_conversation_agent_and_file_scope(self):
        for old_model, new_model, effort in (
            ('claude-opus-4-6-thinking','claude-sonnet-4-6',None),
            ('claude-sonnet-4-6','gemini-3.8-flash-high','high'),
        ):
            with self.subTest(model=new_model), tempfile.TemporaryDirectory() as temporary, patch.object(live, 'ROOT', Path(temporary)):
                parent = live.job_dir('job-quota');parent.mkdir(parents=True)
                live.write_json(parent/'job.json', {'id':'job-quota','mode':'implement','model':old_model,
                    'effort':None,'workspace':temporary,'agent':'original-agent','writeScope':['a.py'],
                    'originalBrief':'Finish a.py','turn':1,'rootTaskId':'job-quota'})
                captured = []
                def fake_launch(args):
                    captured.append(args)
                    child = live.job_dir('job-next');child.mkdir()
                    live.write_json(child/'job.json', {'id':'job-next','resumeSessionId':args.resume_session_id})
                    return {'jobId':'job-next','writeScope':['a.py'],'state':'running'}
                status = {'state':'failed','done':True,'error':'Individual quota reached'}
                with patch.object(live, 'snapshot', return_value=status), patch.object(live, 'steer_files', return_value=['a.py']), patch.object(live, 'resumable_session', return_value='original-session'), patch.object(live, '_active_session_job', return_value=None), patch.object(live, 'launch', side_effect=fake_launch):
                    result = live.continue_job('job-quota','Continue','auto-quota-fallback',fallback_model=new_model)
                self.assertTrue(result['sameSession'])
                self.assertEqual(captured[0].resume_session_id, 'original-session')
                self.assertEqual(captured[0].resume_agent, 'original-agent')
                self.assertEqual(captured[0].files, ['a.py'])
                self.assertEqual(captured[0].model, new_model)
                self.assertEqual(captured[0].effort, effort)

    def test_catalog_does_not_probe_completed_jobs(self):
        with tempfile.TemporaryDirectory() as temporary, patch.object(live, 'ROOT', Path(temporary)):
            directory = live.job_dir('job-old')
            directory.mkdir(parents=True)
            live.write_json(directory/'job.json', {'id':'job-old','state':'finished'})
            live.write_json(directory/'result.json', {'state':'finished','sessionId':'session-old'})
            with patch.object(live, 'snapshot', side_effect=AssertionError('history was probed')):
                item = live.job_catalog()[0]
            self.assertEqual(item['state'], 'finished')
            self.assertEqual(item['sessionId'], 'session-old')

    def test_health_and_assets_do_not_run_cleanup(self):
        server = live.ThreadingHTTPServer(('127.0.0.1', 0), live.Handler)
        thread = threading.Thread(target=server.serve_forever, kwargs={'poll_interval':0.05}, daemon=True)
        thread.start()
        try:
            with patch.object(live, 'cleanup_finished_jobs', side_effect=AssertionError('cleanup blocked HTTP')):
                for path in ('/health', '/'):
                    connection = http.client.HTTPConnection('127.0.0.1', server.server_port, timeout=2)
                    try:
                        connection.request('GET', path)
                        response = connection.getresponse()
                        response.read()
                        self.assertEqual(response.status, 200)
                    finally:
                        connection.close()
        finally:
            server.shutdown()
            server.server_close()
            thread.join(timeout=2)

    def test_success_report(self):
        e = normalize({'event':'result','result':{'status':'SUCCESS','response':'Full report','conversation_id':'id'}})
        self.assertEqual(report.summarize([{'event':e}])['report'],'Full report')
        self.assertEqual(e['sessionId'],'id')
    def test_empty_is_failure(self):
        self.assertEqual(normalize({'event':'result','result':{'status':'SUCCESS','response':''}})['type'],'runError')
    def test_denial_is_failure(self):
        self.assertEqual(normalize({'event':'result','result':{'status':'SUCCESS','response':'partial','denied_actions':['write_file']}})['type'],'runError')
    def test_tool_paths(self):
        e=normalize({'event':'step_update','step_update':{'step_type':'tool','state':'DONE','step_index':2,'tool_name':'replace_file_content','tool_info':{'parameters':{'TargetFile':'/tmp/a'}}}})
        self.assertEqual(report.summarize([{'event':e}])['changedFiles'],['/tmp/a'])
    def test_resume_exact(self):
        cmd=live.worker_command({'mode':'review','agent':'a','model':'m','effort':'low','prompt':'p','resumeSessionId':'abc'})
        self.assertIn('--conversation',cmd)
        self.assertNotIn('--continue',cmd)
    def test_thinking_model_omits_effort(self):
        cmd=live.worker_command({'mode':'implement','agent':'a','model':'claude-opus-4-6-thinking','effort':None,'prompt':'p'})
        self.assertNotIn('--effort',cmd)
        self.assertIsNone(live.resolve_effort('claude-opus-4-6-thinking', None))
        self.assertIsNone(live.resolve_effort('claude-sonnet-4-6', None, 'Claude Sonnet 4.6 (Thinking)'))
    def test_regular_models_keep_low_effort_default(self):
        self.assertEqual(live.resolve_effort('gemini-3.8-flash-low', None), 'low')
    def test_thinking_model_rejects_explicit_effort(self):
        with self.assertRaisesRegex(ValueError, 'does not accept --effort'):
            live.resolve_effort('claude-opus-4-6-thinking', 'low')
    def test_review_no_write_or_command(self):
        cfg=agent.agent_config('a','review',{},'p')
        self.assertEqual(cfg['tools'],['view_file','grep_search'])
        self.assertEqual(cfg['commandExecutionPolicy'],'off')

if __name__=='__main__':unittest.main()
