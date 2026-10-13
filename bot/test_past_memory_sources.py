import tempfile,unittest
from pathlib import Path
import memory_history,thought_digest,thought_feed,personal_search
class PastSourceTests(unittest.TestCase):
 def setUp(self):
  self.tmp=tempfile.TemporaryDirectory();self.addCleanup(self.tmp.cleanup);self.vault=Path(self.tmp.name)
  folder=self.vault/'ZETA/FLEETING_DAILY';folder.mkdir(parents=True)
  identities=[]
  for name,text in [('Old','хочу развивать генератор'),('New','решил развивать рассылку')]:
   path=folder/(name+'.md');path.write_text('# '+name+'\n\n## Мысль\n'+text)
   identities.append(thought_feed._identity_text(path.relative_to(self.vault).as_posix(),path.read_text()))
  self.old,self.new=identities;proposal=memory_history.prepare_supersede(self.vault,self.old,self.new);memory_history.commit_supersede(self.vault,proposal['id'])
 def test_digest_omits_past_source(self):
  sources=thought_digest.collect(self.vault,'recent')['sources']
  self.assertNotIn(self.old,[x['identity'] for x in sources])
 def test_current_search_omits_past_and_explicit_history_can_find_it(self):
  self.assertEqual(personal_search.search('генератор',vault=self.vault),[])
  self.assertTrue(personal_search.search('раньше генератор',vault=self.vault))
