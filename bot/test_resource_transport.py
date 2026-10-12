"""Actual Telegram routes for saved sites, without a network or real vault."""
import tempfile
import os
import types
import unittest
from pathlib import Path
from unittest.mock import AsyncMock, Mock, patch
import telegram_bot as bot
import task_ui as ui

URL = 'https://us-uni-doc-generator.vercel.app/'

class ResourceTransportTests(unittest.IsolatedAsyncioTestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory(); self.addCleanup(self.temp.cleanup)
        self.vault = Path(self.temp.name)
        self.state = {'inline_menu_ready': True}
        self.message = types.SimpleNamespace(reply_text=AsyncMock(), text='', message_id=1)
        self.query = types.SimpleNamespace(answer=AsyncMock(), edit_message_text=AsyncMock(), data='', message=self.message)
        self.update = types.SimpleNamespace(message=self.message, effective_message=self.message, callback_query=self.query)
        self.context = types.SimpleNamespace(user_data=self.state, bot_data={})
        self.sync = Mock()
        self.inbox = Mock(return_value='entry')
        env = patch.dict(os.environ, {'VAULT_PATH': str(self.vault)})
        env.start(); self.addCleanup(env.stop)
        dispatch = patch.object(bot._dispatch, 'dispatch_detailed', return_value=types.SimpleNamespace(text='dispatch', mutated=False))
        dispatch.start(); self.addCleanup(dispatch.stop)
        for name, value in [('auth_check', lambda u: True), ('_vault', lambda: self.vault), ('_sync_note', self.sync), ('_sync_task', Mock()), ('_sync_video', Mock()), ('append_to_inbox', self.inbox), ('_related_hint', lambda t: '')]:
            p = patch.object(bot, name, value); p.start(); self.addCleanup(p.stop)

    def records(self):
        return list((self.vault/'PARA/RESOURCES').rglob('*.md'))

    async def test_plain_website_is_resource_not_thought(self):
        await bot.handle_content(self.update, self.context, 'генератор документов американских университетов ' + URL)
        self.inbox.assert_not_called()
        self.assertEqual(len(self.records()), 1)
        text = self.records()[0].read_text()
        self.assertIn(URL, text)
        self.assertIn('американских университетов', text)
        self.assertIn('ресурс', self.message.reply_text.call_args.args[0].lower())

    async def test_material_button_accepts_site(self):
        ui.begin(self.state, ui.MODE_VIDEO)
        await bot.handle_content(self.update, self.context, URL)
        self.assertEqual(len(self.records()), 1)
        self.inbox.assert_not_called()
        self.assertEqual(ui.mode(self.state), '')

    async def test_explicit_thought_mode_keeps_website_inside_thought(self):
        ui.begin(self.state, ui.MODE_NOTE)
        with patch.object(ui, 'submit_note', return_value=ui.Submission(False, 'note', '', ui.SCREEN_CANCEL)) as capture:
            await bot.handle_content(self.update, self.context, 'идея ' + URL)
        capture.assert_called_once()
        self.assertEqual(self.records(), [])

    async def test_private_link_is_not_public_resource(self):
        await bot.handle_content(self.update, self.context, '!личная ссылка ' + URL)
        self.inbox.assert_called_once()
        self.assertTrue(self.inbox.call_args.kwargs['private'])
        self.assertEqual(self.records(), [])

    async def test_video_keeps_existing_learning_queue(self):
        with patch.object(bot._dispatch, 'dispatch_detailed', return_value=ui.Submission(False, 'queued', '', ui.SCREEN_CANCEL)) as dispatch, patch.object(ui, 'mutation_sync'):
            await bot.handle_content(self.update, self.context, 'https://youtu.be/12345678901')
        dispatch.assert_called_once()
        self.assertEqual(self.records(), [])

    async def test_resource_menu_is_visible_and_empty_list_useful(self):
        self.assertIn('🔖 Ресурсы', [b.text for row in bot.MAIN_KEYBOARD.inline_keyboard for b in row])
        self.query.data = 'm:resources'
        await bot.handle_callback(self.update, self.context)
        self.assertIn('Ресурс', self.query.edit_message_text.call_args.args[0])

    def test_url_rows_open_site_instead_of_becoming_callbacks(self):
        markup = bot._inline(ui.View('x', [[('🌐 Открыть сайт', 'url:' + URL)]]))
        self.assertEqual(markup.inline_keyboard[0][0].url, URL)
        self.assertIsNone(markup.inline_keyboard[0][0].callback_data)

    async def test_resource_add_prompts_and_saves_following_message(self):
        self.query.data = 'rs:a'
        await bot.handle_callback(self.update, self.context)
        await bot.handle_content(self.update, self.context, URL)
        self.assertEqual(len(self.records()), 1)
        self.inbox.assert_not_called()

    async def test_resource_search_finds_saved_site_with_card_button(self):
        await bot.handle_content(self.update, self.context, 'генератор документов ' + URL)
        self.query.data = 'rs:s'
        await bot.handle_callback(self.update, self.context)
        await bot.handle_content(self.update, self.context, 'генератор')
        self.assertIn('генератор', self.message.reply_text.call_args.args[0].lower())
        markup = self.message.reply_text.call_args.kwargs['reply_markup']
        self.assertTrue(any((b.callback_data or '').startswith('rs:o:') for row in markup.inline_keyboard for b in row))
        self.inbox.assert_not_called()

    async def test_natural_find_resource_returns_clickable_results(self):
        await bot.handle_content(self.update, self.context, 'генератор документов ' + URL)
        await bot.handle_content(self.update, self.context, 'найди тот генератор документов')
        markup = self.message.reply_text.call_args.kwargs['reply_markup']
        self.assertTrue(any((b.callback_data or '').startswith('rs:o:') for row in markup.inline_keyboard for b in row))
        self.inbox.assert_not_called()

    async def test_home_cancels_resource_input(self):
        self.query.data = 'rs:a'; await bot.handle_callback(self.update, self.context)
        self.query.data = 'm:home'; await bot.handle_callback(self.update, self.context)
        await bot.handle_content(self.update, self.context, 'мысль без ссылки')
        self.inbox.assert_called_once()

    async def test_edit_delete_restore_round_trip_uses_inline_controls(self):
        import resources
        await bot.handle_content(self.update, self.context, 'генератор документов ' + URL)
        record = resources.list_resources(self.vault)[0]
        self.query.data = f'rs:e:{record.id}:{record.version}'
        await bot.handle_callback(self.update, self.context)
        await bot.handle_content(self.update, self.context, 'сервис для университетских документов')
        record = resources.load(self.vault, record.id)
        self.assertEqual(record.description, 'сервис для университетских документов')
        self.assertEqual(record.url, URL)
        self.query.data = f'rs:d:{record.id}:{record.version}'
        await bot.handle_callback(self.update, self.context)
        self.query.data = f'rs:z:{record.id}:{record.version}'
        await bot.handle_callback(self.update, self.context)
        self.assertEqual(self.records(), [])
        markup = self.query.edit_message_text.call_args.kwargs['reply_markup']
        self.query.data = next(b.callback_data for row in markup.inline_keyboard for b in row if (b.callback_data or '').startswith('rs:u:'))
        await bot.handle_callback(self.update, self.context)
        self.assertEqual(resources.load(self.vault, record.id).description, record.description)
        self.assertEqual(self.sync.call_count, 4)

    async def test_cancelled_delete_confirmation_cannot_delete_resource(self):
        import resources
        await bot.handle_content(self.update, self.context, URL)
        record = resources.list_resources(self.vault)[0]
        self.query.data = f'rs:d:{record.id}:{record.version}'
        await bot.handle_callback(self.update, self.context)
        self.query.data = 'm:home'
        await bot.handle_callback(self.update, self.context)
        self.query.data = f'rs:z:{record.id}:{record.version}'
        await bot.handle_callback(self.update, self.context)
        self.assertIsNotNone(resources.load(self.vault, record.id))
        self.assertEqual(self.sync.call_count, 1)

    async def test_site_save_failure_keeps_add_mode_and_does_not_capture_thought(self):
        self.query.data = 'rs:a'
        await bot.handle_callback(self.update, self.context)
        await bot.handle_content(self.update, self.context, 'без ссылки')
        self.assertTrue(self.state.get('resource_add'))
        self.inbox.assert_not_called()
        await bot.handle_content(self.update, self.context, URL)
        self.assertNotIn('resource_add', self.state)
        self.assertEqual(len(self.records()), 1)

    async def test_unauthorized_callback_does_not_mutate_resources(self):
        with patch.object(bot, 'auth_check', return_value=False):
            self.query.data = 'rs:a'
            await bot.handle_callback(self.update, self.context)
        self.assertNotIn('resource_add', self.state)
        self.assertEqual(self.records(), [])

    async def test_video_domain_mentioned_in_site_path_stays_resource(self):
        with patch.object(bot._dispatch, 'dispatch_detailed', return_value=types.SimpleNamespace(text='video route', mutated=False)) as dispatch:
            await bot.handle_content(self.update, self.context, 'https://tools.example/youtube.com/widget')
        dispatch.assert_not_called()
        self.assertEqual(len(self.records()), 1)
        self.inbox.assert_not_called()

    async def test_material_with_multiple_site_links_saves_all(self):
        ui.begin(self.state, ui.MODE_VIDEO)
        await bot.handle_content(self.update, self.context, 'генераторы ' + URL + ' https://tools.example/generator')
        self.assertEqual(len(self.records()), 2)
        self.assertEqual(self.sync.call_count, 1)

    async def test_mixed_video_and_site_message_is_not_partially_saved(self):
        ui.begin(self.state, ui.MODE_VIDEO)
        with patch.object(bot._dispatch, 'dispatch_detailed') as dispatch:
            await bot.handle_content(self.update, self.context, URL + ' https://youtu.be/12345678901')
        dispatch.assert_not_called()
        self.assertEqual(self.records(), [])
        self.assertIn('отдельным', self.message.reply_text.call_args.args[0])
