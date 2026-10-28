import base64
import json
import stat
import tempfile
import unittest
from pathlib import Path

from cryptography.exceptions import InvalidTag

from reader_crypto import (
    generate_keypair,
    open_snapshot,
    public_key_id,
    seal,
)


class ReaderCryptoTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.temporary = tempfile.TemporaryDirectory()
        cls.addClassCleanup(cls.temporary.cleanup)
        cls.root = Path(cls.temporary.name)
        cls.private_path = cls.root / 'reader.pem'
        cls.public_pem = generate_keypair(cls.private_path)
        cls.other_private_path = cls.root / 'other.pem'
        generate_keypair(cls.other_private_path)

    def test_unicode_conversation_roundtrip(self):
        plaintext = json.dumps(
            [{'content': '実験から気づいたこと🌸\n次のnoteの記事にしたい。'}],
            ensure_ascii=False,
        ).encode('utf-8')
        payload = seal(plaintext, self.public_pem)
        envelope = json.loads(payload)
        self.assertEqual(envelope['version'], 2)
        self.assertEqual(envelope['key_id'], public_key_id(self.public_pem))
        self.assertNotIn(plaintext, payload)
        self.assertEqual(open_snapshot(payload, self.private_path), plaintext)

    def test_other_reader_cannot_decrypt(self):
        payload = seal(b'private conversation', self.public_pem)
        with self.assertRaises((ValueError, InvalidTag)):
            open_snapshot(payload, self.other_private_path)

    def test_modified_ciphertext_is_rejected(self):
        envelope = json.loads(seal(b'private conversation', self.public_pem))
        ciphertext = bytearray(base64.b64decode(envelope['ciphertext']))
        ciphertext[0] ^= 1
        envelope['ciphertext'] = base64.b64encode(ciphertext).decode('ascii')
        with self.assertRaises((ValueError, InvalidTag)):
            open_snapshot(json.dumps(envelope).encode(), self.private_path)

    def test_each_encryption_produces_a_new_snapshot(self):
        plaintext = b'same conversation'
        first = seal(plaintext, self.public_pem)
        second = seal(plaintext, self.public_pem)
        self.assertNotEqual(first, second)
        self.assertEqual(open_snapshot(first, self.private_path), plaintext)
        self.assertEqual(open_snapshot(second, self.private_path), plaintext)

    def test_private_key_is_owner_only(self):
        mode = stat.S_IMODE(self.private_path.stat().st_mode)
        self.assertEqual(mode, 0o600)

    def test_reinitializing_preserves_the_existing_reader_key(self):
        original = self.private_path.read_bytes()
        public_again = generate_keypair(self.private_path)
        self.assertEqual(public_again, self.public_pem)
        self.assertEqual(self.private_path.read_bytes(), original)

    def test_malformed_envelopes_are_rejected(self):
        malformed = [b'', b'not JSON', b'null', b'[]', b'{}', b'{"version":2}']
        for payload in malformed:
            with self.subTest(payload_type=payload[:20]):
                with self.assertRaises(ValueError):
                    open_snapshot(payload, self.private_path)

    def test_oversized_envelope_is_rejected(self):
        envelope = json.loads(seal(b'private conversation', self.public_pem))
        envelope['ciphertext'] = 'A' * (20 * 1024 * 1024)
        payload = json.dumps(envelope).encode()
        self.assertGreater(len(payload), 20 * 1024 * 1024)
        with self.assertRaises(ValueError):
            open_snapshot(payload, self.private_path)

    def test_changed_key_identifier_is_rejected(self):
        envelope = json.loads(seal(b'private conversation', self.public_pem))
        envelope['key_id'] = '0' * 64
        with self.assertRaises(ValueError):
            open_snapshot(json.dumps(envelope).encode(), self.private_path)


if __name__ == '__main__':
    unittest.main()
