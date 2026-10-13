import importlib,importlib.util,tempfile,unittest
from pathlib import Path
from unittest.mock import patch
import semantic_search_jobs as jobs

class QueueSyncTests(unittest.TestCase):
 def setUp(self):
  self.temp=tempfile.TemporaryDirectory();self.addCleanup(self.temp.cleanup);self.root=Path(self.temp.name)
 def module(self):
  self.assertIsNotNone(importlib.util.find_spec('queue_sync'))
  return importlib.import_module('queue_sync')
 def test_other_file_is_never_staged(self):
  mod=self.module();p=self.root/'Private/secret.json';p.parent.mkdir();p.write_text('{}')
  with patch.object(mod,'_git') as git:self.assertFalse(mod.sync_paths(self.root,[p]))
  git.assert_not_called()
 def test_valid_request_stages_only_requested_file(self):
  mod=self.module();job=jobs.enqueue(self.root,'найти идею');p=jobs.request_path(self.root,job['id'])
  calls=[]
  def git(args,root):calls.append(args);return 0
  with patch.object(mod,'_git',side_effect=git):self.assertTrue(mod.sync_paths(self.root,[p]))
  adds=[x for x in calls if x[:2]==['git','add']]
  self.assertEqual(adds,[['git','add','--',str(p)]])
