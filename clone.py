#!/usr/bin/env python3
"""Download a page's source into a ZIP, keeping folder structure.

Two modes:
  (default)  real browser - runs JS/WebGL, captures every runtime asset
             (what Chrome F12 -> Sources shows). Needs: pip install playwright
                                                        python -m playwright install chromium
  --static   no browser - only HTML + CSS-referenced assets (fast, stdlib only)

Usage:
    python clone.py URL [output] [--wait SECONDS] [--head] [--static] [--no-subpages] [--depth N]

output       ends with .zip -> ZIP file;  otherwise -> a folder of the same name
--wait       seconds to keep the page open so lazy/gameplay assets load (default 8)
--head       show the browser window; play/click to trigger more loads, CLOSE it to save
--static     skip the browser entirely (HTML + CSS assets only, no JS)
--no-subpages  clone only the given URL, not pages linked under the same path (default: follows them)
--depth N    how many path levels below the start URL to crawl (default 10; past
             that it's just wasted probing - nobody nests paths that deep)

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
        self.links = set()  # <a href> page links, kept apart from asset refs

    def handle_starttag(self, tag, attrs):
        d = dict(attrs)
        if tag == "a" and d.get("href"):
            self.links.add(d["href"])
        else:
            for a in ASSET_ATTRS:
                if d.get(a):
                    self.urls.add(d[a])
        if d.get("srcset"):
            for part in d["srcset"].split(","):
                u = part.strip().split(" ")[0]
                if u:
                    self.urls.add(u)


def is_subpath(url, base_netloc, base_dir):
    """True if url is on the same site, under the same directory as the start URL."""
    p = urlparse(url)
    if p.netloc and p.netloc != base_netloc:
        return False
    return unquote(p.path or "/").startswith(base_dir)


def within_depth(url, base_dir, depth):
    """True if url is at most `depth` path levels below base_dir (0 = start page only)."""
    rel = unquote(urlparse(url).path or "/")
    rel = rel[len(base_dir):] if rel.startswith(base_dir) else rel
    return len([s for s in rel.split("/") if s]) <= depth


def candidate_subpaths(base_netloc, base_dir, depth, links):
    """Generic: from every link on the page (even links pointing off-site) derive
    candidate subpaths on THIS host and try them. Covers a homepage that links out to
    e.g. github.com/<user>/<repo> while hosting the live page at <host>/<repo>/.
    Tries the last segment AND every nested prefix, so sub-sub paths get probed too:
      .../a/b/c  ->  /a/  /a/b/  /a/b/c/  /c/
    Non-existent ones 404 and get skipped. Not host-specific."""
    out = set()
    for href in links:
        if href.startswith(("data:", "javascript:", "mailto:", "tel:", "#")):
            continue
        segs = [s for s in unquote(urlparse(href).path or "").split("/")
                if s and "." not in s]  # drop empties and file-looking segments
        if not segs:
            continue
        for i in range(len(segs)):  # cumulative prefixes: /a/, /a/b/, /a/b/c/
            out.add(f"https://{base_netloc}/{'/'.join(segs[:i + 1])}/")
        out.add(f"https://{base_netloc}/{segs[-1]}/")  # and the bare last segment
    return {u for u in out
            if is_subpath(u, base_netloc, base_dir) and within_depth(u, base_dir, depth)}


def harvest_refs(base_netloc, base_dir, depth, text):
    """Scan a page's raw text for ANY mention of a URL/path on this same host - not
    just <a href>, but inline scripts, JSON, data-attributes, plain text. Keeps the
    same-host subpaths. Runs alongside link-following to find pages faster."""
    cands = re.findall(r'https?://[^\s"\'<>()\\]+', text)
    cands += [f"https://{base_netloc}{p}"
              for p in re.findall(r'''["'(]\s*(/[^\s"'<>()]*)''', text)]
    out = set()
    for u in cands:
        p = urlparse(u)
        if p.netloc != base_netloc:  # off-site mentions handled by candidate_subpaths
            continue
        last = unquote(p.path).rstrip("/").split("/")[-1]
        if "." in last:  # file/asset-looking, not a page
            continue
        if is_subpath(u, base_netloc, base_dir) and within_depth(u, base_dir, depth):
            out.add(u)
    return out


def eta_str(t0, done, remaining):
    """Rough time-left estimate from average time per item processed so far."""
    import time
    if done <= 0:
        return "?"
    secs = int((time.time() - t0) / done * remaining)
    return f"{secs // 60}m{secs % 60:02d}s" if secs >= 60 else f"{secs}s"


def discover_sitemap(base_netloc, base_dir, depth):
    """Generic, works for any domain: pull URLs from robots.txt Sitemap: lines and
    sitemap.xml (recursing into sitemap-index files), keep same-site subpaths.
    This finds pages that aren't linked from anywhere. Silent on any failure."""
    found, tried, todo = [], set(), [
        f"https://{base_netloc}/robots.txt",
        f"https://{base_netloc}/sitemap.xml",
    ]
    while todo:
        u = todo.pop()
        if u in tried:
            continue
        tried.add(u)
        try:
            data, _ = fetch(u)
        except Exception:
            continue
        txt = data.decode("utf-8", "replace")
        if u.endswith("robots.txt"):
            todo += re.findall(r"(?im)^\s*sitemap:\s*(\S+)", txt)
            continue
        locs = re.findall(r"<loc>\s*([^<\s]+)\s*</loc>", txt)
        for loc in locs:
            if loc.endswith(".xml") and loc not in tried:  # sitemap index -> recurse
                todo.append(loc)
            elif is_subpath(loc, base_netloc, base_dir) and within_depth(loc, base_dir, depth):
                found.append(loc)
    return found


def css_refs(css_bytes):
    txt = css_bytes.decode("utf-8", "replace")
    return (re.findall(r"url\(\s*['\"]?([^'\")]+)", txt)
            + re.findall(r"@import\s+['\"]([^'\"]+)", txt))


def fetch(url):
    with urlopen(Request(url, headers=UA), timeout=30) as r:
        return r.read(), r.headers.get_content_type()


def clone_static(url, out, crawl_subpages=True, depth=10):
    base_netloc = urlparse(url).netloc
    base_path = unquote(urlparse(url).path) or "/"
    base_dir = base_path if base_path.endswith("/") else posixpath.dirname(base_path) + "/"

    seen = {}
    visited_pages = set()
    import time
    t0, done = time.time(), 0
    with zipfile.ZipFile(out, "w", zipfile.ZIP_DEFLATED) as z:
        page_queue = [url]
        if crawl_subpages:
            page_queue += discover_sitemap(base_netloc, base_dir, depth)
        asset_queue = []
        while page_queue:
            page_url = page_queue.pop(0)
            if page_url in visited_pages:
                continue
            visited_pages.add(page_url)
            try:
                html, _ = fetch(page_url)
            except Exception as e:
                print("  ! skip", page_url, e)
                continue
            done += 1
            name = local_path(base_netloc, page_url)
            if name not in seen:
                z.writestr(name, html)
                seen[name] = True
                left = len(page_queue) + len(asset_queue)
                print(f"  + {name}   [~{left} left, ETA {eta_str(t0, done, left)}]")

            text = html.decode("utf-8", "replace")
            finder = AssetFinder()
            finder.feed(text)
            for r in css_refs(html):
                finder.urls.add(r)
            asset_queue += [(page_url, r) for r in finder.urls]
            if crawl_subpages:
                for href in finder.links:
                    if href.startswith(("data:", "javascript:", "mailto:", "#")):
                        continue
                    absu = urljoin(page_url, href)
                    if (urlparse(absu).scheme.startswith("http")
                            and is_subpath(absu, base_netloc, base_dir)
                            and within_depth(absu, base_dir, depth)):
                        page_queue.append(absu)
                page_queue += list(candidate_subpaths(base_netloc, base_dir, depth, finder.links))
                page_queue += list(harvest_refs(base_netloc, base_dir, depth, text))

        while asset_queue:
            base, ref = asset_queue.pop()
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
                done += 1
                left = len(page_queue) + len(asset_queue)
                print(f"  + {name}   [~{left} left, ETA {eta_str(t0, done, left)}]")
                if ctype == "text/css":
                    asset_queue += [(absu, r) for r in css_refs(data)]
            except Exception as e:  # ponytail: skip failed asset, no retry
                print("  ! skip", absu, e)
    print(f"Done: {out} ({len(seen)} files)")


# --------------------------------------------------------------- browser mode
def clone_browser(url, out, wait, headless, crawl_subpages=True, depth=10):
    import os
    # frozen (PyInstaller) builds extract to a new temp dir each run, so pin
    # the browser cache to a stable folder instead of the driver's default
    os.environ.setdefault("PLAYWRIGHT_BROWSERS_PATH", os.path.join(os.path.expanduser("~"), "AppData", "Local", "ms-playwright"))
    from playwright.sync_api import sync_playwright  # imported only when used
    base_netloc = urlparse(url).netloc
    base_path = unquote(urlparse(url).path) or "/"
    base_dir = base_path if base_path.endswith("/") else posixpath.dirname(base_path) + "/"
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

        # crawling subpages only makes sense unattended; in --head mode the
        # user drives navigation themselves and closes the window to stop
        crawl = crawl_subpages and headless
        visited_pages = set()
        page_queue = [url]
        if crawl:
            page_queue += discover_sitemap(base_netloc, base_dir, depth)
        import time
        while page_queue:
            page_url = page_queue.pop(0)
            if page_url in visited_pages:
                continue
            visited_pages.add(page_url)
            print(f"Loading {page_url} ...", flush=True)
            try:
                page.goto(page_url, wait_until="load", timeout=60000)  # 'load' not 'networkidle': games never idle
            except Exception as e:
                print("  ! skip", page_url, e)
                continue
            if headless:
                print(f"Loaded. Collecting assets (up to {wait:g}s, "
                      "stops early once loading settles) ...", flush=True)
            else:
                print("Loaded. Play/click in the window; CLOSE it when done to save "
                      f"(or auto-saves after {wait:g}s).", flush=True)

            # live heartbeat so it never looks frozen; headless stops after a quiet spell
            deadline = time.time() + wait
            last, quiet = -1, 0
            while time.time() < deadline:
                try:
                    page.wait_for_timeout(1000)  # 1s tick
                except Exception:
                    break  # window closed
                if page.is_closed():
                    break
                n = len(files)
                quiet = quiet + 1 if n == last else 0
                last = n
                left = int(deadline - time.time())
                tail = "close window to save" if not headless else "no new assets"
                # ETA over the whole job: time left on this page + ~wait per still-queued page
                job_eta = left + int(len(page_queue) * wait) if headless else left
                print(f"\r  recording... {n} files | ~{len(page_queue)} pages queued | "
                      f"ETA ~{job_eta // 60}m{job_eta % 60:02d}s | {tail} {quiet}s   ",
                      end="", flush=True)
                if headless and quiet >= 12:  # 12s with nothing new = done loading
                    break
            print()
            if page.is_closed():
                break

            if crawl:
                try:
                    hrefs = page.eval_on_selector_all("a[href]", "els => els.map(e => e.href)")
                except Exception:
                    hrefs = []
                try:
                    text = page.content()  # rendered DOM: catches JS-mentioned URLs too
                except Exception:
                    text = ""
                for h in hrefs:
                    if (h not in visited_pages and is_subpath(h, base_netloc, base_dir)
                            and within_depth(h, base_dir, depth)):
                        page_queue.append(h)
                page_queue += list(candidate_subpaths(base_netloc, base_dir, depth, hrefs))
                page_queue += list(harvest_refs(base_netloc, base_dir, depth, text))

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
    crawl_subpages = "--no-subpages" not in args
    wait = 8
    if "--wait" in args:
        wait = float(args[args.index("--wait") + 1])
    depth = 10
    if "--depth" in args:
        depth = int(args[args.index("--depth") + 1])
    valued = {"--wait", "--depth"}  # flags whose following value isn't a positional
    pos = [a for i, a in enumerate(args)
           if not a.startswith("--") and args[i - 1] not in valued]
    url = pos[0]
    if "://" not in url:  # allow bare "google.com"
        url = "https://" + url
    # no output given -> a folder named after the site (simplest for non-tech users)
    out = pos[1] if len(pos) > 1 else (urlparse(url).netloc or "site")
    if static:
        clone_static(url, out, crawl_subpages, depth)
    else:
        clone_browser(url, out, wait, headless, crawl_subpages, depth)
