import unittest
from agy_stream import normalize
import agy_report as report
import agy_live as live
import agy_agent as agent

class AdapterTests(unittest.TestCase):
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
    def test_review_no_write_or_command(self):
        cfg=agent.agent_config('a','review',{},'p')
        self.assertEqual(cfg['tools'],['view_file','grep_search'])
        self.assertEqual(cfg['commandExecutionPolicy'],'off')

if __name__=='__main__':unittest.main()
