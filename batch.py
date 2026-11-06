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
MAX_RANGE_DAYS = 366


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
    if not isinstance(request_data, dict):
        raise ValueError('Collection request must be a JSON object')
    has_target = 'target_date' in request_data
    has_range = 'start_date' in request_data or 'end_date' in request_data
    if has_target and has_range:
        raise ValueError('Use target_date or start_date/end_date, not both')
    if not has_target and not has_range:
        return None

    def calendar_date(field):
        value = request_data.get(field)
        if not isinstance(value, str):
            raise ValueError('Date fields must be YYYY-MM-DD')
        day = date.fromisoformat(value)
        if day.isoformat() != value:
            raise ValueError('Date fields must be YYYY-MM-DD')
        return day

    if has_target:
        first = last = calendar_date('target_date')
        selection = {'target_date': first.isoformat()}
    else:
        if 'start_date' not in request_data or 'end_date' not in request_data:
            raise ValueError('Both start_date and end_date are required')
        first, last = calendar_date('start_date'), calendar_date('end_date')
        selection = {'start_date': first.isoformat(), 'end_date': last.isoformat()}
    days = (last - first).days + 1
    if not 1 <= days <= MAX_RANGE_DAYS:
        raise ValueError('Collection range must contain 1 to 366 calendar days')
    if first < date(2015, 1, 1) or last == date.max:
        raise ValueError('Collection dates must fit Discord timestamp boundaries')
    return {**selection, 'since': datetime.combine(first, time.min, tzinfo=JAPAN_TIME),
            'until': datetime.combine(last + timedelta(days=1), time.min, tzinfo=JAPAN_TIME)}


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
        limits.update({key: window[key] for key in ('target_date', 'start_date', 'end_date') if key in window})
        limits.update({'timezone': 'Asia/Tokyo', 'maximum_range_days': MAX_RANGE_DAYS,
                       'since': window['since'].astimezone(timezone.utc).isoformat(),
                       'until': window['until'].astimezone(timezone.utc).isoformat(),
                       'note': limits['note'] + ' Requested period may be incomplete if coverage is capped.'})
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
