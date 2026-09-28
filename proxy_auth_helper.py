"""
Generate Chrome extension for proxy authentication.

Chrome --proxy-server doesn't support inline credentials (user:pass@host).
CDP Fetch.enable breaks with nodriver on navigation.
Solution: tiny MV2 extension that handles 407 Proxy-Authenticate.
"""

import json
import os

EXT_DIR = os.path.join(os.path.dirname(__file__), "proxy_auth_ext")

BACKGROUND_JS_TEMPLATE = """chrome.webRequest.onAuthRequired.addListener(
  function(details) {
    return {
      authCredentials: {
        username: %s,
        password: %s
      }
    };
  },
  {urls: ["<all_urls>"]},
  ["blocking"]
);
"""


def setup_proxy_auth_extension(username: str, password: str) -> str:
    """Write credentials into the extension and return extension path."""
    os.makedirs(EXT_DIR, exist_ok=True)
    bg_js = BACKGROUND_JS_TEMPLATE % (json.dumps(username), json.dumps(password))
    bg_path = os.path.join(EXT_DIR, "background.js")
    with open(bg_path, "w") as f:
        f.write(bg_js)
    os.chmod(bg_path, 0o600)
    return EXT_DIR
