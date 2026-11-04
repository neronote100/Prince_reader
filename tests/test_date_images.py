import contextlib
import io
import json
import os
import sqlite3
import tempfile
import unittest
from datetime import datetime, timedelta, timezone
from pathlib import Path
from unittest.mock import patch
from urllib.parse import parse_qs, urlsplit

import batch
import discord_note as app


class DateImageTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        root = Path(self.temp.name)
        for module in (app, batch):
            patcher = patch.object(module, 'ROOT', root)
            patcher.start()
            self.addCleanup(patcher.stop)
        environment = patch.dict(os.environ, {}, clear=True)
        environment.start()
        self.addCleanup(environment.stop)
        (root / 'config.json').write_text('{"guild_id":"1","channel_ids":["2"]}')
        self.start = datetime(2026, 11, 3, 15, tzinfo=timezone.utc)
        self.end = self.start + timedelta(days=1)

    def row(self, instant, sequence=0, **extras):
        parsed = app.utc_datetime(instant)
        row = {'id': str(int(app.snowflake_before(parsed)) + sequence),
               'timestamp': parsed.isoformat(), 'content': 'synthetic conversation'}
        row.update(extras)
        return row

    def insert(self, rows):
        with app.database() as db:
            db.executemany('INSERT INTO messages VALUES (?, ?, ?, ?, ?)', rows)

    def request_file(self, value):
        path = app.ROOT / 'requests'
        path.mkdir(exist_ok=True)
        (path / 'collect.json').write_text(json.dumps(value))

    def test_range_is_inclusive_start_and_exclusive_end(self):
        rows = [self.row(self.end), self.row(self.end - timedelta(microseconds=1)),
                self.row(self.start), self.row(self.start - timedelta(microseconds=1))]
        with patch.object(app, 'request', side_effect=[{'guild_id': '1'}, rows]):
            result = app.sync(since=self.start, until=self.end)
        messages = app.read_messages(since=self.start, until=self.end)
        self.assertEqual(len(messages), 2)
        self.assertEqual([item['created'] for item in messages],
                         [app.utc_timestamp(self.start), app.utc_timestamp(self.end - timedelta(microseconds=1))])
        self.assertTrue(result['coverage']['complete'])
        self.assertEqual(result['coverage']['channels'][0]['reason'], 'start_reached')

    def test_range_pages_from_until_and_stops_at_start(self):
        first = [self.row(self.start + timedelta(hours=1), i) for i in reversed(range(100))]
        second = [self.row(self.start), self.row(self.start - timedelta(seconds=1))]
        with patch.object(app, 'request', side_effect=[{'guild_id': '1'}, first, second]) as request:
            result = app.sync(max_pages=100, since=self.start, until=self.end)
        queries = [parse_qs(urlsplit(call.args[0]).query) for call in request.call_args_list[1:]]
        self.assertEqual(queries[0]['before'], [app.snowflake_before(self.end)])
        self.assertEqual(queries[1]['before'], [min((row['id'] for row in first), key=int)])
        self.assertEqual(result['processed'], 101)
        self.assertEqual(result['coverage']['channels'][0]['pages'], 2)
        self.assertTrue(result['coverage']['complete'])

    def test_page_cap_reports_incomplete_coverage(self):
        rows = [self.row(self.start + timedelta(hours=1), i) for i in reversed(range(100))]
        with patch.object(app, 'request', side_effect=[{'guild_id': '1'}, rows]):
            result = app.sync(max_pages=1, since=self.start, until=self.end)
        coverage = result['coverage']['channels'][0]
        self.assertFalse(result['coverage']['complete'])
        self.assertTrue(coverage['capped'])
        self.assertEqual(coverage['reason'], 'page_limit')

    def test_empty_channel_is_complete(self):
        with patch.object(app, 'request', side_effect=[{'guild_id': '1'}, []]):
            result = app.sync(max_pages=100, since=self.start, until=self.end)
        self.assertEqual(result['processed'], 0)
        self.assertTrue(result['coverage']['complete'])
        self.assertEqual(app.read_messages(since=self.start, until=self.end), [])

    def test_raw_bot_timestamps_can_establish_coverage(self):
        rows = [self.row(self.start - timedelta(seconds=1), author={'bot': True})]
        with patch.object(app, 'request', side_effect=[{'guild_id': '1'}, rows]):
            result = app.sync(since=self.start, until=self.end)
        self.assertTrue(result['coverage']['complete'])
        self.assertEqual(app.read_messages(), [])

    def test_range_requires_timezone_and_both_boundaries(self):
        for options in ({'since': self.start}, {'until': self.end},
                        {'since': self.start, 'until': self.start},
                        {'since': datetime(2026, 11, 4), 'until': self.end}):
            with self.subTest(options=options), self.assertRaises(ValueError):
                app.sync(**options)

    def test_read_range_does_not_use_latest_1000_limit(self):
        rows = [(str(i), '2', app.utc_timestamp(self.start), 'synthetic', None) for i in range(1101)]
        self.insert(rows)
        self.assertEqual(len(app.read_messages(limit=1000)), 1000)
        self.assertEqual(len(app.read_messages(since=self.start, until=self.end)), 1101)

    def test_read_range_never_silently_truncates_at_safety_limit(self):
        self.insert([(str(i), '2', app.utc_timestamp(self.start), 'synthetic', None) for i in range(3)])
        with patch.object(app, 'MAX_RANGE_MESSAGES', 2), self.assertRaises(ValueError):
            app.read_messages(since=self.start, until=self.end)

    def test_migration_preserves_old_table_and_normalizes_offsets_exactly(self):
        directory = app.ROOT / 'data'
        directory.mkdir()
        with sqlite3.connect(directory / 'messages.sqlite3') as db:
            db.execute('CREATE TABLE messages (id TEXT PRIMARY KEY, channel TEXT, created TEXT, content TEXT, reply_to TEXT)')
            db.executemany('INSERT INTO messages VALUES (?, ?, ?, ?, ?)', [
                ('1', '2', '2026-11-03T23:59:59.999999+09:00', 'before', None),
                ('2', '2', '2026-11-04T00:00:00+09:00', 'start', None),
                ('3', '2', '2026-11-04T23:59:59.999999+09:00', 'last', None),
                ('4', '2', '2026-11-05T00:00:00+09:00', 'after', None)])
        messages = app.read_messages(since=self.start, until=self.end)
        self.assertEqual([message['content'] for message in messages], ['start', 'last'])
        self.assertEqual([message['attachments'] for message in messages], [[], []])
        with app.database() as db:
            self.assertEqual(len(db.execute('PRAGMA table_info(messages)').fetchall()), 5)
            db.execute('INSERT INTO messages VALUES (?, ?, ?, ?, ?)', ('5', '2', app.utc_timestamp(self.start), 'compatible', None))
        self.assertEqual(len(app.read_messages(since=self.start, until=self.end)), 3)

    def test_attachment_metadata_is_restricted_to_discord_images(self):
        accepted = [
            {'url': 'https://cdn.discordapp.com/attachments/1/2/example.png?ex=synthetic',
             'filename': 'example.png', 'size': 123, 'content_type': 'image/png'},
            {'url': 'https://media.discordapp.net/attachments/1/2/example.JPG',
             'filename': 'example.JPG', 'size': 456},
            {'url': 'https://cdn.discordapp.com/attachments/1/2/image.bin',
             'filename': 'image.bin', 'content_type': 'image/webp'}]
        rejected = [
            {'url': 'https://cdn.discordapp.com/attachments/1/2/file.pdf', 'filename': 'file.pdf', 'content_type': 'application/pdf'},
            {'url': 'http://cdn.discordapp.com/image.png', 'filename': 'image.png'},
            {'url': 'https://example.org/image.png', 'filename': 'image.png'},
            {'url': 'https://cdn.discordapp.com.example.org/image.png', 'filename': 'image.png'},
            {'url': 'https://cdn.discordapp.com@example.org/image.png', 'filename': 'image.png'},
            {'url': 'https://name@cdn.discordapp.com/image.png', 'filename': 'image.png'},
            {'url': 'https://cdn.discordapp.com:444/image.png', 'filename': 'image.png'},
            {'url': 'https://cdn.discordapp.com:invalid/image.png', 'filename': 'image.png'},
            {'url': None, 'filename': 'image.png'}, None]
        row = self.row(self.start, attachments=accepted + rejected,
                       embeds=[{'image': {'url': 'https://example.org/embedded.png'}}],
                       author={'username': 'must not persist'})
        with patch.object(app, 'request', side_effect=[{'guild_id': '1'}, [row]]):
            app.sync(since=self.start, until=self.end)
        message = app.read_messages(since=self.start, until=self.end)[0]
        self.assertEqual(len(message['attachments']), 3)
        self.assertEqual(message['attachments'][0], accepted[0])
        self.assertEqual(message['attachments'][1]['content_type'], None)
        self.assertNotIn('author', message)
        self.assertNotIn('embeds', message)

    def test_no_attachment_and_removal_are_empty_lists(self):
        row = self.row(self.start, attachments=[{'url': 'https://cdn.discordapp.com/image.png', 'filename': 'image.png'}])
        with patch.object(app, 'request', side_effect=[{'guild_id': '1'}, [row]]):
            app.sync()
        self.assertEqual(len(app.read_messages()[0]['attachments']), 1)
        row.pop('attachments')
        with patch.object(app, 'request', side_effect=[{'guild_id': '1'}, [row]]):
            app.sync()
        self.assertEqual(app.read_messages()[0]['attachments'], [])

    def test_date_request_uses_japan_midnight(self):
        self.request_file({'requested_at': '2026-11-04T19:00:00Z', 'target_date': '2026-11-04'})
        window = batch.collection_window()
        self.assertEqual(window['since'].astimezone(timezone.utc), self.start)
        self.assertEqual(window['until'].astimezone(timezone.utc), self.end)

    def test_scheduled_job_ignores_retained_date(self):
        self.request_file({'target_date': '2026-11-04'})
        with patch.dict(os.environ, {'GITHUB_EVENT_NAME': 'schedule'}):
            self.assertIsNone(batch.collection_window())

    def test_timestamp_only_and_missing_request_keep_original_mode(self):
        self.assertIsNone(batch.collection_window())
        self.request_file({'requested_at': '2026-11-04T19:00:00Z'})
        self.assertIsNone(batch.collection_window())

    def test_target_date_requires_iso_calendar_date(self):
        for value in ['20261104', '2026-11-31', '2026-11-04T00:00:00', 20261104]:
            self.request_file({'target_date': value})
            with self.subTest(value=value), self.assertRaises(ValueError):
                batch.collection_window()

    def run_batch(self, summary=None, messages=None):
        os.environ.update({'ARCHIVE_PASSWORD': 'synthetic-archive-password-long-enough',
                           'DISCORD_BOT_TOKEN': 'synthetic-bot-token'})
        (app.ROOT / 'reader-public.pem').write_bytes(b'synthetic public key')
        summary = summary or {'channels': [{'id': '2', 'name': 'example'}],
                              'coverage': {'complete': True, 'channels': []}}
        messages = messages or [{'content': 'synthetic private text', 'attachments': []}]
        with patch.object(batch, 'sync', return_value=summary) as sync, \
                patch.object(batch, 'read_messages', return_value=messages) as read, \
                patch('reader_crypto.seal', return_value=b'codex encrypted') as seal, \
                contextlib.redirect_stdout(io.StringIO()) as output:
            batch.main()
        self.assertNotIn('synthetic private text', output.getvalue())
        return sync, read, seal

    def test_daily_batch_preserves_both_encrypted_formats(self):
        self.request_file({'target_date': '2026-11-04'})
        _, read, seal = self.run_batch()
        read.assert_called_once_with(limit=None, since=batch.collection_window()['since'], until=batch.collection_window()['until'])
        snapshot = json.loads(seal.call_args.args[0])
        self.assertEqual(snapshot['limits']['target_date'], '2026-11-04')
        self.assertEqual(snapshot['limits']['pages_per_channel'], 100)
        self.assertEqual(snapshot['limits']['maximum_messages'], 30000)
        self.assertTrue(snapshot['coverage']['complete'])
        self.assertIn('Image metadata only', snapshot['limits']['attachment_storage'])
        legacy = batch.decrypt((app.ROOT / 'output' / 'discord-snapshot.enc').read_bytes(), os.environ['ARCHIVE_PASSWORD'])
        self.assertEqual(json.loads(legacy), snapshot['messages'])
        self.assertEqual((app.ROOT / 'output' / 'codex-snapshot.enc').read_bytes(), b'codex encrypted')

    def test_default_batch_keeps_original_limits(self):
        sync, read, seal = self.run_batch()
        sync.assert_called_once_with(max_pages=5)
        read.assert_called_once_with(limit=1000)
        snapshot = json.loads(seal.call_args.args[0])
        self.assertEqual(snapshot['limits']['pages_per_channel'], 5)
        self.assertNotIn('target_date', snapshot['limits'])

    def test_oversized_snapshot_fails_before_writing_archives(self):
        with patch.object(batch, 'MAX_SNAPSHOT_BYTES', 1), self.assertRaises(batch.SnapshotLimitError):
            self.run_batch()
        self.assertFalse((app.ROOT / 'output').exists())


if __name__ == '__main__':
    unittest.main()
