"""同じWi-Fiのスマホからカメラを使うための自己署名証明書を作る。

    python -m server.make_cert

var/cert.pem と var/key.pem を作り、.env に SSL_CERT / SSL_KEY を書く。
スマホでは最初に「安全ではありません」と出るので、「詳細」から進む。
（Webプッシュは自己署名証明書では動かない。CLAUDE.md の「他の端末から使う」を参照）
"""
import ipaddress
from datetime import datetime, timedelta, timezone

from cryptography import x509
from cryptography.hazmat.primitives import hashes, serialization
from cryptography.hazmat.primitives.asymmetric import ec
from cryptography.x509.oid import NameOID

from .app import ROOT, lan_addresses


def main():
    var = ROOT / "var"
    var.mkdir(exist_ok=True)
    key = ec.generate_private_key(ec.SECP256R1())
    ips = ["127.0.0.1", *lan_addresses()]
    san = x509.SubjectAlternativeName(
        [x509.DNSName("localhost")] + [x509.IPAddress(ipaddress.ip_address(a)) for a in ips])
    name = x509.Name([x509.NameAttribute(NameOID.COMMON_NAME, "kyou-wa-muri local")])
    now = datetime.now(timezone.utc)
    cert = (x509.CertificateBuilder()
            .subject_name(name).issuer_name(name)
            .public_key(key.public_key())
            .serial_number(x509.random_serial_number())
            .not_valid_before(now - timedelta(days=1))
            .not_valid_after(now + timedelta(days=365))
            .add_extension(san, critical=False)
            .sign(key, hashes.SHA256()))
    (var / "cert.pem").write_bytes(cert.public_bytes(serialization.Encoding.PEM))
    (var / "key.pem").write_bytes(key.private_bytes(
        serialization.Encoding.PEM, serialization.PrivateFormat.PKCS8, serialization.NoEncryption()))

    env = ROOT / ".env"
    lines = [l for l in (env.read_text(encoding="utf-8").splitlines() if env.exists() else [])
             if not l.startswith(("SSL_CERT=", "SSL_KEY="))]
    lines += ["SSL_CERT=var/cert.pem", "SSL_KEY=var/key.pem"]
    env.write_text("\n".join(lines) + "\n", encoding="utf-8")
    print("var/cert.pem と var/key.pem を作り、.env に設定しました。")
    print("対象のアドレス:", ", ".join(["localhost", *ips]))
    print("HTTPS をやめるときは .env の SSL_CERT / SSL_KEY の行を消してください。")


if __name__ == "__main__":
    main()
