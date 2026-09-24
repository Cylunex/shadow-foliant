"""Conservative, auditable news snippets for the holdings weekly report."""

from urllib.parse import urlsplit


OFFICIAL_HOSTS = ("cninfo.com.cn", "sse.com.cn", "szse.cn", "bse.cn",
                  "hkexnews.hk", "gov.cn")


def relevant_official_news(news: list[dict], holdings: list[dict], limit: int = 8) -> list[str]:
    """Require a named holding and an official source URL; assert no impact."""
    names = {str(row.get("name") or row.get("stock_name") or "").strip()
             for row in holdings}
    names = {name for name in names if len(name) >= 3}
    codes = {str(row.get("code") or row.get("symbol") or "").strip()[-6:]
             for row in holdings}
    codes = {code for code in codes if len(code) == 6 and code.isdigit()}
    output, seen = [], set()
    for item in news or []:
        title = str(item.get("title") or item.get("content") or "").strip()
        url = str(item.get("url") or item.get("link") or "").strip()
        host = (urlsplit(url).hostname or "").lower()
        if (not title or not host or not any(
                host == domain or host.endswith("." + domain) for domain in OFFICIAL_HOSTS)):
            continue
        if not any(name in title for name in names) and not any(code in title for code in codes):
            continue
        if url in seen:
            continue
        seen.add(url)
        when = str(item.get("time") or "")[:16]
        output.append(f"  [{when}] {title[:80]} {url}")
        if len(output) >= limit:
            break
    return output
