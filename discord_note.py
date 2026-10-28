"""Read-only Discord ingestion. Tokens are read only from the environment."""
import argparse
import json
import os
import sqlite3
import time
import urllib.error
import urllib.parse
import urllib.request
from pathlib import Path

ROOT = Path(__file__).resolve().parent


def config():
    raw = os.environ.get('DISCORD_CHANNEL_CONFIG')
    cfg = json.loads(raw) if raw else json.loads((ROOT / 'config.json').read_text())
    if not str(cfg.get('guild_id', '')).isdigit():
        raise ValueError('config.json: guild_id is required')
    ids = cfg.get('channel_ids', [])
    if not ids or any(not str(c).isdigit() for c in ids):
        raise ValueError('config.json: explicit channel_ids are required')
    return cfg


def database():
    path = ROOT / 'data'
    path.mkdir(mode=0o700, exist_ok=True)
    db = sqlite3.connect(path / 'messages.sqlite3')
    db.execute('CREATE TABLE IF NOT EXISTS messages (id TEXT PRIMARY KEY, channel TEXT, created TEXT, content TEXT, reply_to TEXT)')
    return db


def request(endpoint):
    token = os.environ.get('DISCORD_BOT_TOKEN')
    if not token:
        raise ValueError('DISCORD_BOT_TOKEN is not configured')
    url = 'https://discord.com/api/v10/' + endpoint
    for attempt in range(6):
        req = urllib.request.Request(url, headers={'Authorization': 'Bot ' + token, 'User-Agent': 'DiscordNote/1.0'})
        try:
            with urllib.request.urlopen(req, timeout=30) as response:
                return json.load(response)
        except urllib.error.HTTPError as error:
            if error.code == 429:
                delay = float(json.load(error).get('retry_after', 1))
                time.sleep(min(max(delay, 0.1), 60))
            elif error.code >= 500:
                time.sleep(2 ** attempt)
            else:
                raise RuntimeError(f'Discord access failed (HTTP {error.code}); check bot permissions and channel IDs') from None
    raise RuntimeError('Discord retry limit reached; run sync again later')


def sync(max_pages=5):
    """Fetch newest pages per allowed channel; reruns update retained messages."""
    if not 1 <= max_pages <= 100:
        raise ValueError('max_pages must be between 1 and 100')
    cfg = config()
    count = 0
    with database() as db:
        for channel in map(str, cfg['channel_ids']):
            meta = request(f'channels/{channel}')
            if str(meta.get('guild_id')) != str(cfg['guild_id']):
                raise ValueError('A channel does not belong to the configured server')
            before = None
            for _ in range(max_pages):
                query = {'limit': 100}
                if before:
                    query['before'] = before
                rows = request(f'channels/{channel}/messages?' + urllib.parse.urlencode(query))
                if not rows:
                    break
                for row in rows:
                    # Do not retain usernames, attachments or bot messages.
                    if row.get('author', {}).get('bot'):
                        continue
                    db.execute('INSERT OR REPLACE INTO messages VALUES (?, ?, ?, ?, ?)', (
                        row['id'], channel, row['timestamp'], row.get('content', ''),
                        row.get('message_reference', {}).get('message_id')))
                    count += 1
                before = min((row['id'] for row in rows), key=int)
                if len(rows) < 100:
                    break
    return {'processed': count, 'scope': 'Newest configured pages only; deletion tracking and automatic thread discovery are not included.'}


def read_messages(limit=200):
    if not 1 <= limit <= 1000:
        raise ValueError('limit must be between 1 and 1000')
    cfg = config()
    channels = list(map(str, cfg['channel_ids']))
    with database() as db:
        rows = db.execute('SELECT id, channel, created, content, reply_to FROM messages WHERE channel IN (' + ','.join('?' for _ in channels) + ') ORDER BY created DESC LIMIT ?', (*channels, limit)).fetchall()
    return [{'created': row[2], 'content': row[3], 'reply_to': row[4],
             'source': f'https://discord.com/channels/{cfg["guild_id"]}/{row[1]}/{row[0]}'} for row in reversed(rows)]


if __name__ == '__main__':
    parser = argparse.ArgumentParser()
    parser.add_argument('action', choices=['sync', 'read'])
    parser.add_argument('--pages', type=int, default=5)
    parser.add_argument('--limit', type=int, default=200)
    args = parser.parse_args()
    try:
        print(json.dumps(sync(args.pages) if args.action == 'sync' else read_messages(args.limit), ensure_ascii=False))
    except (ValueError, RuntimeError, OSError) as error:
        parser.exit(1, str(error) + '\n')
