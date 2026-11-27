"""Publish prepared NYT and Yandex book rankings."""
from concurrent.futures import ThreadPoolExecutor
from html.parser import HTMLParser
from pathlib import Path
import json
import re
from urllib.parse import urljoin, urlparse

from update import CATALOGUE_ERRORS, catalogue_html, plain_description, russian_text


NYT = "https://bestseller.com.ru/tops/new-york-times-bestsellers/hardcover-fiction/current"
YANDEX = "https://books.yandex.ru/section/all/popular-dn8StcVb"
YANDEX_ALIAS = "https://books.yandex.ru/books/s-popular"
VOID_TAGS = set("area base br col embed hr img input link meta param source track wbr".split())


class Page(HTMLParser):
    def __init__(self, raw):
        super().__init__()
        self.nodes = []
        self.stack = []
        self.feed(raw)

    def handle_starttag(self, tag, attrs):
        node = (tag, dict(attrs), [])
        (self.stack[-1][2] if self.stack else self.nodes).append(node)
        if tag not in VOID_TAGS:
            self.stack.append(node)

    def handle_endtag(self, tag):
        for index in range(len(self.stack) - 1, -1, -1):
            if self.stack[index][0] == tag:
                del self.stack[index:]
                break

    def handle_data(self, data):
        if self.stack:
            self.stack[-1][2].append(data)


def nodes(tree):
    for node in tree:
        if isinstance(node, tuple):
            yield node
            yield from nodes(node[2])


def has_class(node, name):
    return name in (node[1].get("class") or "").split()


def node_text(node):
    def pieces(children):
        for child in children:
            if isinstance(child, str):
                yield child
            elif child[0] not in {"button", "script", "style"}:
                yield from pieces(child[2])
    return " ".join(" ".join(pieces(node[2])).split()) if node else ""


def source_link(base, raw, domain, prefix="/"):
    if not raw:
        return ""
    url = urljoin(base, raw)
    parts = urlparse(url)
    host = parts.hostname or ""
    if (parts.scheme == "https" and not parts.username and not parts.password
            and (host == domain or host.endswith("." + domain)) and parts.path.startswith(prefix)):
        return url
    return ""


def nyt_title(value):
    match = re.fullmatch(r"(.+?) \((.+)\)", value)
    if match and russian_text(match[1]) != russian_text(match[2]):
        return (match[1], match[2]) if russian_text(match[1]) else (match[2], match[1])
    return value, value


def synopsis(url, source):
    try:
        page = Page(catalogue_html(url))
    except CATALOGUE_ERRORS:
        return ""
    if source == "nyt":
        section = next((n for n in nodes(page.nodes) if n[1].get("id") == "descr"), None)
        if section:
            return node_text(next((n for n in nodes(section[2]) if has_class(n, "bs-prose")), None))
    else:
        annotation = next((n for n in nodes(page.nodes) if n[1].get("data-test-id") == "EXPANDABLE_TEXT"), None)
        return node_text(annotation)
    return ""


def nyt_chart():
    page = Page(catalogue_html(NYT))
    revision = ""
    books = []
    for node in nodes(page.nodes):
        if not revision and has_class(node, "ttl"):
            date = re.search(r"Выпуск\s+(\d{4}-\d{2}-\d{2})", node_text(node))
            if date:
                revision = date[1]
        if node[0] != "article" or not has_class(node, "bs-rankrow"):
            continue
        fields = {value: next((n for n in nodes(node[2]) if has_class(n, "bs-rankrow__"+value)), None)
                  for value in ("title", "author", "note", "cover")}
        location = fields["title"] or fields["cover"]
        if not location:
            continue
        link = location if location[0] == "a" else next((n for n in nodes(location[2]) if n[0] == "a"), None)
        url = source_link(NYT, link[1].get("href") if link else "", "bestseller.com.ru", "/book/")
        title, original = nyt_title(node_text(fields["title"]))
        artwork = list(nodes(fields["cover"][2])) if fields["cover"] else []
        # The picture element declares a larger rendition for the same cover.
        picture = next((n[1].get("srcset") for n in artwork if n[0] == "source" and n[1].get("srcset")), None)
        img = next((n[1] for n in artwork if n[0] == "img"), {})
        cover = source_link(NYT, picture or img.get("data-src") or img.get("src"), "bestseller.com.ru")
        if title and url:
            books.append({"title": title, "original_title": original, "url": url, "cover_url": cover,
                          "author": nyt_title(node_text(fields["author"]))[0], "description": node_text(fields["note"])})
        if len(books) == 15:
            break
    if not revision or len(books) < 12:
        raise ValueError("Bestseller did not return a complete NYT issue")
    return books, revision


def yandex_chart():
    flight = re.compile(r'^self\.__next_f\.push\(\[1,("(?:[^"\\]|\\.)*")\]\)$')
    annotation = re.compile(r'"uuid":"([a-zA-Z0-9]+)","initUuid":"[a-zA-Z0-9]+","annotation":("(?:[^"\\]|\\.)*")')
    for url in (YANDEX, YANDEX_ALIAS):
        try:
            page = Page(catalogue_html(url))
        except CATALOGUE_ERRORS:
            continue
        books, descriptions = [], {}
        for node in nodes(page.nodes):
            if node[0] != "script" or not node[2] or not isinstance(node[2][0], str):
                continue
            script = node[2][0].strip().rstrip(";")
            if node[1].get("type") == "application/ld+json":
                for group in json.loads(script).get("@graph", []):
                    if group.get("@type") != "ItemList" or "за неделю" not in group.get("name", "").lower():
                        continue
                    for entry in group.get("itemListElement", []):
                        b = entry.get("item", {})
                        title = b.get("name", "")
                        link = source_link(url, b.get("url"), "books.yandex.ru", "/books/")
                        if b.get("@type") == "Book" and link and russian_text(title):
                            cover = source_link(url, b.get("image"), "bookmate.ru")
                            books.append({"title": title, "original_title": title, "url": link,
                                          "cover_url": cover, "author": ", ".join(a["name"] for a in b.get("author", []))})
            elif match := flight.fullmatch(script):
                for book_id, description in annotation.findall(json.loads(match[1])):
                    descriptions[book_id] = plain_description(json.loads(description))
        if len(books) >= 10:
            for book in books:
                book_id = urlparse(book["url"]).path.rstrip("/").split("/")[-1]
                book["description"] = descriptions.get(book_id, "")
            return books, ""
    raise ValueError("Yandex did not return a complete weekly chart")


def publish(source, loader):
    filename = Path(f"{source}.json")
    previous = json.loads(filename.read_text(encoding="utf-8")) if filename.exists() else {}
    books, revision = loader()
    if revision and revision < previous.get("revision", ""):
        return
    old = {b["url"]: b for b in previous.get("books", [])}

    def prepare(book):
        text = book["description"]
        if not text or text.endswith(("...", "…")):
            saved = old.get(book["url"], {}).get("description", "")
            if saved and not saved.endswith(("...", "…")):
                book["description"] = saved
            elif description := synopsis(book["url"], source):
                if len(description) > len(text):
                    book["description"] = description

    with ThreadPoolExecutor(max_workers=5) as workers:
        list(workers.map(prepare, books))
    if len({book["url"] for book in books}) != len(books):
        raise ValueError(f"Duplicate books in the {source} chart")
    feed = {"schema_version": 1, "source": source, "revision": revision, "books": books}
    filename.write_text(json.dumps(feed, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    print(f"Published {source}: {len(books)} books")


if __name__ == "__main__":
    failures = []
    with ThreadPoolExecutor(max_workers=2) as pool:
        jobs = {source: pool.submit(publish, source, loader) for source, loader in
                (("nyt", nyt_chart), ("yandex-week", yandex_chart))}
        for source, job in jobs.items():
            try:
                job.result()
            except CATALOGUE_ERRORS as error:
                failures.append(source)
                print(f"{source}: {error}")
    if failures:
        raise SystemExit(f"Failed to refresh: {', '.join(failures)}")
