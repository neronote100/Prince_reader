"""Read-only Discord ingestion. Tokens are read only from the environment."""
import argparse
import json
import os
import sqlite3
import time
import urllib.error
import urllib.parse
import urllib.request
from datetime import datetime, timedelta, timezone
from pathlib import Path

ROOT = Path(__file__).resolve().parent
DISCORD_EPOCH = datetime(2015, 1, 1, tzinfo=timezone.utc)
IMAGE_HOSTS = frozenset({'cdn.discordapp.com', 'media.discordapp.net'})
IMAGE_EXTENSIONS = frozenset({'.png', '.jpg', '.jpeg', '.webp', '.gif'})
MAX_RANGE_MESSAGES = 30000


def utc_datetime(value, *, allow_naive=False):
    """Parse Discord/ISO timestamps without depending on the machine timezone."""
    if isinstance(value, str):
        value = datetime.fromisoformat(value.replace('Z', '+00:00'))
    if not isinstance(value, datetime):
        raise ValueError('A timestamp must be an ISO datetime')
    if value.tzinfo is None or value.utcoffset() is None:
        if not allow_naive:
            raise ValueError('Date range timestamps must include a timezone')
        value = value.replace(tzinfo=timezone.utc)
    return value.astimezone(timezone.utc)


def utc_timestamp(value):
    # Fixed precision gives SQLite an exact, lexically sortable UTC value.
    return utc_datetime(value, allow_naive=True).isoformat(timespec='microseconds')


def date_range(since, until):
    if since is None and until is None:
        return None
    if since is None or until is None:
        raise ValueError('Both since and until are required')
    start, end = utc_datetime(since), utc_datetime(until)
    if start >= end or start < DISCORD_EPOCH:
        raise ValueError('Date range must begin in 2015 or later and end after it begins')
    return start, end


def snowflake_before(instant):
    """Exclusive snowflake boundary, including a partial final millisecond."""
    elapsed = instant - DISCORD_EPOCH
    milliseconds = elapsed // timedelta(milliseconds=1)
    if elapsed % timedelta(milliseconds=1):
        milliseconds += 1
    return str(milliseconds << 22)


def image_attachments(row):
    """Retain image metadata only, restricted to Discord's HTTPS CDN hosts."""
    result = []
    for attachment in row.get('attachments') or []:
        if not isinstance(attachment, dict):
            continue
        filename = attachment.get('filename', '')
        content_type = attachment.get('content_type')
        url = attachment.get('url')
        if not isinstance(filename, str) or not isinstance(url, str):
            continue
        is_image = (isinstance(content_type, str) and content_type.lower().startswith('image/'))
        if not is_image and Path(filename).suffix.lower() not in IMAGE_EXTENSIONS:
            continue
        try:
            parsed = urllib.parse.urlsplit(url)
            safe_url = (parsed.scheme == 'https' and parsed.hostname in IMAGE_HOSTS
                        and parsed.username is None and parsed.password is None
                        and parsed.port in (None, 443))
        except ValueError:
            safe_url = False
        if not safe_url:
            continue
        size = attachment.get('size')
        result.append({'url': url, 'filename': filename,
                       'size': size if isinstance(size, int) and not isinstance(size, bool) and size >= 0 else None,
                       'content_type': content_type if isinstance(content_type, str) else None})
    return result


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
    # Keep the original five-column table compatible with existing local tools.
    # This additive migration also gives old rows an empty attachment list.
    db.execute('CREATE TABLE IF NOT EXISTS message_attachments (message_id TEXT PRIMARY KEY, attachments TEXT NOT NULL)')
    db.execute('CREATE INDEX IF NOT EXISTS messages_channel_created ON messages (channel, created)')
    # Older databases used Discord's original timestamp/offset representation.
    # Normalize those rows once so exact range filters also work across offsets.
    old_rows = db.execute("SELECT id, created FROM messages WHERE created NOT LIKE '____-__-__T__:__:__.______+00:00'").fetchall()
    for message_id, created in old_rows:
        try:
            normalized = utc_timestamp(created)
        except (ValueError, TypeError):
            continue
        db.execute('UPDATE messages SET created = ? WHERE id = ?', (normalized, message_id))
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


def sync(max_pages=5, *, since=None, until=None):
    """Fetch allowed channels, optionally within an exact [since, until) range."""
    if not 1 <= max_pages <= 100:
        raise ValueError('max_pages must be between 1 and 100')
    window = date_range(since, until)
    cfg = config()
    count = 0
    channel_info = []
    coverage = []
    with database() as db:
        for channel in map(str, cfg['channel_ids']):
            meta = request(f'channels/{channel}')
            if str(meta.get('guild_id')) != str(cfg['guild_id']):
                raise ValueError('A channel does not belong to the configured server')
            channel_info.append({'id': channel, 'name': meta.get('name', channel)})
            before = snowflake_before(window[1]) if window else None
            complete, reason, pages = False, 'page_limit', 0
            for _ in range(max_pages):
                query = {'limit': 100}
                if before:
                    query['before'] = before
                rows = request(f'channels/{channel}/messages?' + urllib.parse.urlencode(query))
                pages += 1
                if not rows:
                    complete, reason = True, 'history_exhausted'
                    break
                reached_start = False
                for row in rows:
                    created = utc_datetime(row['timestamp'], allow_naive=True) if window else None
                    if window and created < window[0]:
                        reached_start = True
                    if window and not window[0] <= created < window[1]:
                        continue
                    # Do not retain usernames, external embeds, or bot messages.
                    if row.get('author', {}).get('bot'):
                        continue
                    db.execute('INSERT OR REPLACE INTO messages (id, channel, created, content, reply_to) VALUES (?, ?, ?, ?, ?)', (
                        row['id'], channel, utc_timestamp(row['timestamp']), row.get('content', ''),
                        (row.get('message_reference') or {}).get('message_id')))
                    db.execute('INSERT OR REPLACE INTO message_attachments VALUES (?, ?)', (
                        row['id'], json.dumps(image_attachments(row), ensure_ascii=False)))
                    count += 1
                next_before = min((row['id'] for row in rows), key=int)
                if before is not None and int(next_before) >= int(before):
                    raise RuntimeError('Discord pagination did not advance')
                before = next_before
                if reached_start:
                    complete, reason = True, 'start_reached'
                    break
                if len(rows) < 100:
                    complete, reason = True, 'history_exhausted'
                    break
            coverage.append({'id': channel, 'pages': pages, 'complete': complete,
                             'capped': not complete, 'reason': reason})
    return {'processed': count, 'channels': channel_info,
            'coverage': {'complete': all(item['complete'] for item in coverage), 'channels': coverage},
            'scope': ('Requested time range only.' if window else 'Newest configured pages only.')
                     + ' Deletion tracking and automatic thread discovery are not included.'}


def read_messages(limit=200, *, since=None, until=None):
    window = date_range(since, until)
    if not window and (limit is None or not 1 <= limit <= 1000):
        raise ValueError('limit must be between 1 and 1000')
    cfg = config()
    channels = list(map(str, cfg['channel_ids']))
    with database() as db:
        query = ('SELECT m.id, m.channel, m.created, m.content, m.reply_to, a.attachments '
                 'FROM messages m LEFT JOIN message_attachments a ON a.message_id = m.id '
                 'WHERE m.channel IN (' + ','.join('?' for _ in channels) + ')')
        parameters = list(channels)
        if window:
            query += ' AND m.created >= ? AND m.created < ?'
            parameters.extend(utc_timestamp(boundary) for boundary in window)
        query += ' ORDER BY m.created DESC, length(m.id) DESC, m.id DESC LIMIT ?'
        parameters.append(MAX_RANGE_MESSAGES + 1 if window else limit)
        rows = db.execute(query, parameters).fetchall()
        if window and len(rows) > MAX_RANGE_MESSAGES:
            raise ValueError('Requested range exceeds the snapshot message limit')
    return [{'created': row[2], 'content': row[3], 'reply_to': row[4],
             'attachments': json.loads(row[5]) if row[5] else [],
             'source': f'https://discord.com/channels/{cfg["guild_id"]}/{row[1]}/{row[0]}'} for row in reversed(rows)]


if __name__ == '__main__':
    parser = argparse.ArgumentParser()
    parser.add_argument('action', choices=['sync', 'read'])
    parser.add_argument('--pages', type=int, default=5)
    parser.add_argument('--limit', type=int, default=200)
    parser.add_argument('--since', help='Inclusive ISO datetime with timezone')
    parser.add_argument('--until', help='Exclusive ISO datetime with timezone')
    args = parser.parse_args()
    try:
        options = {'since': args.since, 'until': args.until}
        print(json.dumps(sync(args.pages, **options) if args.action == 'sync' else read_messages(args.limit, **options), ensure_ascii=False))
    except (ValueError, RuntimeError, OSError) as error:
        parser.exit(1, str(error) + '\n')
