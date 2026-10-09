"""Executable PTB contracts for inline chat controls; all transports mocked."""
import asyncio
import tempfile
import types
import unittest
from pathlib import Path
from unittest.mock import AsyncMock, Mock, patch

try:
    import telegram
    import telegram_bot as bot
except ModuleNotFoundError:
    telegram = None
    bot = None
import task_ui as ui
import runtime_store


@unittest.skipIf(bot is None, 'PTB runtime required')
class InlineMenuTests(unittest.IsolatedAsyncioTestCase):
    def setUp(self):
        self.directory = tempfile.TemporaryDirectory()
        self.addCleanup(self.directory.cleanup)
        self.root = Path(self.directory.name)
        self.store = runtime_store.RuntimeStore(self.root / 'state.sqlite3')
        self.addCleanup(self.store.close)
        self.state = {}
        self.message = types.SimpleNamespace(text='', reply_text=AsyncMock())
        self.query = types.SimpleNamespace(data='', message=self.message,
                                          answer=AsyncMock(), edit_message_text=AsyncMock())
        self.update = types.SimpleNamespace(message=self.message, effective_message=self.message,
                                            callback_query=self.query, update_id=42)
        self.context = types.SimpleNamespace(user_data=self.state, bot_data={})
        for name, value in [('auth_check', lambda u: True), ('_vault', lambda: self.root),
                            ('_runtime_store', lambda: self.store), ('_sync_reminder', Mock()),
                            ('_sync_task', Mock()), ('_sync_note', Mock())]:
            p = patch.object(bot, name, value); p.start(); self.addCleanup(p.stop)

    def test_main_controls_are_inline_with_visible_reminder(self):
        markup = bot.main_keyboard()
        self.assertIsInstance(markup, telegram.InlineKeyboardMarkup)
        buttons = [b for row in markup.inline_keyboard for b in row]
        self.assertIn('🔔 Напомнить', [b.text for b in buttons])
        self.assertTrue(all(b.callback_data and len(b.callback_data.encode()) <= 64 for b in buttons))

    async def test_start_removes_old_keyboard_then_shows_inline_menu(self):
        await bot.start(self.update, self.context)
        calls = self.message.reply_text.call_args_list
        self.assertTrue(any(isinstance(c.kwargs.get('reply_markup'), telegram.ReplyKeyboardRemove) for c in calls))
        self.assertIsInstance(calls[-1].kwargs['reply_markup'], telegram.InlineKeyboardMarkup)

    async def test_callback_selects_note_and_requires_no_text_command(self):
        self.query.data = 'm:thought'
        await bot.handle_callback(self.update, self.context)
        self.assertEqual(ui.mode(self.state), ui.MODE_NOTE)
        self.query.answer.assert_awaited_once()
        self.query.edit_message_text.assert_awaited()

    async def test_reminder_menu_shows_new_and_existing_routes(self):
        self.query.data = 'm:remind'
        await bot.handle_callback(self.update, self.context)
        markup = self.query.edit_message_text.call_args.kwargs['reply_markup']
        callbacks = [b.callback_data for row in markup.inline_keyboard for b in row]
        self.assertIn('m:remindnew', callbacks)
        self.assertIn('m:remindtasks', callbacks)

    async def test_bad_reminder_time_stays_in_input_does_not_capture(self):
        self.query.data = 'm:remindnew'
        await bot.handle_callback(self.update, self.context)
        self.message.reply_text.reset_mock()
        with patch.object(bot, 'append_to_inbox') as capture:
            await bot.handle_content(self.update, self.context, 'когда-нибудь купить кабель')
        capture.assert_not_called()
        self.assertTrue(self.state.get('menu_reminder_input'))
        self.assertIn('время', self.message.reply_text.call_args.args[0].lower())

    async def test_valid_reminder_creates_one_task_and_one_reminder(self):
        self.query.data = 'm:remindnew'
        await bot.handle_callback(self.update, self.context)
        await bot.handle_content(self.update, self.context, 'через 2 часа отправить отчёт')
        tasks = list((self.root / 'TaskNotes' / 'Tasks').glob('*.md'))
        self.assertEqual(len(tasks), 1)
        self.assertIn('reminders:', tasks[0].read_text())
        self.assertFalse(self.state.get('menu_reminder_input'))
        bot._sync_reminder.assert_called_once()

    async def test_home_clears_all_pending_inputs(self):
        ui.begin(self.state, ui.MODE_NOTE)
        self.state['menu_reminder_input'] = True
        self.state['reminder_input'] = {'path': 'unused'}
        self.query.data = 'm:home'
        await bot.handle_callback(self.update, self.context)
        self.assertEqual(ui.mode(self.state), '')
        self.assertNotIn('menu_reminder_input', self.state)
        self.assertNotIn('reminder_input', self.state)

    async def test_unauthorized_callback_has_no_effect(self):
        self.query.data = 'm:thought'
        with patch.object(bot, 'auth_check', return_value=False):
            await bot.handle_callback(self.update, self.context)
        self.assertEqual(self.state, {})
        self.query.edit_message_text.assert_not_awaited()
        self.message.reply_text.assert_not_awaited()

    async def test_menu_word_is_navigation_not_inbox_content(self):
        self.message.text = 'меню'
        with patch.object(bot, 'append_to_inbox') as capture:
            await bot.handle_text(self.update, self.context)
        capture.assert_not_called()
        self.assertIsInstance(self.message.reply_text.call_args.kwargs['reply_markup'], telegram.InlineKeyboardMarkup)

    async def test_new_task_confirmation_has_immediate_remind_and_focus(self):
        ui.begin(self.state, ui.MODE_TASK)
        await bot.handle_content(self.update, self.context, 'Купить кабель')
        buttons = [b.text for row in self.message.reply_text.call_args.kwargs['reply_markup'].inline_keyboard for b in row]
        self.assertIn('🔔 Напомнить', buttons)
        self.assertTrue(any('Фокус' in x for x in buttons))

    async def test_existing_reminder_picker_opens_time_menu_directly(self):
        ui.submit_task({}, 'Позвонить', vault=self.root)
        self.query.data = 'm:remindtasks'
        await bot.handle_callback(self.update, self.context)
        markup = self.query.edit_message_text.call_args.kwargs['reply_markup']
        self.assertTrue(any(b.callback_data.startswith('r:d:') for row in markup.inline_keyboard for b in row))

    async def test_delivery_is_not_silent(self):
        application = types.SimpleNamespace(bot_data={}, bot=types.SimpleNamespace(send_message=AsyncMock()))
        captured = []
        def scheduler(**kwargs):
            captured.append(kwargs['deliver'])
            return types.SimpleNamespace(run=AsyncMock(), stop=Mock())
        with patch.object(bot.focus, 'reconcile', return_value=types.SimpleNamespace(sync_required=False)), \
             patch.object(bot.reminders, 'reconcile'), \
             patch.object(bot.focus_scheduler, 'FocusScheduler', side_effect=scheduler), \
             patch.object(bot.reminder_scheduler, 'ReminderScheduler', side_effect=scheduler):
            await bot.post_init(application)
            await asyncio.gather(application.bot_data['focus_task'], application.bot_data['reminder_task'])
            for deliver in captured:
                await deliver(types.SimpleNamespace(text='test', buttons=[]))
        self.assertEqual(len(application.bot.send_message.call_args_list), 2)
        self.assertTrue(all(c.kwargs.get('disable_notification') is False for c in application.bot.send_message.call_args_list))

    async def test_voice_capture_buttons_target_exact_saved_record_not_latest(self):
        import inbox_core
        import thought_feed
        def capture(text, kind):
            line = inbox_core.append_line(self.root / thought_feed.INBOX_REL, text, kind)
            inbox_core.append_line(self.root / thought_feed.INBOX_REL, 'Другая новая мысль', kind)
            return line
        with patch.object(bot, 'append_to_inbox', side_effect=capture), patch.object(bot, '_related_hint', return_value=''):
            await bot.handle_content(self.update, self.context, 'Ошибочная расшифровка', origin='voice')
        markup = self.message.reply_text.call_args.kwargs['reply_markup']
        callbacks = [b.callback_data for row in markup.inline_keyboard for b in row]
        edit = next(c for c in callbacks if c.startswith('f:e:'))
        self.assertTrue(any(c.startswith('f:d:') for c in callbacks))
        identity = ui.resolve_thought_token(self.state, edit[4:])
        self.assertEqual(thought_feed.load_thought(self.root, identity).title, 'Ошибочная расшифровка')

    async def test_inline_edit_delete_restore_of_voice_capture(self):
        import inbox_core
        import thought_feed
        inbox = self.root / thought_feed.INBOX_REL
        with patch.object(bot, 'append_to_inbox', side_effect=lambda text, kind: inbox_core.append_line(inbox, text, kind)), \
             patch.object(bot, '_related_hint', return_value=''):
            await bot.handle_content(self.update, self.context, 'Исходная ашибка', origin='voice')
        markup = self.message.reply_text.call_args.kwargs['reply_markup']
        edit = next(b.callback_data for row in markup.inline_keyboard for b in row if b.callback_data.startswith('f:e:'))
        self.query.data = edit
        await bot.handle_callback(self.update, self.context)
        await bot.handle_content(self.update, self.context, 'Правильная мысль', origin='voice')
        self.assertNotIn('Исходная ашибка', inbox.read_text())
        self.assertEqual(len(thought_feed.inbox_thoughts(self.root, limit=10)), 1)
        corrected = self.message.reply_text.call_args.kwargs['reply_markup']
        delete = next(b.callback_data for row in corrected.inline_keyboard for b in row if b.callback_data.startswith('f:d:'))
        self.query.data = delete
        await bot.handle_callback(self.update, self.context)
        self.assertIn('Правильная мысль', inbox.read_text(), 'first tap only asks confirmation')
        confirmation = self.query.edit_message_text.call_args.kwargs['reply_markup']
        self.query.data = next(b.callback_data for row in confirmation.inline_keyboard for b in row if b.callback_data.startswith('f:z:'))
        await bot.handle_callback(self.update, self.context)
        self.assertNotIn('Правильная мысль', inbox.read_text())
        deleted = self.query.edit_message_text.call_args.kwargs['reply_markup']
        self.query.data = next(b.callback_data for row in deleted.inline_keyboard for b in row if b.callback_data.startswith('f:u:'))
        await bot.handle_callback(self.update, self.context)
        self.assertIn('Правильная мысль', inbox.read_text())
        self.assertEqual(len(thought_feed.inbox_thoughts(self.root, limit=10)), 1)

    async def test_note_mode_writes_buttons_for_exact_note(self):
        import dispatch
        ui.begin(self.state, ui.MODE_NOTE)
        with patch.object(dispatch, 'VAULT', self.root):
            await bot.handle_content(self.update, self.context, 'Идея из голосового', origin='voice')
        notes = list((self.root / 'ZETA' / 'FLEETING_DAILY').glob('*.md'))
        self.assertEqual(len(notes), 1)
        markup = self.message.reply_text.call_args.kwargs['reply_markup']
        self.assertTrue(any(b.callback_data.startswith('f:e:') for row in markup.inline_keyboard for b in row))

    async def test_cancel_delete_revokes_old_confirmation(self):
        import inbox_core
        import thought_feed
        line = inbox_core.append_line(self.root / thought_feed.INBOX_REL, 'Сохранить меня')
        view = ui.saved_thought_view(self.state, self.root, line=line)
        delete = next(data for row in view.rows for label, data in row if data.startswith('f:d:'))
        self.query.data = delete
        await bot.handle_callback(self.update, self.context)
        markup = self.query.edit_message_text.call_args.kwargs['reply_markup']
        confirm = next(b.callback_data for row in markup.inline_keyboard for b in row if b.callback_data.startswith('f:z:'))
        cancel = next(b.callback_data for row in markup.inline_keyboard for b in row if b.callback_data.startswith('f:o:'))
        self.query.data = cancel
        await bot.handle_callback(self.update, self.context)
        self.query.data = confirm
        await bot.handle_callback(self.update, self.context)
        self.assertIn('Сохранить меня', (self.root / thought_feed.INBOX_REL).read_text())
        bot._sync_note.assert_not_called()

    async def test_command_first_interaction_removes_legacy_keyboard(self):
        for handler in (bot.handle_cancel, bot.handle_health, bot.handle_exit):
            with self.subTest(handler=handler.__name__), patch.object(bot, 'health_text', return_value='healthy'):
                self.state.clear(); self.message.reply_text.reset_mock()
                await handler(self.update, self.context)
                self.assertTrue(any(isinstance(c.kwargs.get('reply_markup'), telegram.ReplyKeyboardRemove)
                                    for c in self.message.reply_text.call_args_list))

    async def test_cancel_returns_menu_in_same_message(self):
        self.state['inline_menu_ready'] = True
        ui.begin(self.state, ui.MODE_NOTE)
        self.query.data = 'x'
        await bot.handle_callback(self.update, self.context)
        self.assertIs(self.query.edit_message_text.call_args.kwargs.get('reply_markup'), bot.MAIN_KEYBOARD)
        self.message.reply_text.assert_not_awaited()

    async def test_task_card_navigation_cancels_thought_edit(self):
        self.state[ui.THOUGHT_EDIT_KEY] = 'not-relevant'
        result = ui.submit_task({}, 'Открыть дверь', vault=self.root)
        token = ui.issue_token(self.state, result.path)
        self.query.data = 't:' + token
        await bot.handle_callback(self.update, self.context)
        self.assertFalse(ui.thought_edit_pending(self.state))

    async def test_photo_first_interaction_removes_legacy_keyboard(self):
        self.message.photo = [types.SimpleNamespace(get_file=AsyncMock(
            return_value=types.SimpleNamespace(download_to_drive=AsyncMock())))]
        self.message.caption = ''
        await bot.handle_photo(self.update, self.context)
        self.assertTrue(any(isinstance(c.kwargs.get('reply_markup'), telegram.ReplyKeyboardRemove)
                            for c in self.message.reply_text.call_args_list))

    async def test_old_thoughts_button_clears_pending_correction(self):
        self.state[ui.THOUGHT_EDIT_KEY] = 'old'
        self.message.text = ui.BTN_THOUGHTS
        await bot.handle_text(self.update, self.context)
        self.assertFalse(ui.thought_edit_pending(self.state))
        self.assertIsInstance(self.message.reply_text.call_args.kwargs['reply_markup'], telegram.InlineKeyboardMarkup)
