import datetime as dt,os,tempfile,types,unittest
from pathlib import Path
from unittest.mock import AsyncMock,Mock,patch
import telegram_bot as bot,task_ui as ui,resources,semantic_search_jobs as jobs,thought_feed,memory_history

class MemoryRecallTransportTests(unittest.IsolatedAsyncioTestCase):
 def setUp(self):
  self.temp=tempfile.TemporaryDirectory();self.addCleanup(self.temp.cleanup);self.vault=Path(self.temp.name)
  self.state={'inline_menu_ready':True};self.message=types.SimpleNamespace(reply_text=AsyncMock(),text='',message_id=1)
  self.query=types.SimpleNamespace(data='',message=self.message,answer=AsyncMock(),edit_message_text=AsyncMock())
  self.update=types.SimpleNamespace(message=self.message,effective_message=self.message,callback_query=self.query)
  self.context=types.SimpleNamespace(user_data=self.state,bot_data={})
  env=patch.dict(os.environ,{'VAULT_PATH':str(self.vault),'DUSK_AI_USAGE_DB':str(self.vault/'local/usage.sqlite3'),'DUSK_SEMANTIC_DB':str(self.vault/'local/semantic.sqlite3')});env.start();self.addCleanup(env.stop)
  for name,value in [('auth_check',lambda u:True),('_vault',lambda:self.vault),('_runtime_store',lambda:None),('_sync_note',Mock()),('_sync_assistant',Mock(return_value=True)),('append_to_inbox',Mock(return_value='captured'))]:
   p=patch.object(bot,name,value,create=True);p.start();self.addCleanup(p.stop)
 def thought(self,name,body):
  folder=self.vault/'ZETA/FLEETING_DAILY';folder.mkdir(parents=True,exist_ok=True)
  p=folder/(name+'.md');p.write_text('# '+name+'\n\n## Мысль\n'+body+'\n')
  return thought_feed._identity_text(p.relative_to(self.vault).as_posix(),p.read_text())
 def buttons(self,method):return [b.callback_data or '' for row in method.call_args.kwargs['reply_markup'].inline_keyboard for b in row]
 async def test_empty_personal_search_enqueues_semantic_without_capture(self):
  await bot.handle_content(self.update,self.context,'найди штуку для колледжей')
  self.assertEqual(len(jobs.requests(self.vault)),1)
  bot.append_to_inbox.assert_not_called()
 async def test_lexical_hit_stays_fast_and_offers_semantic_button(self):
  resources.save(self.vault,'https://example.edu','генератор документов')
  await bot.handle_content(self.update,self.context,'найди генератор документов')
  self.assertEqual(jobs.requests(self.vault),[])
  self.assertTrue(any(x.startswith('ss:q:') for x in self.buttons(self.message.reply_text)))
 async def test_explicit_semantic_callback_creates_one_job(self):
  self.query.data='ss:search'
  await bot.handle_callback(self.update,self.context)
  await bot.handle_content(self.update,self.context,'штука для колледжей')
  self.assertEqual(len(jobs.requests(self.vault)),1)
 async def test_thought_card_has_history_and_update_previous(self):
  token=ui.issue_thought_token(self.state,self.thought('Первая','идея'))
  view=ui.render_thought(self.state,self.vault,None,token)
  self.assertIn('mh:h:t:'+token,[x for row in view.rows for _,x in row])
  self.assertIn('mh:p:'+token,[x for row in view.rows for _,x in row])
 async def test_supersede_requires_visible_confirmation_and_preserves_sources(self):
  older=self.thought('Старая','первая идея');newer=self.thought('Новая','уточнённая идея')
  old=ui.issue_thought_token(self.state,older);new=ui.issue_thought_token(self.state,newer)
  self.query.data=f'mh:s:{new}:{old}';await bot.handle_callback(self.update,self.context)
  self.assertFalse(memory_history.is_past(self.vault,older))
  self.query.data=next(x for x in self.buttons(self.query.edit_message_text) if x.startswith('mh:c:'))
  await bot.handle_callback(self.update,self.context)
  self.assertTrue(memory_history.is_past(self.vault,older))
  self.assertIsNotNone(thought_feed.load_thought(self.vault,older))
  self.assertFalse(any(x.identity==older for x in thought_feed.recent_thoughts(self.vault)))
  self.query.data=next(x for x in self.buttons(self.query.edit_message_text) if x.startswith('mh:u:'))
  await bot.handle_callback(self.update,self.context)
  self.assertFalse(memory_history.is_past(self.vault,older))
 async def test_resource_card_exposes_readable_history(self):
  record=resources.save(self.vault,'https://example.edu','старое описание').resource
  resources.update(self.vault,record.id,record.version,'новое описание')
  self.query.data='mh:h:r:'+record.id;await bot.handle_callback(self.update,self.context)
  self.assertIn('старое описание',self.query.edit_message_text.call_args.args[0])
  self.assertIn('новое описание',self.query.edit_message_text.call_args.args[0])
 async def test_cancelled_proposal_cannot_commit(self):
  older=self.thought('Старая','первая идея');newer=self.thought('Новая','новая идея')
  old=ui.issue_thought_token(self.state,older);new=ui.issue_thought_token(self.state,newer)
  self.query.data=f'mh:s:{new}:{old}';await bot.handle_callback(self.update,self.context)
  confirm=next(x for x in self.buttons(self.query.edit_message_text) if x.startswith('mh:c:'))
  self.query.data='m:home';await bot.handle_callback(self.update,self.context)
  self.query.data=confirm;await bot.handle_callback(self.update,self.context)
  self.assertFalse(memory_history.is_past(self.vault,older))
 async def test_stale_pair_picker_returns_clear_message_without_approval(self):
  older=self.thought('Old','прежняя идея');newer=self.thought('New','новая идея')
  old=ui.issue_thought_token(self.state,older);new=ui.issue_thought_token(self.state,newer)
  (self.vault/'ZETA/FLEETING_DAILY/Old.md').unlink()
  self.query.data=f'mh:s:{new}:{old}';await bot.handle_callback(self.update,self.context)
  self.assertIn('изменилась',self.query.edit_message_text.call_args.args[0])
  self.assertNotIn('memory_supersede_confirm',self.state)
