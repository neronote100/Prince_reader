"""Produce an encrypted snapshot, without printing conversations."""
import json
import os
import secrets
from datetime import datetime, timezone

from cryptography.hazmat.primitives.ciphers.aead import AESGCM
from cryptography.hazmat.primitives.kdf.scrypt import Scrypt
from discord_note import ROOT, read_messages, sync


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
    summary = sync(max_pages=5)
    messages = read_messages(limit=1000)
    output = ROOT / 'output'
    output.mkdir(mode=0o700, exist_ok=True)
    (output / 'discord-snapshot.enc').write_bytes(encrypt(json.dumps(messages, ensure_ascii=False).encode(), password))
    public_path = ROOT / 'reader-public.pem'
    if public_path.exists():
        from reader_crypto import seal
        snapshot = {'captured_at': datetime.now(timezone.utc).isoformat(),
                    'channels': summary.get('channels', []), 'messages': messages,
                    'limits': {'pages_per_channel': 5, 'maximum_messages': 1000,
                               'note': 'Snapshot, not complete history. Bot messages and attachments excluded.'}}
        (output / 'codex-snapshot.enc').write_bytes(seal(json.dumps(snapshot, ensure_ascii=False).encode(), public_path.read_bytes()))
    print('Encrypted snapshot created. Conversation text was not printed.')


if __name__ == '__main__':
    try:
        main()
    except Exception:
        # Avoid tracebacks containing server responses or conversation contents in public CI logs.
        raise SystemExit('Snapshot failed. Check secret settings, bot permissions and network access.') from None
