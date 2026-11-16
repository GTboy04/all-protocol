"""Local certificate references. Projects contain only an opaque identity ID."""

from dataclasses import asdict, dataclass
from datetime import datetime, timedelta, timezone
import json
import os
from pathlib import Path
from uuid import uuid4

from cryptography import x509
from cryptography.hazmat.primitives import hashes, serialization
from cryptography.hazmat.primitives.asymmetric import rsa
from cryptography.x509.oid import ExtendedKeyUsageOID, NameOID

from ...application.contracts import ClientError, ErrorKind


def default_directory():
    return Path(os.environ.get("LOCALAPPDATA", Path.home() / ".local/share")) / "AllProtocol/opcua"


@dataclass(frozen=True, slots=True)
class UaIdentity:
    certificate: str
    private_key: str
    trusted_server: str
    application_uri: str


class IdentityStore:
    def __init__(self, directory=None):
        self.directory = Path(directory) if directory is not None else default_directory()

    def load(self, identity_id):
        if not identity_id or not identity_id.isidentifier():
            raise ClientError(ErrorKind.INPUT, "请配置本机客户端证书、私钥和已信任的服务端证书")
        try:
            record = json.loads((self.directory / (identity_id + ".json")).read_text(encoding="utf-8"))
            identity = UaIdentity(**record)
            if any(not isinstance(value, str) or not value for value in asdict(identity).values()):
                raise ValueError()
            return identity
        except (OSError, ValueError, TypeError) as error:
            raise ClientError(ErrorKind.INPUT, "本机缺少此证书配置，请在当前计算机重新配置证书") from error

    def save(self, identity):
        # Read the public certificates now so configuration errors are reviewable locally.
        load_certificate(identity.certificate)
        load_certificate(identity.trusted_server)
        key = serialization.load_pem_private_key(Path(identity.private_key).read_bytes(), password=None)
        certificate = load_certificate(identity.certificate)
        if key.public_key().public_numbers() != certificate.public_key().public_numbers():
            raise ValueError("客户端证书与私钥不匹配")
        if not identity.application_uri or identity.application_uri not in certificate.extensions.get_extension_for_class(
                x509.SubjectAlternativeName).value.get_values_for_type(x509.UniformResourceIdentifier):
            raise ValueError("应用 URI 必须出现在客户端证书 SAN 中")
        self.directory.mkdir(parents=True, exist_ok=True)
        identity_id = "ua_" + uuid4().hex
        path = self.directory / (identity_id + ".json")
        with path.open("x", encoding="utf-8") as stream:
            json.dump(asdict(identity), stream, ensure_ascii=False)
        return identity_id

    def generate(self):
        """Create an application identity locally; never exported in project artifacts."""
        self.directory.mkdir(parents=True, exist_ok=True)
        name = "ua_" + uuid4().hex
        uri = "urn:allprotocol:client:" + name
        key = rsa.generate_private_key(public_exponent=65537, key_size=2048)
        subject = x509.Name([x509.NameAttribute(NameOID.COMMON_NAME, "AllProtocol Client")])
        now = datetime.now(timezone.utc)
        cert = (x509.CertificateBuilder().subject_name(subject).issuer_name(subject).public_key(key.public_key())
                .serial_number(x509.random_serial_number()).not_valid_before(now - timedelta(minutes=5))
                .not_valid_after(now + timedelta(days=365))
                .add_extension(x509.SubjectAlternativeName([x509.UniformResourceIdentifier(uri)]), critical=False)
                .add_extension(x509.BasicConstraints(ca=False, path_length=None), critical=True)
                .add_extension(x509.KeyUsage(True, True, True, True, False, False, False, None, None), critical=True)
                .add_extension(x509.ExtendedKeyUsage([ExtendedKeyUsageOID.CLIENT_AUTH]), critical=False)
                .sign(key, hashes.SHA256()))
        private_path, cert_path = self.directory / (name + ".pem"), self.directory / (name + ".der")
        with private_path.open("xb") as stream:
            stream.write(key.private_bytes(serialization.Encoding.PEM, serialization.PrivateFormat.PKCS8,
                                           serialization.NoEncryption()))
        private_path.chmod(0o600)
        cert_path.write_bytes(cert.public_bytes(serialization.Encoding.DER))
        return str(cert_path), str(private_path), uri


def load_certificate(path):
    content = Path(path).read_bytes()
    return x509.load_pem_x509_certificate(content) if b"-----BEGIN CERTIFICATE" in content else x509.load_der_x509_certificate(content)


def fingerprint(cert):
    return cert.fingerprint(hashes.SHA256()).hex(":").upper()
