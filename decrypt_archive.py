"""Decrypt downloaded snapshots locally, never to standard output."""
import argparse
import getpass
import os
from pathlib import Path
from batch import decrypt

if __name__ == '__main__':
    parser = argparse.ArgumentParser()
    parser.add_argument('archive', type=Path)
    parser.add_argument('--output', type=Path, default=Path('data/conversations.json'))
    args = parser.parse_args()
    password = os.environ.get('ARCHIVE_PASSWORD') or getpass.getpass('Archive password: ')
    try:
        plaintext = decrypt(args.archive.read_bytes(), password)
        args.output.parent.mkdir(mode=0o700, parents=True, exist_ok=True)
        fd = os.open(args.output, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
        with os.fdopen(fd, 'wb') as target:
            target.write(plaintext)
    except Exception:
        raise SystemExit('Decryption failed; check password, archive and output path (must not exist).') from None
    print('Snapshot saved locally. Keep it outside version control.')
