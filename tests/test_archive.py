import unittest
from cryptography.exceptions import InvalidTag
from batch import encrypt, decrypt


class ArchiveTests(unittest.TestCase):
    def test_roundtrip_and_randomization(self):
        password = 'test-only-password-at-least-20-characters'
        content = '公開しない会話'.encode()
        payload = encrypt(content, password)
        self.assertNotIn(content, payload)
        self.assertEqual(decrypt(payload, password), content)
        self.assertNotEqual(payload, encrypt(content, password))

    def test_wrong_password_rejected(self):
        with self.assertRaises(InvalidTag):
            decrypt(encrypt(b'private', 'correct'), 'wrong')

    def test_tampering_rejected(self):
        payload = bytearray(encrypt(b'private', 'correct'))
        payload[-1] ^= 1
        with self.assertRaises(InvalidTag):
            decrypt(bytes(payload), 'correct')
