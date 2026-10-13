import datetime as dt,os,tempfile,types,unittest
from pathlib import Path
from unittest.mock import AsyncMock,Mock,patch
import ai_usage,telegram_bot as bot,semantic_search_jobs as jobs
from thought_digest_delivery import DeliveryLedger
class AssistantDeliveryTests(unittest.IsolatedAsyncioTestCase):
 def setUp(self):
  self.temp=tempfile.TemporaryDirectory();self.addCleanup(self.temp.cleanup);self.root=Path(self.temp.name)
  env=patch.dict(os.environ,{'DUSK_AI_USAGE_DB':str(self.root/'usage.sqlite3')});env.start();self.addCleanup(env.stop)
  self.ledger=DeliveryLedger(self.root/'delivery.sqlite3')
  self.app=types.SimpleNamespace(bot=types.SimpleNamespace(send_message=AsyncMock()),user_data={bot.ALLOWED_USER:{}})
  for name,value in [('_vault',lambda:self.root),('_sync_assistant',Mock(return_value=True)),('_now',lambda:dt.datetime(2026,10,14,5,tzinfo=dt.timezone.utc))]:
   p=patch.object(bot,name,value);p.start();self.addCleanup(p.stop)
 async def test_semantic_delivery_marks_only_success_and_never_reprocesses_job(self):
  job=jobs.enqueue(self.root,'найди мысли')
  jobs.process_one(self.root,search=lambda root,q:{'hits':[],'coverage':{},'error':''})
  self.app.bot.send_message.side_effect=RuntimeError('transport failure')
  with self.assertRaises(RuntimeError):await bot._assistant_tick(self.app,self.ledger)
  self.assertFalse(self.ledger.contains(job['id']))
  self.app.bot.send_message.side_effect=None
  await bot._assistant_tick(self.app,self.ledger)
  self.assertTrue(self.ledger.contains(job['id']))
  self.app.bot.send_message.reset_mock()
  await bot._assistant_tick(self.app,self.ledger)
  self.app.bot.send_message.assert_not_awaited()
 async def test_daily_notice_once_after_morning_without_search_jobs(self):
  yesterday=dt.datetime(2026,10,13,8,tzinfo=dt.timezone.utc)
  ai_usage.record_call(provider='vertex',model='gemini-2.5-flash',input_tokens=10,output_tokens=20,now=yesterday)
  ai_usage.publish(self.root,now=bot._now())
  with patch.object(bot,'_now',return_value=dt.datetime(2026,10,14,2,tzinfo=dt.timezone.utc)):
   await bot._assistant_tick(self.app,self.ledger)
  self.app.bot.send_message.assert_not_awaited()
  await bot._assistant_tick(self.app,self.ledger)
  self.app.bot.send_message.assert_awaited_once()
  self.assertIn('За 2026-10-13',self.app.bot.send_message.call_args.kwargs['text'])
  await bot._assistant_tick(self.app,self.ledger)
  self.app.bot.send_message.assert_awaited_once()
