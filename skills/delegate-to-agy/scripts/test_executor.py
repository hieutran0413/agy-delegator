import unittest
from pathlib import Path
from execution_gate import decide
from shared_executor import start,run_dir
class GateTests(unittest.TestCase):
 def test_native_execution_blocked(self):
  for name in ['run_command','send_command_input','notebook_execution','invoke_subagent']:
   self.assertEqual(decide({'toolCall':{'name':name,'args':{}}})['decision'],'deny')
 def test_policy_write_blocked(self):
  self.assertEqual(decide({'toolCall':{'name':'write_to_file','args':{'TargetFile':str(Path.home()/'.local/share/agy-executor/policy.json')}}})['decision'],'deny')
 def test_untrusted_mcp_blocked(self):
  self.assertEqual(decide({'toolCall':{'name':'call_mcp_tool','args':{'ServerName':'computer-use'}}})['decision'],'deny')
 def test_bridge_uses_normal_permission(self):
  self.assertEqual(decide({'toolCall':{'name':'call_mcp_tool','args':{'ServerName':'shared-executor'}}})['decision'],'ask')
 def test_unknown_action_blocked(self):
  with self.assertRaises(ValueError):start('/tmp','not-configured-test')
 def test_invalid_run_path(self):
  with self.assertRaises(ValueError):run_dir('../../etc')
if __name__=='__main__':unittest.main()
