#!/usr/bin/env python3
"""Download a page's source into a ZIP, keeping folder structure.

Two modes:
  (default)  real browser - runs JS/WebGL, captures every runtime asset
             (what Chrome F12 -> Sources shows). Needs: pip install playwright
                                                        python -m playwright install chromium
  --static   no browser - only HTML + CSS-referenced assets (fast, stdlib only)

Usage:
    python clone.py URL [output] [--wait SECONDS] [--head] [--static]

output   ends with .zip -> ZIP file;  otherwise -> a folder of the same name
--wait   seconds to keep the page open so lazy/gameplay assets load (default 8)
--head   show the browser window; play/click to trigger more loads, CLOSE it to save
--static skip the browser entirely (HTML + CSS assets only, no JS)

URL may omit the scheme ("google.com" -> "https://google.com").
"""
import sys, re, zipfile, posixpath
from urllib.parse import urljoin, urlparse, unquote
from urllib.request import Request, urlopen
from html.parser import HTMLParser

UA = {"User-Agent": "Mozilla/5.0 (site-cloner)"}


def local_path(base_netloc, url):
    """Map an absolute URL to a path inside the zip."""
    p = urlparse(url)
    path = unquote(p.path)
    if not path or path.endswith("/"):
        path += "index.html"
    path = path.lstrip("/")
    if p.netloc and p.netloc != base_netloc:
        path = f"_external/{p.netloc}/{path}"
    name = posixpath.normpath(path)
    if p.query:  # keep distinct query-string variants apart
        name += "__" + "".join(c if c.isalnum() else "_" for c in p.query)[:40]
    return name


# ---------------------------------------------------------------- static mode
ASSET_ATTRS = {"src", "href", "poster"}


class AssetFinder(HTMLParser):
    def __init__(self):
        super().__init__()
        self.urls = set()

    def handle_starttag(self, tag, attrs):
        d = dict(attrs)
        for a in ASSET_ATTRS:
            if d.get(a):
                self.urls.add(d[a])
        if d.get("srcset"):
            for part in d["srcset"].split(","):
                u = part.strip().split(" ")[0]
                if u:
                    self.urls.add(u)


def css_refs(css_bytes):
    txt = css_bytes.decode("utf-8", "replace")
    return (re.findall(r"url\(\s*['\"]?([^'\")]+)", txt)
            + re.findall(r"@import\s+['\"]([^'\"]+)", txt))


def fetch(url):
    with urlopen(Request(url, headers=UA), timeout=30) as r:
        return r.read(), r.headers.get_content_type()


def clone_static(url, out):
    base_netloc = urlparse(url).netloc
    html, _ = fetch(url)
    finder = AssetFinder()
    finder.feed(html.decode("utf-8", "replace"))
    for r in css_refs(html):
        finder.urls.add(r)

    seen = {}
    with zipfile.ZipFile(out, "w", zipfile.ZIP_DEFLATED) as z:
        root = local_path(base_netloc, url)
        z.writestr(root, html)
        seen[root] = True
        queue = [(url, r) for r in finder.urls]
        while queue:
            base, ref = queue.pop()
            if ref.startswith(("data:", "javascript:", "mailto:", "#")):
                continue
            absu = urljoin(base, ref)
            if not urlparse(absu).scheme.startswith("http"):
                continue
            name = local_path(base_netloc, absu)
            if name in seen:
                continue
            try:
                data, ctype = fetch(absu)
                z.writestr(name, data)
                seen[name] = True
                print("  +", name)
                if ctype == "text/css":
                    queue += [(absu, r) for r in css_refs(data)]
            except Exception as e:  # ponytail: skip failed asset, no retry
                print("  ! skip", absu, e)
    print(f"Done: {out} ({len(seen)} files)")


# --------------------------------------------------------------- browser mode
def clone_browser(url, out, wait, headless):
    import os
    from playwright.sync_api import sync_playwright  # imported only when used
    base_netloc = urlparse(url).netloc
    as_zip = out.lower().endswith(".zip")
    outdir = out if not as_zip else None
    files = {}  # name -> bytes  (zip mode: kept for final write; folder mode: written live)

    def save_to_folder(name, data):
        dest = os.path.join(outdir, name.replace("/", os.sep))
        os.makedirs(os.path.dirname(dest) or ".", exist_ok=True)
        with open(dest, "wb") as f:
            f.write(data)

    if outdir:
        os.makedirs(outdir, exist_ok=True)

    with sync_playwright() as pw:
        browser = pw.chromium.launch(headless=headless)
        page = browser.new_page()

        def on_response(resp):
            try:
                if resp.status >= 400 or resp.request.method != "GET":
                    return
                if resp.url.startswith(("data:", "blob:")):
                    return
                name = local_path(base_netloc, resp.url)
                if name in files:
                    return
                data = resp.body()
                files[name] = data
                if outdir:  # write live so nothing is lost if interrupted
                    save_to_folder(name, data)
                print(f"  + [{len(files):>4}] {name}", flush=True)
            except Exception:
                pass  # ponytail: unreadable body, skip

        page.on("response", on_response)
        print(f"Loading {url} ...", flush=True)
        page.goto(url, wait_until="load", timeout=60000)  # 'load' not 'networkidle': games never idle
        if headless:
            print(f"Loaded. Waiting {wait:g}s for lazy assets ...", flush=True)
            page.wait_for_timeout(int(wait * 1000))
        else:
            print("Loaded. Play/click in the window; CLOSE it when done to save "
                  f"(or auto-saves after {wait:g}s).", flush=True)
            try:  # end early the moment the user closes the browser window
                page.wait_for_event("close", timeout=wait * 1000)
            except Exception:
                pass
        try:
            browser.close()
        except Exception:
            pass

    total = len(files)
    if as_zip:
        with zipfile.ZipFile(out, "w", zipfile.ZIP_DEFLATED) as z:
            for i, (name, data) in enumerate(files.items(), 1):
                z.writestr(name, data)
                print(f"\r  zipping {i}/{total} ({i * 100 // total}%)",
                      end="", flush=True)
        print(f"\nDone: {out} ({total} files)")
    else:
        print(f"Done: {out}/ ({total} files)")


if __name__ == "__main__":
    args = sys.argv[1:]
    if not args:
        sys.exit(__doc__)
    static = "--static" in args
    headless = "--head" not in args
    wait = 8
    if "--wait" in args:
        wait = float(args[args.index("--wait") + 1])
    pos = [a for i, a in enumerate(args)
           if not a.startswith("--") and args[i - 1] != "--wait"]
    url = pos[0]
    if "://" not in url:  # allow bare "google.com"
        url = "https://" + url
    out = pos[1] if len(pos) > 1 else (urlparse(url).netloc or "site") + ".zip"
    if static:
        clone_static(url, out)
    else:
        clone_browser(url, out, wait, headless)
