"""Local commands for key registration and safe artifact ingestion."""
import argparse
import json
import os
import zipfile
from pathlib import Path

from reader_crypto import MAX_PAYLOAD, generate_keypair, open_snapshot

ROOT = Path(__file__).resolve().parent
PRIVATE_PATH = ROOT / '.reader-keys' / 'private.pem'


def unpack(archive: Path, output: Path):
    # Read only the expected entry; never extract arbitrary ZIP paths.
    with zipfile.ZipFile(archive) as source:
        entry = source.getinfo('codex-snapshot.enc')
        if entry.file_size > MAX_PAYLOAD:
            raise ValueError('Artifact too large')
        payload = source.read(entry)
    plaintext = open_snapshot(payload, PRIVATE_PATH)
    data = json.loads(plaintext)
    if not isinstance(data, dict) or not isinstance(data.get('messages'), list):
        raise ValueError('Invalid conversation snapshot')
    output.parent.mkdir(parents=True, mode=0o700, exist_ok=True)
    fd = os.open(output, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)
    with os.fdopen(fd, 'wb') as target:
        target.write(plaintext)
    output.chmod(0o600)
    return {'message_count': len(data['messages']), 'channels': data.get('channels', []),
            'captured_at': data.get('captured_at'), 'oldest': min((m['created'] for m in data['messages']), default=None),
            'newest': max((m['created'] for m in data['messages']), default=None)}


if __name__ == '__main__':
    parser = argparse.ArgumentParser()
    sub = parser.add_subparsers(dest='action', required=True)
    sub.add_parser('init')
    read = sub.add_parser('unpack')
    read.add_argument('archive', type=Path)
    read.add_argument('--output', type=Path, default=ROOT / 'data' / 'latest.json')
    args = parser.parse_args()
    if args.action == 'init':
        public = generate_keypair(PRIVATE_PATH)
        path = ROOT / 'reader-public.pem'
        path.write_bytes(public)
        print('Reader key ready; only reader-public.pem may be uploaded.')
    else:
        print(json.dumps(unpack(args.archive, args.output), ensure_ascii=False))
