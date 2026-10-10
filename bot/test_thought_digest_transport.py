import tempfile
import types
import unittest
from pathlib import Path
from unittest.mock import AsyncMock, Mock, patch
import telegram_bot as bot
import task_ui as ui


class DigestTransportTests(unittest.IsolatedAsyncioTestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.vault = Path(self.temp.name)
        self.state = {}
        self.message = types.SimpleNamespace(reply_text=AsyncMock(),text='')
        self.query = types.SimpleNamespace(answer=AsyncMock(),edit_message_text=AsyncMock(),data='',message=self.message)
        self.update = types.SimpleNamespace(message=self.message,effective_message=self.message,callback_query=self.query,update_id=456)
        self.context = types.SimpleNamespace(user_data=self.state,bot_data={})
        for name,value in [('auth_check',lambda u:True),('_vault',lambda:self.vault),('_sync_digest',Mock())]:
            p=patch.object(bot,name,value,create=True);p.start();self.addCleanup(p.stop)

    def test_digest_entry_in_menu(self):
        labels=[b.text for row in bot.main_keyboard().inline_keyboard for b in row]
        self.assertIn('💡 Разбор мыслей',labels)

    def test_period_picker_has_no_punctuation_commands(self):
        view=bot._digest_period_view()
        callbacks=[value for row in view.rows for _,value in row]
        self.assertIn('dg:p:yesterday',callbacks)
        self.assertIn('dg:p:week',callbacks)

    async def test_request_routes_without_inbox_capture(self):
        with patch.object(bot,'_request_digest',AsyncMock()) as request, patch.object(bot,'append_to_inbox') as capture:
            await bot.handle_content(self.update,self.context,'разбери мои мысли за вчера')
        capture.assert_not_called()
        request.assert_awaited_once()
        self.assertEqual(request.call_args.args[2],'yesterday')

    async def test_note_mode_keeps_digest_words_as_note(self):
        ui.begin(self.state,ui.MODE_NOTE)
        with patch.object(bot,'_request_digest',AsyncMock()) as request, patch.object(ui,'submit_note',return_value=ui.Submission(False,'saved','',ui.SCREEN_CANCEL)) as save:
            await bot.handle_content(self.update,self.context,'разбери мои мысли за вчера')
        request.assert_not_awaited()
        save.assert_called_once()

    async def test_old_report_keyboard_opens_status_without_capture(self):
        self.message.text = '📊 Отчёт за день'
        ui.begin(self.state, ui.MODE_NOTE)
        with patch.object(bot, '_show_status', AsyncMock()) as status, patch.object(bot, 'handle_content', AsyncMock()) as capture:
            await bot.handle_text(self.update, self.context)
        status.assert_awaited_once()
        capture.assert_not_awaited()
        self.assertEqual(ui.mode(self.state), '')

    async def test_picker_clears_modes(self):
        ui.begin(self.state,ui.MODE_NOTE)
        self.query.data='m:digest'
        await bot.handle_callback(self.update,self.context)
        self.assertEqual(ui.mode(self.state),'')
        self.assertIn('Разбор',self.query.edit_message_text.call_args.args[0])

    async def test_period_callback_enqueues(self):
        self.query.data='dg:p:today'
        with patch.object(bot,'_request_digest',AsyncMock()) as request:
            await bot.handle_callback(self.update,self.context)
        request.assert_awaited_once()
        self.assertEqual(request.call_args.args[2],'today')

    async def test_unauthorized_digest_rejected(self):
        self.query.data='dg:p:today'
        with patch.object(bot,'auth_check',return_value=False), patch.object(bot,'_request_digest',AsyncMock()) as request:
            await bot.handle_callback(self.update,self.context)
        request.assert_not_awaited()

    def test_result_markup_uses_exact_source_ids(self):
        result={'snapshot':{'sources':[{'id':'s1','identity':'exact#hash','title':'Моя идея'}]},'sections':[],'error':''}
        view=bot._digest_result_view('a'*24,result)
        self.assertIn(('1. Моя идея','dg:o:'+('a'*24)+':s1'),view.rows[0])
        self.assertLessEqual(len(view.rows[0][0][1].encode()),64)

    async def test_source_callback_loads_exact_identity(self):
        job='a'*24
        self.query.data=f'dg:o:{job}:s1'
        result={'result':{'snapshot':{'sources':[{'id':'s1','identity':'exact#hash','title':'idea'}]}}}
        with patch.object(bot.thought_digest_jobs,'get_result',return_value=result), patch.object(ui,'render_thought',return_value=ui.View('Exact',[])) as show:
            await bot.handle_callback(self.update,self.context)
        token=show.call_args.args[3]
        self.assertEqual(ui.resolve_thought_token(self.state,token),'exact#hash')

    async def test_model_failure_is_not_capture_or_success(self):
        with patch.object(bot.thought_digest_jobs,'enqueue',side_effect=ValueError('bad')):
            await bot._request_digest(self.update,self.context,'yesterday')
        self.assertIn('не удалось',self.message.reply_text.call_args.args[0].lower())

class DigestDeliveryRuntimeTests(unittest.IsolatedAsyncioTestCase):
    def setUp(self):
        import thought_digest_jobs as jobs
        from thought_digest_delivery import DeliveryLedger
        self.temp=tempfile.TemporaryDirectory();self.addCleanup(self.temp.cleanup)
        self.vault=Path(self.temp.name)
        daily=self.vault/'ZETA/FLEETING_DAILY';daily.mkdir(parents=True)
        (daily/'2026-10-10 — Idea.md').write_text('---\ncreated: 2026-10-10\n---\n# Idea\n\nТестовая идея')
        self.job=jobs.enqueue(self.vault,'recent')
        jobs.process_one(self.vault,generate=lambda snapshot:{'snapshot':snapshot,'sections':[{'kind':'reflect','items':[{'text':'Обдумай идею.','source_ids':['s1']}]}],'error':''})
        self.ledger=DeliveryLedger(self.vault/'acks.sqlite3')
        self.app=types.SimpleNamespace(bot=types.SimpleNamespace(send_message=AsyncMock()))
        for name,value in [('_vault',lambda:self.vault),('_sync_digest',Mock())]:
            p=patch.object(bot,name,value);p.start();self.addCleanup(p.stop)

    async def test_delivered_result_is_not_sent_again_after_ledger_reopen(self):
        from thought_digest_delivery import DeliveryLedger
        await bot._digest_tick(self.app,self.ledger)
        self.app.bot.send_message.assert_awaited_once()
        self.assertIn('Предложения',self.app.bot.send_message.call_args.kwargs['text'])
        callbacks=[b.callback_data for row in self.app.bot.send_message.call_args.kwargs['reply_markup'].inline_keyboard for b in row]
        self.assertIn('dg:o:'+self.job['id']+':s1',callbacks)
        await bot._digest_tick(self.app,DeliveryLedger(self.vault/'acks.sqlite3'))
        self.app.bot.send_message.assert_awaited_once()

    async def test_failed_send_remains_retriable(self):
        self.app.bot.send_message.side_effect=RuntimeError('transport unavailable')
        with self.assertRaises(RuntimeError): await bot._digest_tick(self.app,self.ledger)
        self.assertFalse(self.ledger.contains(self.job['id']))
        self.app.bot.send_message.side_effect=None
        await bot._digest_tick(self.app,self.ledger)
        self.assertTrue(self.ledger.contains(self.job['id']))

class DigestShutdownTests(unittest.IsolatedAsyncioTestCase):
    async def test_already_cancelled_task_does_not_abort_shutdown(self):
        import asyncio
        task=asyncio.create_task(asyncio.sleep(100))
        task.cancel()
        application=types.SimpleNamespace(bot_data={'digest_task':task,'digest_stop':asyncio.Event(),'digest_wake':asyncio.Event()})
        await bot.post_shutdown(application)
        self.assertTrue(application.bot_data['digest_stop'].is_set())

class DigestBacklogTests(unittest.IsolatedAsyncioTestCase):
    async def test_old_unsent_result_survives_hundred_newer_acks(self):
        from thought_digest_delivery import DeliveryLedger
        with tempfile.TemporaryDirectory() as directory:
            ledger=DeliveryLedger(Path(directory)/'acks.sqlite3')
            backlog=[{'id':format(i,'024x')} for i in range(101)]
            for request in backlog[1:]: ledger.mark(request['id'])
            result={'result':{'snapshot':{'sources':[]},'sections':[],'error':''}}
            app=types.SimpleNamespace(bot=types.SimpleNamespace(send_message=AsyncMock()))
            with patch.object(bot.thought_digest_jobs,'requests',side_effect=lambda vault,limit:backlog[-limit:]),patch.object(bot.thought_digest_jobs,'get_result',return_value=result),patch.object(bot,'_sync_digest',Mock()):
                await bot._digest_tick(app,ledger)
            app.bot.send_message.assert_awaited_once()
            self.assertTrue(ledger.contains(backlog[0]['id']))
