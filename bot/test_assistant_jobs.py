import importlib,importlib.util,os,tempfile,unittest
from pathlib import Path
from unittest.mock import patch
import semantic_search_jobs as jobs

class AssistantJobsTests(unittest.TestCase):
 def setUp(self):
  self.temp=tempfile.TemporaryDirectory();self.addCleanup(self.temp.cleanup);self.vault=Path(self.temp.name)
  env=patch.dict(os.environ,{'DUSK_AI_USAGE_DB':str(self.vault/'local/usage.sqlite3')});env.start();self.addCleanup(env.stop)
 def module(self):
  self.assertIsNotNone(importlib.util.find_spec('assistant_jobs'))
  return importlib.import_module('assistant_jobs')
 def test_processes_semantic_job_with_injected_search_and_syncs_result(self):
  mod=self.module();job=jobs.enqueue(self.vault,'идея по смыслу')
  def search(root,query):return {'hits':[],'coverage':{},'error':''}
  with patch.object(mod,'_dirty',return_value=[]),patch.object(mod.queue_sync,'sync_paths',return_value=True) as sync:
   self.assertTrue(mod.run(self.vault,search=search))
  self.assertIsNotNone(jobs.get_result(self.vault,job['id']))
  self.assertIn(jobs.result_path(self.vault,job['id']),sync.call_args.args[1])
 def test_failed_sync_is_not_reported_success(self):
  mod=self.module()
  with patch.object(mod,'_dirty',return_value=[]),patch.object(mod.queue_sync,'sync_paths',return_value=False):
   self.assertFalse(mod.run(self.vault))
