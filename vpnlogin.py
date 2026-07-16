"""
Nuist VPN SSO client

Features
- Simulates a real browser (headers, Accept-Language, referers, retries)
- Step1: GET /enlink/sso/login, parse <script> that defines indexConfig, extract `key`
- Step2: POST /enlink/sso/login/submit with:
    username: <username>
    password: AES-128-CBC(key=key, iv=reverse(key)) + PKCS#7 + base64
    token: key
  and browser-like headers including `Accept-Language: zh-CN,zh;q=0.9,en;q=0.8`
- Returns final cookies as a dict via `login_and_get_cookies()`

Dependencies
    pip install requests pycryptodome

Note
- This library does not hardcode the key length. AES-128 requires 16-byte key and IV.
  If the extracted key is not 16 bytes, a ValueError is raised (adjust if the site changes).
- Parsing is robust: it searches the JavaScript object assigned to `indexConfig` and extracts the `key` field.
"""
from __future__ import annotations

import base64
import re
from dataclasses import dataclass, field
from typing import Dict, Optional

import requests
from requests.adapters import HTTPAdapter
from urllib3.util.retry import Retry

try:
    from Crypto.Cipher import AES  # pycryptodome
except ImportError as e:
    raise SystemExit("pycryptodome is required. Install with: pip install pycryptodome") from e


DEFAULT_UA = (
    "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
    "AppleWebKit/537.36 (KHTML, like Gecko) "
    "Chrome/127.0.0.0 Safari/537.36"
)


def _pkcs7_pad(data: bytes, block_size: int = 16) -> bytes:
    pad_len = block_size - (len(data) % block_size)
    return data + bytes([pad_len]) * pad_len


@dataclass
class NuistVPNClient:
    username: str
    password: str
    base_url: str = "https://client.vpn.nuist.edu.cn"
    timeout: int = 20
    user_agent: str = DEFAULT_UA
    session: requests.Session = field(default_factory=requests.Session, init=False)
    _current_key: Optional[str] = field(default=None, init=False, repr=False)

    def __post_init__(self) -> None:
        # Set browser-like defaults
        self.session.headers.update(
            {
                "User-Agent": self.user_agent,
                "Accept": "text/html,application/xhtml+xml,application/xml;q=0.9,image/avif,image/webp,*/*;q=0.8",
                "Accept-Language": "zh-CN,zh;q=0.9,en;q=0.8",
                "Connection": "keep-alive",
                "Upgrade-Insecure-Requests": "1",
            }
        )
        # Robust retries for flaky networks
        retry = Retry(
            total=3,
            backoff_factor=0.4,
            status_forcelist=(429, 500, 502, 503, 504),
            allowed_methods=("GET", "POST"),
            raise_on_status=False,
        )
        adapter = HTTPAdapter(max_retries=retry)
        self.session.mount("https://", adapter)
        self.session.mount("http://", adapter)

    # ---------------------- Public API ----------------------
    def login_and_get_cookies(self) -> Dict[str, str]:
        """Perform Step1 and Step2, then return cookies as a plain dict."""
        self._ensure_key()
        self._post_credentials()
        return {c.name: c.value for c in self.session.cookies}

    # ---------------------- Internals ----------------------
    def _ensure_key(self) -> str:
        if self._current_key:
            return self._current_key
        url = f"{self.base_url}/enlink/sso/login"
        resp = self.session.get(url, timeout=self.timeout)
        resp.raise_for_status()
        key = self._extract_key_from_html(resp.text)
        if not key:
            raise ValueError("Failed to extract 'key' from indexConfig on the login page.")
        self._current_key = key
        return key

    def _post_credentials(self) -> None:
        assert self._current_key is not None, "Key must be fetched before posting credentials."
        key = self._current_key
        enc_pwd = self._encrypt_password(self.password, key)

        url = f"{self.base_url}/enlink/sso/login/submit"
        # Browser-ish headers for XHR form submit
        headers = {
            "Accept": "application/json, text/javascript, */*; q=0.01",
            "Accept-Language": "zh-CN,zh;q=0.9,en;q=0.8",
            "Content-Type": "application/x-www-form-urlencoded; charset=UTF-8",
            "Origin": self.base_url,
            "Referer": f"{self.base_url}/enlink/sso/login",
            "X-Requested-With": "XMLHttpRequest",
            "User-Agent": self.user_agent,
        }
        # If the endpoint expects form-encoded body
        form = {
            "username": self.username,
            "password": enc_pwd,
            "token": key,
            "language": "zh-CN,zh;q=0.9,en;q=0.8"
        }
        resp = self.session.post(url, data=form, headers=headers, timeout=self.timeout)
        resp.raise_for_status()
        # Some SSO flows set cookies via redirects or response headers.
        # requests.Session will keep them in self.session.cookies automatically.

    @staticmethod
    def _extract_key_from_html(html: str) -> Optional[str]:
        """
        Parse the HTML, find the <script> where `indexConfig = {...}` is defined, and extract `key`.
        This avoids strict JSON parsing by grabbing the key value directly within the object literal.
        """
        # Locate the indexConfig object first
        obj_match = re.search(r"indexConfig\s*=\s*\{.*?\}", html, re.DOTALL)
        if not obj_match:
            return None
        obj_text = obj_match.group(0)
        # Extract the key field value inside the object literal
        key_match = re.search(r"['\"]key['\"]\s*:\s*['\"]([^'\"\\]+)['\"]", obj_text)
        if not key_match:
            return None
        return key_match.group(1)

    @staticmethod
    def _encrypt_password(plaintext: str, key_str: str) -> str:
        """
        AES-128-CBC with PKCS#7 padding; IV is the reversed key string.
        Returns base64-encoded ciphertext (utf-8 str) suitable for form submission.
        """
        key_bytes = key_str.encode("utf-8")
        iv_bytes = key_str[::-1].encode("utf-8")
        if len(key_bytes) != 16:
            raise ValueError(f"Expected a 16-byte AES-128 key, got {len(key_bytes)} bytes.")
        if len(iv_bytes) != 16:
            raise ValueError(f"Expected a 16-byte IV (reversed key), got {len(iv_bytes)} bytes.")

        cipher = AES.new(key_bytes, AES.MODE_CBC, iv=iv_bytes)
        padded = _pkcs7_pad(plaintext.encode("utf-8"), 16)
        ct = cipher.encrypt(padded)
        return base64.b64encode(ct).decode("utf-8")


# ---------------------- Example usage ----------------------
# if __name__ == "__main__":
#     client = NuistVPNClient(username="your_username", password="your_password")
#     cookies = client.login_and_get_cookies()
#     print(cookies)
