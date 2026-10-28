import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch
import discord_note as app


class SyncTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.root = patch.object(app, 'ROOT', Path(self.temp.name))
        self.root.start()
        self.addCleanup(self.root.stop)
        (app.ROOT / 'config.json').write_text('{"guild_id":"1","channel_ids":["2"]}')

    def test_sync_deduplicates_and_updates(self):
        row = {'id': '10', 'timestamp': '2026-10-27', 'content': 'first'}
        with patch.object(app, 'request', side_effect=[{'guild_id': '1'}, [row]]):
            app.sync()
        row['content'] = 'edited'
        with patch.object(app, 'request', side_effect=[{'guild_id': '1'}, [row]]):
            app.sync()
        result = app.read_messages()
        self.assertEqual(len(result), 1)
        self.assertEqual(result[0]['content'], 'edited')
        self.assertEqual(result[0]['source'], 'https://discord.com/channels/1/2/10')

    def test_wrong_server_rejected(self):
        with patch.object(app, 'request', return_value={'guild_id': '9'}):
            with self.assertRaises(ValueError):
                app.sync()

    def test_removed_channel_not_exposed(self):
        with app.database() as db:
            db.execute('INSERT INTO messages VALUES (?,?,?,?,?)', ('1', '99', 'today', 'private', None))
        self.assertEqual(app.read_messages(), [])

    def test_bots_excluded(self):
        with patch.object(app, 'request', side_effect=[{'guild_id': '1'}, [{'id': '10', 'author': {'bot': True}}]]):
            app.sync()
        self.assertEqual(app.read_messages(), [])


if __name__ == '__main__':
    unittest.main()
