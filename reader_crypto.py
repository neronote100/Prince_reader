"""Recipient-only encryption for Codex; no secrets belong in the repository."""
import base64
import hashlib
import json
import os
from pathlib import Path

from cryptography.hazmat.primitives import hashes, serialization
from cryptography.hazmat.primitives.asymmetric import padding, rsa
from cryptography.hazmat.primitives.ciphers.aead import AESGCM

MAX_PAYLOAD = 20 * 1024 * 1024
AAD = b'prince-reader-v2'


def _public_bytes(key):
    return key.public_bytes(serialization.Encoding.PEM, serialization.PublicFormat.SubjectPublicKeyInfo)


def generate_keypair(private_path: Path) -> bytes:
    private_path = Path(private_path)
    private_path.parent.mkdir(parents=True, mode=0o700, exist_ok=True)
    if private_path.exists():
        key = serialization.load_pem_private_key(private_path.read_bytes(), password=None)
        private_path.chmod(0o600)
        return _public_bytes(key.public_key())
    key = rsa.generate_private_key(public_exponent=65537, key_size=3072)
    encoded = key.private_bytes(serialization.Encoding.PEM, serialization.PrivateFormat.PKCS8, serialization.NoEncryption())
    try:
        fd = os.open(private_path, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
    except FileExistsError:
        return generate_keypair(private_path)
    with os.fdopen(fd, 'wb') as target:
        target.write(encoded)
    return _public_bytes(key.public_key())


def public_key_id(public_pem: bytes) -> str:
    key = serialization.load_pem_public_key(public_pem)
    if not isinstance(key, rsa.RSAPublicKey) or key.key_size < 3072:
        raise ValueError('A 3072-bit or stronger RSA recipient key is required')
    der = key.public_bytes(serialization.Encoding.DER, serialization.PublicFormat.SubjectPublicKeyInfo)
    return hashlib.sha256(der).hexdigest()


def _oaep():
    return padding.OAEP(mgf=padding.MGF1(hashes.SHA256()), algorithm=hashes.SHA256(), label=AAD)


def seal(plaintext: bytes, public_pem: bytes) -> bytes:
    if len(plaintext) > MAX_PAYLOAD // 2:
        raise ValueError('Snapshot too large')
    key_id = public_key_id(public_pem)
    public_key = serialization.load_pem_public_key(public_pem)
    data_key, nonce = os.urandom(32), os.urandom(12)
    ciphertext = AESGCM(data_key).encrypt(nonce, plaintext, AAD)
    def b64(value):
        return base64.b64encode(value).decode('ascii')
    return json.dumps({'version': 2, 'key_id': key_id,
                       'wrapped_key': b64(public_key.encrypt(data_key, _oaep())),
                       'nonce': b64(nonce), 'ciphertext': b64(ciphertext)}, sort_keys=True).encode()


def open_snapshot(payload: bytes, private_path: Path) -> bytes:
    if len(payload) > MAX_PAYLOAD:
        raise ValueError('Snapshot too large')
    envelope = json.loads(payload)
    if not isinstance(envelope, dict) or envelope.get('version') != 2:
        raise ValueError('Unsupported snapshot format')
    private = serialization.load_pem_private_key(Path(private_path).read_bytes(), password=None)
    if envelope.get('key_id') != public_key_id(_public_bytes(private.public_key())):
        raise ValueError('Snapshot belongs to a different reader; request a fresh snapshot')
    def unb64(field):
        return base64.b64decode(envelope[field], validate=True)
    data_key = private.decrypt(unb64('wrapped_key'), _oaep())
    return AESGCM(data_key).decrypt(unb64('nonce'), unb64('ciphertext'), AAD)
