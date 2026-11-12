"""Link to the existing authenticated application, never to a machine token."""
import os
from urllib.parse import urlsplit, urlunsplit


def history_reference():
    callback = urlsplit(os.getenv("SHADOW_OIDC_REDIRECT_URI", ""))
    if (callback.scheme == "https" and callback.netloc and not callback.username and not callback.password
            and callback.path.endswith("/auth/callback")):
        path = callback.path[:-len("/auth/callback")] + "/notifications"
        return "完整正文：" + urlunsplit((callback.scheme, callback.netloc, path, "", ""))
    return "完整正文：登录 Foliant，打开「通知记录」。"
