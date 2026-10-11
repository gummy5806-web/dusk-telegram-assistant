"""Opening and resolving sources must preserve the exact saved digest."""
import tempfile
import types
import unittest
from pathlib import Path
from unittest.mock import AsyncMock, Mock, patch
import telegram_bot as bot
import task_ui as ui
import thought_feed

class DigestNavigationTests(unittest.IsolatedAsyncioTestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory(); self.addCleanup(self.temp.cleanup)
        self.vault = Path(self.temp.name)
        daily = self.vault/'ZETA/FLEETING_DAILY'; daily.mkdir(parents=True)
        self.path = daily/'2026-10-01 — Idea.md'
        self.path.write_text('---\ncreated: 2026-10-01\n---\n# Idea\n\n## Мысль\n- моя идея\n')
        self.identity = thought_feed._identity_text(self.path.relative_to(self.vault).as_posix(), self.path.read_text())
        self.job = 'a'*24
        self.result = {'result': {'snapshot': {'sources':[{'id':'s1','identity':self.identity,'path':self.path.relative_to(self.vault).as_posix(),'title':'моя идея','date':'2026-10-01','role':'primary','text':'моя идея'}], 'coverage':{}},'sections':[{'kind':'reflect','items':[{'text':'Подумай об идее.','source_ids':['s1']}]}],'error':''}}
        self.state = {}
        self.child = types.SimpleNamespace(message_id=202)
        self.message = types.SimpleNamespace(message_id=101,reply_text=AsyncMock(return_value=self.child))
        self.query = types.SimpleNamespace(data='',message=self.message,answer=AsyncMock(),edit_message_text=AsyncMock())
        self.update = types.SimpleNamespace(callback_query=self.query,message=self.message,effective_message=self.message)
        self.context = types.SimpleNamespace(user_data=self.state,bot_data={})
        for name,value in [('auth_check',lambda u:True),('_vault',lambda:self.vault),('_runtime_store',lambda:None),('_sync_note',Mock())]:
            p=patch.object(bot,name,value);p.start();self.addCleanup(p.stop)
        p=patch.object(bot.thought_digest_jobs,'get_result',return_value=self.result); self.get=p.start();self.addCleanup(p.stop)
        p=patch.object(bot.thought_digest_jobs,'enqueue');self.enqueue=p.start();self.addCleanup(p.stop)

    async def open_source(self):
        self.query.data=f'dg:o:{self.job}:s1'
        await bot.handle_callback(self.update,self.context)
        return ui.issue_thought_token(self.state,self.identity)

    def callbacks(self, method):
        markup=method.call_args.kwargs['reply_markup']
        return [b.callback_data for row in markup.inline_keyboard for b in row]

    async def test_source_opens_separately_and_leaves_summary_message_untouched(self):
        await self.open_source()
        self.query.edit_message_text.assert_not_awaited()
        self.message.reply_text.assert_awaited_once()
        self.assertIn('dg:r:'+self.job,self.callbacks(self.message.reply_text))

    async def test_delete_confirmation_and_restore_keep_return_to_same_digest(self):
        token=await self.open_source(); self.query.message=self.child
        for action in ('d','z'):
            self.query.data=f'f:{action}:{token}'
            await bot.handle_callback(self.update,self.context)
            self.assertIn('dg:r:'+self.job,self.callbacks(self.query.edit_message_text))
        self.assertFalse(self.path.exists())
        restore=next(x for x in self.callbacks(self.query.edit_message_text) if x.startswith('f:u:'))
        self.query.data=restore
        await bot.handle_callback(self.update,self.context)
        self.assertTrue(self.path.exists())
        self.assertIn('dg:r:'+self.job,self.callbacks(self.query.edit_message_text))
        self.enqueue.assert_not_called()

    async def test_done_keeps_return_and_back_reads_saved_result_without_model(self):
        token=await self.open_source(); self.query.message=self.child
        self.query.data='f:c:'+token
        await bot.handle_callback(self.update,self.context)
        self.assertIn('dg:r:'+self.job,self.callbacks(self.query.edit_message_text))
        self.query.data='dg:r:'+self.job
        self.context.user_data={}
        await bot.handle_callback(self.update,self.context)
        self.assertEqual(self.query.edit_message_text.call_args.args[0], bot._digest_result_view(self.job,self.result['result']).text)
        self.enqueue.assert_not_called()

    async def test_different_digest_cards_keep_their_own_return(self):
        await self.open_source()
        self.job='b'*24;self.message.reply_text.return_value=types.SimpleNamespace(message_id=303)
        await self.open_source()
        self.query.message=self.child; token=ui.issue_thought_token(self.state,self.identity)
        self.query.data='f:d:'+token
        await bot.handle_callback(self.update,self.context)
        self.assertIn('dg:r:'+('a'*24),self.callbacks(self.query.edit_message_text))
        self.assertNotIn('dg:r:'+('b'*24),self.callbacks(self.query.edit_message_text))

    async def test_missing_source_leaves_original_digest_and_offers_return(self):
        self.path.unlink()
        await self.open_source()
        self.query.edit_message_text.assert_not_awaited()
        self.assertIn('dg:r:'+self.job,self.callbacks(self.message.reply_text))

    async def test_edit_input_and_new_saved_card_keep_same_return(self):
        token=await self.open_source(); self.query.message=self.child
        self.query.data='f:e:'+token
        await bot.handle_callback(self.update,self.context)
        self.assertIn('dg:r:'+self.job,self.callbacks(self.query.edit_message_text))
        self.message.reply_text.return_value=types.SimpleNamespace(message_id=404)
        await bot.handle_content(self.update,self.context,'исправленная мысль')
        self.assertIn('dg:r:'+self.job,self.callbacks(self.message.reply_text))
        self.assertIn('исправленная мысль',self.path.read_text())
        self.enqueue.assert_not_called()

    async def test_invalid_back_is_safe_and_never_creates_new_request(self):
        self.get.return_value=None
        self.query.data='dg:r:'+self.job
        await bot.handle_callback(self.update,self.context)
        self.assertIn('недоступен',self.query.edit_message_text.call_args.args[0])
        self.enqueue.assert_not_called()

    async def test_card_markup_keeps_return_after_memory_is_cleared(self):
        token = await self.open_source()
        self.child.reply_markup = self.message.reply_text.call_args.kwargs['reply_markup']
        self.state.pop('digest_messages')
        self.query.message = self.child
        self.query.data = 'f:d:' + token
        await bot.handle_callback(self.update, self.context)
        self.assertIn('dg:r:' + self.job, self.callbacks(self.query.edit_message_text))

    async def test_failed_edit_keeps_return_and_cancel_clears_pending_edit(self):
        token = await self.open_source()
        self.query.message = self.child
        self.query.data = 'f:e:' + token
        await bot.handle_callback(self.update, self.context)
        await bot.handle_content(self.update, self.context, '')
        callbacks = self.callbacks(self.message.reply_text)
        self.assertIn('dg:r:' + self.job, callbacks)
        self.assertIn('x', callbacks)
        self.query.data = 'x'
        await bot.handle_callback(self.update, self.context)
        self.assertFalse(ui.thought_edit_pending(self.state))
        self.assertNotIn('digest_edit_return', self.state)

    async def test_next_and_hide_keep_return_and_hub_drops_it(self):
        token = await self.open_source()
        self.query.message = self.child
        for action in ('n', 'x'):
            self.query.data = 'f:' + action + ':' + token
            await bot.handle_callback(self.update, self.context)
            self.assertIn('dg:r:' + self.job, self.callbacks(self.query.edit_message_text))
        self.query.data = 'f:h'
        await bot.handle_callback(self.update, self.context)
        self.assertNotIn('dg:r:' + self.job, self.callbacks(self.query.edit_message_text))
