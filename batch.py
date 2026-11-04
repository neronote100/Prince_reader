"""Produce an encrypted snapshot, without printing conversations."""
import json
import os
import secrets
from datetime import date, datetime, time, timedelta, timezone

from cryptography.hazmat.primitives.ciphers.aead import AESGCM
from cryptography.hazmat.primitives.kdf.scrypt import Scrypt
from discord_note import ROOT, read_messages, sync

JAPAN_TIME = timezone(timedelta(hours=9))
MAX_SNAPSHOT_BYTES = 10 * 1024 * 1024


class SnapshotLimitError(ValueError):
    """Safe category for a snapshot that cannot fit in the encrypted envelope."""


def collection_window():
    """A retained on-demand date must never change the daily scheduled job."""
    request_path = ROOT / 'requests' / 'collect.json'
    if os.environ.get('GITHUB_EVENT_NAME') == 'schedule' or not request_path.exists():
        return None
    if request_path.stat().st_size > 16384:
        raise ValueError('Collection request is too large')
    request_data = json.loads(request_path.read_text())
    target = request_data.get('target_date')
    if target is None:
        return None
    if not isinstance(target, str):
        raise ValueError('target_date must be YYYY-MM-DD')
    day = date.fromisoformat(target)
    if day.isoformat() != target:
        raise ValueError('target_date must be YYYY-MM-DD')
    start = datetime.combine(day, time.min, tzinfo=JAPAN_TIME)
    return {'target_date': target, 'since': start, 'until': start + timedelta(days=1)}


def key_for(password, salt):
    return Scrypt(salt=salt, length=32, n=2**14, r=8, p=1).derive(password.encode())


def encrypt(plaintext, password):
    salt, nonce = secrets.token_bytes(16), secrets.token_bytes(12)
    ciphertext = AESGCM(key_for(password, salt)).encrypt(nonce, plaintext, b'prince-reader-v1')
    return b'PRINCE1\n' + salt + nonce + ciphertext


def decrypt(payload, password):
    if not payload.startswith(b'PRINCE1\n'):
        raise ValueError('Unsupported archive')
    return AESGCM(key_for(password, payload[8:24])).decrypt(payload[24:36], payload[36:], b'prince-reader-v1')


def main():
    password = os.environ.get('ARCHIVE_PASSWORD', '')
    if len(password) < 20:
        raise ValueError('ARCHIVE_PASSWORD must contain at least 20 characters')
    if not os.environ.get('DISCORD_BOT_TOKEN'):
        raise ValueError('DISCORD_BOT_TOKEN is required')
    # Validate the complete allowlist before any network requests.
    from discord_note import config
    config()
    window = collection_window()
    range_options = {'since': window['since'], 'until': window['until']} if window else {}
    pages = 100 if window else 5
    summary = sync(max_pages=pages, **range_options)
    messages = read_messages(limit=None if window else 1000, **range_options)
    limits = {'pages_per_channel': pages,
              'maximum_messages': 30000 if window else 1000,
              'attachment_storage': 'Image metadata only; image files are not downloaded by Actions.',
              'note': 'Bot messages, external embeds, deletion tracking and automatic thread discovery are excluded.'}
    if window:
        limits.update({'target_date': window['target_date'], 'timezone': 'Asia/Tokyo',
                       'since': window['since'].astimezone(timezone.utc).isoformat(),
                       'until': window['until'].astimezone(timezone.utc).isoformat(),
                       'note': limits['note'] + ' Requested day may be incomplete if coverage is capped.'})
    else:
        limits['note'] += ' Newest messages only, not complete history.'
    snapshot = {'captured_at': datetime.now(timezone.utc).isoformat(),
                'channels': summary.get('channels', []), 'messages': messages,
                'limits': limits, 'coverage': summary.get('coverage', {})}
    encoded_snapshot = json.dumps(snapshot, ensure_ascii=False).encode()
    if len(encoded_snapshot) > MAX_SNAPSHOT_BYTES:
        raise SnapshotLimitError('Snapshot exceeds the encrypted payload size limit')
    output = ROOT / 'output'
    output.mkdir(mode=0o700, exist_ok=True)
    (output / 'discord-snapshot.enc').write_bytes(encrypt(json.dumps(messages, ensure_ascii=False).encode(), password))
    public_path = ROOT / 'reader-public.pem'
    if public_path.exists():
        from reader_crypto import seal
        (output / 'codex-snapshot.enc').write_bytes(seal(encoded_snapshot, public_path.read_bytes()))
    print('Encrypted snapshot created. Conversation text was not printed.')


if __name__ == '__main__':
    try:
        main()
    except SnapshotLimitError:
        raise SystemExit('Snapshot failed: encrypted payload size limit exceeded. Narrow the collection range.') from None
    except Exception:
        # Avoid tracebacks containing server responses or conversation contents in public CI logs.
        raise SystemExit('Snapshot failed. Check secret settings, bot permissions and network access.') from None
