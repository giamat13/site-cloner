#!/usr/bin/env python3
"""Download a page + its assets into a ZIP, keeping folder structure.
Usage: python clone.py https://example.com [output.zip]
"""
import sys, io, re, zipfile, posixpath
from urllib.parse import urljoin, urlparse, unquote
from urllib.request import Request, urlopen
from html.parser import HTMLParser

UA = {"User-Agent": "Mozilla/5.0 (site-cloner)"}
# attrs that hold a fetchable asset URL
ASSET_ATTRS = {"src", "href", "poster"}


def fetch(url):
    with urlopen(Request(url, headers=UA), timeout=30) as r:
        return r.read(), r.headers.get_content_type()


class AssetFinder(HTMLParser):
    def __init__(self):
        super().__init__()
        self.urls = set()

    def handle_starttag(self, tag, attrs):
        d = dict(attrs)
        for a in ASSET_ATTRS:
            if d.get(a):
                self.urls.add(d[a])
        if d.get("srcset"):  # img/source srcset: "url 1x, url 2x"
            for part in d["srcset"].split(","):
                u = part.strip().split(" ")[0]
                if u:
                    self.urls.add(u)


def local_path(base, url):
    """Map an absolute URL to a path inside the zip."""
    p = urlparse(url)
    path = unquote(p.path)
    if not path or path.endswith("/"):
        path += "index.html"
    path = path.lstrip("/")
    # keep other hosts under _external/<host>/
    if p.netloc and p.netloc != urlparse(base).netloc:
        path = f"_external/{p.netloc}/{path}"
    return posixpath.normpath(path)


def clone(url, out):
    html, _ = fetch(url)
    finder = AssetFinder()
    finder.feed(html.decode("utf-8", "replace"))

    # also grab url(...) refs inside inline CSS/style
    for m in re.findall(rb"url\(\s*['\"]?([^'\")]+)", html):
        finder.urls.add(m.decode("utf-8", "replace"))

    seen = {}
    with zipfile.ZipFile(out, "w", zipfile.ZIP_DEFLATED) as z:
        z.writestr(local_path(url, url), html)
        seen[local_path(url, url)] = True
        for ref in finder.urls:
            if ref.startswith(("data:", "javascript:", "mailto:", "#")):
                continue
            absu = urljoin(url, ref)
            if not urlparse(absu).scheme.startswith("http"):
                continue
            name = local_path(url, absu)
            if name in seen:
                continue
            try:
                data, _ = fetch(absu)
                z.writestr(name, data)
                seen[name] = True
                print("  +", name)
            except Exception as e:  # ponytail: skip failed asset, no retry
                print("  ! skip", absu, e)
    print(f"Done: {out} ({len(seen)} files)")


if __name__ == "__main__":
    if len(sys.argv) < 2:
        sys.exit("Usage: python clone.py URL [output.zip]")
    url = sys.argv[1]
    out = sys.argv[2] if len(sys.argv) > 2 else (urlparse(url).netloc or "site") + ".zip"
    clone(url, out)
