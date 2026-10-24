"""Webプッシュ通知（Service Worker＋Push API＋VAPID）。

送信には pywebpush を使う。入っていなければ通知なしで動く（画面のデモボタンで代わりにできる）。
VAPIDキーは初回起動時に var/vapid_private.pem に作る（コミットしない）。
"""
import base64
import json
from pathlib import Path

try:
    from cryptography.hazmat.primitives import serialization
    from cryptography.hazmat.primitives.asymmetric import ec
    from py_vapid import Vapid
    from pywebpush import WebPushException, webpush
    AVAILABLE = True
except ImportError:
    AVAILABLE = False

CLAIMS_SUB = "mailto:demo@example.com"


class Push:
    def __init__(self, var_dir: Path):
        self.available = AVAILABLE
        self.public_key = None
        self._vapid = None
        if not AVAILABLE:
            return
        key_file = var_dir / "vapid_private.pem"
        if not key_file.exists():
            var_dir.mkdir(exist_ok=True)
            key = ec.generate_private_key(ec.SECP256R1())
            key_file.write_bytes(key.private_bytes(
                serialization.Encoding.PEM,
                serialization.PrivateFormat.PKCS8,
                serialization.NoEncryption(),
            ))
        self._vapid = Vapid.from_file(str(key_file))
        raw = self._vapid.public_key.public_bytes(
            serialization.Encoding.X962, serialization.PublicFormat.UncompressedPoint)
        self.public_key = base64.urlsafe_b64encode(raw).decode().rstrip("=")

    def send(self, subscriptions, title, body, url="/"):
        """全端末に送る。もう使えない購読（404/410）の endpoint を返す。"""
        dead = []
        if not self.available:
            return dead
        payload = json.dumps({"title": title, "body": body, "url": url}, ensure_ascii=False)
        for sub in subscriptions:
            try:
                webpush(sub, payload, vapid_private_key=self._vapid,
                        vapid_claims={"sub": CLAIMS_SUB}, ttl=3600, timeout=10)
            except WebPushException as e:
                code = getattr(e.response, "status_code", None)
                if code in (404, 410):
                    dead.append(sub["endpoint"])
                print(f"push failed ({code}): {e}")
            except Exception as e:  # ネットワークエラーなどで他の端末への送信を止めない
                print(f"push failed: {e}")
        return dead
