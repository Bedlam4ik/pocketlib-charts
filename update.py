"""Publish ordered Hardcover charts suitable for PocketLib."""
from concurrent.futures import ThreadPoolExecutor
from html import unescape
from http.client import HTTPException
import json
import re
import os
import sys
from pathlib import Path
from urllib.error import HTTPError, URLError
from urllib.parse import urlencode, urlparse
from urllib.request import Request, urlopen


BOOKS = """
query Books($ids: [Int!]!, $russian: Int!) {
  books(where: {id: {_in: $ids}}) {
    id slug title description release_date rating ratings_count cached_contributors cached_image
    default_physical_edition { isbn_10 isbn_13 }
    default_ebook_edition { isbn_10 isbn_13 }
    editions(where: {language_id: {_eq: $russian}}, order_by: {score: desc_nulls_last}, limit: 8) {
      id title isbn_10 isbn_13 cached_image
    }
  }
}
"""

# Cross-catalogue links verified against the work or publisher, keyed by Hardcover work ID.
FANTLAB_WORKS = {356467: 9633, 379760: 9632, 498097: 731357}
FANTLAB_COVERS = {356467: 327417}
VERIFIED_COVERS = {
    # The larger Hardcover image is a Coming Soon placeholder for this work.
    328491: "https://assets.hardcover.app/edition/12295826/e51fb38d185340f42f1b3f6eea1b6866991a8818.jpeg",
    379760: "https://fantlab.ru/images/editions/big/315021",
}
CORRECTED_EDITIONS = {379760: ("1984", "9785041118655")}
PUBLISHED_RUSSIAN_EDITIONS = {
    524963: ("Переломный момент", "https://heatedrivalry.kz/product/moment"),
    524964: ("Заклятые соперники", "https://heatedrivalry.kz/product/soperniki"),
}
PUBLISHER_EDITIONS = {
    266084: ("АСТ", "https://ast.ru/book/postoronniy-858196/", "9785171373238"),
    385491: ("Азбука", "https://azbooka.ru/books/ubiytsa-voyn", "9785389201804"),
    386401: ("АСТ", "https://ast.ru/book/grozovoy-pereval-852785/", "9785171272951"),
    469783: ("АСТ", "https://ast.ru/book/prestuplenie-i-nakazanie-034017/", "9785170619276"),
}
LITRES_EDITIONS = {
    446681: ("karl-obhodchik-podzemeliy-67643834", "Карл – обходчик подземелий", None),
    446730: ("povarennaya-kniga-anarhista-podzemelya-70208305", "Поваренная книга анархиста Подземелья", "9785222420287"),
    446722: ("vrata-dikih-bogov-70208311", "Врата Диких Богов", "9785222420294"),
}
RULIT_TRANSLATIONS = {
    818989: ("oko-nevesty-bedlama-download-1148010", "Око Невесты Бедлама", "Динниман"),
    1464466: ("etot-neotvratimyj-krah-download-1151434", "Этот неотвратимый крах", "Динниман"),
    1817542: ("parad-uzhasov-download-1154379", "Парад ужасов", "Динниман"),
    436502: ("korotkoe-prebyvanie-v-adu-download-1137434", "Короткое пребывание в аду", "Пек"),
}
CATALOGUE_ERRORS = (OSError, HTTPException, ValueError, KeyError, TypeError)


def graphql(query, variables=None):
    request = Request(
        "https://api.hardcover.app/v1/graphql",
        data=json.dumps({"query": query, "variables": variables or {}}).encode(),
        headers={
            "Authorization": "Bearer " + os.environ["HARDCOVER_TOKEN"],
            "Content-Type": "application/json",
            "User-Agent": "PocketLib",
        },
    )
    with urlopen(request, timeout=25) as response:
        result = json.load(response)
    if result.get("errors") or not result.get("data"):
        raise ValueError("Hardcover rejected the GraphQL request")
    return result["data"]


def cover(record):
    image = (record or {}).get("cached_image") or {}
    url = image.get("url") if isinstance(image, dict) else None
    return url if isinstance(url, str) and url.startswith("https://") else None


def isbns(record):
    return [record[field] for field in ("isbn_13", "isbn_10") if record and record.get(field)]


def russian_text(value):
    letters = [char for char in (value or "") if char.isalpha()]
    return bool(letters and sum("А" <= char <= "я" or char in "Ёё" for char in letters) * 2 >= len(letters))


def book_authors(book):
    contributors = book.get("cached_contributors") or []
    authors = [entry["author"]["name"] for entry in contributors
               if isinstance(entry, dict) and isinstance(entry.get("author"), dict)
               and entry["author"].get("name") and not entry.get("contribution")]
    if not authors:
        authors = [entry["author"]["name"] for entry in contributors
                   if isinstance(entry, dict) and isinstance(entry.get("author"), dict)
                   and entry["author"].get("name")][:1]
    return list(dict.fromkeys(authors))


def compact(value):
    return "".join(char for char in (value or "").casefold() if char.isalnum())


def author_key(value):
    return tuple(sorted(re.findall(r"[^\W_]+", value.casefold()))) if isinstance(value, str) else ()


def isbn_keys(values):
    keys = list(dict.fromkeys(re.sub(r"[-\s]", "", value.upper()) for value in values))
    return [key for key in keys if re.fullmatch(r"[0-9]{13}|[0-9]{9}[0-9X]", key)]


def catalogue_json(host, path, query=None):
    url = host + path + ("?" + urlencode(query) if query else "")
    request = Request(url, headers={"User-Agent": "PocketLib Hardcover Feed", "Accept": "application/json"})
    with urlopen(request, timeout=7) as response:
        return json.load(response)


def find_fantlab_work(book, original_isbns, authors):
    host = "https://api.fantlab.ru"
    names = {author_key(author) for author in authors}
    known_id = FANTLAB_WORKS.get(book.get("id"))
    if known_id:
        try:
            work = catalogue_json(host, f"/work/{known_id}/extended")
            if str(work.get("work_id")) == str(known_id):
                return work
        except CATALOGUE_ERRORS:
            pass
    candidates = catalogue_json(host, "/search-works", {"q": book["title"], "onlymatches": "1"})
    if not isinstance(candidates, list):
        return None
    verified = []
    for row in candidates[:12]:
        if author_key(row.get("all_autor_name") or "") not in names:
            continue
        work_id = int(row["doc"])
        work = catalogue_json(host, f"/work/{work_id}/extended")
        titles = [work.get("work_name_orig"), work.get("work_name"), *(work.get("work_name_alts") or [])]
        catalogued_isbns = set(isbn_keys((work.get("editions_info") or {}).get("isbn_list", "").split("||")))
        matches_isbn = bool(set(original_isbns) & catalogued_isbns)
        if not matches_isbn and compact(book["title"]) not in {compact(title) for title in titles}:
            continue
        if not any(names & {author_key(author.get(field) or "") for field in ("name_orig", "name", "real_name", "pseudoname")}
                   for author in work.get("authors") or []):
            continue
        verified.append((matches_isbn, work))
    exact_isbn = [work for matches_isbn, work in verified if matches_isbn]
    if len(exact_isbn) == 1:
        return exact_isbn[0]
    return verified[0][1] if len(verified) == 1 else None


def fantlab_russian_edition(work):
    if not work:
        return None
    titles = list(dict.fromkeys(
        entry["title"]
        for group in work.get("translations") or [] if group.get("lang_code") == "ru"
        for translation in group.get("translations") or []
        for entry in translation.get("titles") or []
        if entry.get("count", 0) and russian_text(entry.get("title"))
    ))
    if not titles:
        return None
    title = titles[0]
    title_keys = {compact(value) for value in titles}
    editions = [
        row for group in (work.get("editions_blocks") or {}).values()
        for row in group.get("list") or []
        if row.get("lang_code") == "ru" and compact(row.get("name")) in title_keys
    ]
    edition = next((row for row in editions if compact(row.get("name")) == compact(title)), None)
    numbers = isbn_keys(((edition or {}).get("isbn") or "").split(","))
    image = fantlab_cover(edition)
    description = work.get("work_description")
    return {"title": title, "isbns": numbers, "cover": image, "editions": editions,
            "description": description if russian_text(description) else None}


def fantlab_cover(edition):
    if edition and edition.get("pic_num") and str(edition.get("edition_id")).isdigit():
        return f'https://fantlab.ru/images/editions/big/{edition["edition_id"]}'
    return None


def open_library_russian_edition(original_isbns, title, authors):
    host = "https://openlibrary.org"
    works = []
    if original_isbns:
        result = catalogue_json(host, "/search.json",
                                {"q": "isbn:(" + " OR ".join(original_isbns) + ")", "fields": "key", "limit": "2"})
        works = result.get("docs") or []
    isbn_verified = len(works) == 1
    if not isbn_verified and authors:
        result = catalogue_json(host, "/search.json", {
            "title": title, "author": authors[0],
            "fields": "key,title,author_name", "limit": "12",
        })
        names = {author_key(author) for author in authors}
        works = [row for row in result.get("docs") or []
                 if compact(row.get("title")) == compact(title)
                 and {author_key(name) for name in row.get("author_name") or []} == names]
    if len(works) != 1:
        return None, False
    work_key = works[0].get("key", "")
    if not re.fullmatch(r"/works/OL[0-9]+W", work_key):
        return None, False
    result = catalogue_json(host, "/search.json", {
        "q": f'key:"{work_key}" AND language:rus', "lang": "ru",
        "fields": "key,editions,editions.title,editions.language,editions.isbn", "limit": "1",
    })
    for work in result.get("docs") or []:
        if work.get("key") != work_key:
            continue
        for edition in (work.get("editions") or {}).get("docs") or []:
            title = edition.get("title")
            if "rus" in edition.get("language", []) and russian_text(title):
                return {"title": title, "isbns": edition.get("isbn") or []}, True
    return None, isbn_verified and not result.get("docs")


def open_library_description(russian_isbns):
    keys = [f"ISBN:{isbn}" for isbn in russian_isbns]
    response = catalogue_json("https://openlibrary.org", "/api/books",
                              {"bibkeys": ",".join(keys), "jscmd": "details", "format": "json"})
    for key in keys:
        record = (response.get(key) or {}).get("details") or {}
        description = record.get("description")
        if isinstance(description, dict):
            description = description.get("value")
        if russian_text(description):
            return description
    return None


def catalogue_html(url):
    request = Request(url, headers={"User-Agent": "PocketLib Hardcover Feed"})
    with urlopen(request, timeout=7) as response:
        return response.read(4 << 20).decode("utf-8", errors="replace")


def labirint_description(russian_isbns):
    for isbn in russian_isbns:
        try:
            search = catalogue_html(f"https://www.labirint.ru/search/{isbn}/")
            links = re.findall(r'<a\s+href="(/books/\d+/)"\s+class="product-card__img"', search)
            for path in list(dict.fromkeys(links))[:3]:
                page = catalogue_html("https://www.labirint.ru" + path)
                identifiers = re.findall(r'<meta\b[^>]*itemprop="isbn"\s+content="([0-9Xx-]+)"', page)
                if isbn not in {value.replace("-", "").upper() for value in identifiers}:
                    continue
                annotation = re.search(
                    r'id="annotation".*?<div class="_content_[^"]*">(?:\s*<div[^>]*>)+(.+?)</div>',
                    page, re.DOTALL,
                )
                if annotation:
                    description = plain_description(annotation[1])
                    if russian_text(description):
                        return description
        except (OSError, HTTPException):
            continue
    return None


def plain_description(value):
    value = re.sub(r"<br\s*/?>|</p>", "\n", unescape(value), flags=re.IGNORECASE)
    return unescape(re.sub(r"<[^>]+>", "", value)).strip()


def publisher_description(book_id):
    if book_id in LITRES_EDITIONS:
        slug, title, isbn = LITRES_EDITIONS[book_id]
        try:
            page = catalogue_html(f"https://www.litres.ru/book/mett-dinniman/{slug}/")
        except (OSError, HTTPException):
            return None, None
        heading = re.search(r"<h1\b[^>]*>(.*?)</h1>", page, re.I | re.S)
        if not heading or compact(plain_description(heading[1])) != compact(title):
            return None, None
        if isbn:
            identifier = re.search(r'<span\b[^>]*itemprop="isbn"[^>]*>([^<]+)<', page, re.I)
            if not identifier or isbn not in isbn_keys([identifier[1]]):
                return None, None
        elif "мэттдинниман" not in compact(page):
            return None, None
        match = re.search(r'<div\b[^>]*data-testid="book-description__wrapper"[^>]*>(.*?)</div>', page, re.I | re.S)
        description = plain_description(match[1]) if match else ""
        return (description, "Литрес") if russian_text(description) else (None, None)
    edition = PUBLISHER_EDITIONS.get(book_id)
    if not edition:
        return None, None
    source, url, isbn = edition
    try:
        page = catalogue_html(url)
    except (OSError, HTTPException):
        return None, None
    page_isbns = re.findall(r"(?:[0-9][- ]?){12}[0-9]", page)
    if isbn not in isbn_keys(page_isbns):
        return None, None
    if source == "Азбука":
        match = re.search(r'<div\b[^>]*itemprop="description"[^>]*>(.*?)</div>', page, re.I | re.S)
    else:
        match = re.search(r'<meta\s+property="og:description"\s+content="([^"]+)"', page, re.I)
    description = plain_description(match[1]) if match else ""
    return (description, source) if russian_text(description) else (None, None)


def boys_and_books_description(url):
    try:
        page = catalogue_html(url)
    except (OSError, HTTPException):
        return None
    match = re.search(r'<div class="product__description-content static-text">\s*(<p>.*?)</div>', page, re.I | re.S)
    description = plain_description(match[1]) if match else ""
    return description if russian_text(description) else None


def rulit_translation(book_id):
    slug, title, surname = RULIT_TRANSLATIONS[book_id]
    url = f"https://www.rulit.me/books/{slug}.html"
    try:
        page = catalogue_html(url)
    except (OSError, HTTPException):
        return None
    headings = re.findall(r"<h2\b[^>]*>(.*?)</h2>", page, re.I | re.S)
    if not any(compact(plain_description(value)) == compact(title) for value in headings):
        return None
    author = re.search(r'Автор:\s*<a\b[^>]*>(.*?)</a>', page, re.I | re.S)
    if not author or surname.casefold() not in plain_description(author[1]).casefold().split():
        return None
    if not re.search(r'Язык:\s*<span\b[^>]*>русский\s*</span>', page, re.I):
        return None
    kind = "fan" if re.search(r'/genre/fan-translation["\']\s+class="post-cat"', page) else "unofficial"
    annotation = re.search(r'<div class="annotation">\s*<h4>Аннотация</h4>(.*?)</div>', page, re.S)
    description = plain_description(annotation[1]) if annotation else None
    return {"title": title, "kind": kind, "url": url, "description": description}


def cover_dimensions(url):
    if urlparse(url).hostname not in {"assets.hardcover.app", "fantlab.ru"}:
        return 0, 0
    request = Request(url, headers={"User-Agent": "PocketLib Hardcover Feed", "Range": "bytes=0-65535"})
    try:
        with urlopen(request, timeout=4) as response:
            data = response.read(65536)
    except (OSError, HTTPException):
        return 0, 0
    if data.startswith(b"\x89PNG\r\n\x1a\n") and len(data) >= 24:
        return int.from_bytes(data[16:20], "big"), int.from_bytes(data[20:24], "big")
    if len(data) >= 30 and data[:4] == b"RIFF" and data[8:16] == b"WEBPVP8X":
        return int.from_bytes(data[24:27], "little") + 1, int.from_bytes(data[27:30], "little") + 1
    if not data.startswith(b"\xff\xd8"):
        return 0, 0
    offset = 2
    while offset + 9 < len(data) and data[offset] == 255:
        marker = data[offset + 1]
        if marker == 255:
            offset += 1
            continue
        if marker in (192, 193, 194, 195, 197, 198, 199, 201, 202, 203, 205, 206, 207):
            return int.from_bytes(data[offset+7:offset+9], "big"), int.from_bytes(data[offset+5:offset+7], "big")
        length = int.from_bytes(data[offset+2:offset+4], "big")
        if marker in (217, 218) or length < 2:
            break
        offset += 2 + length
    return 0, 0


def better_cover(current, alternatives):
    def resolution(url):
        width, height = cover_dimensions(url)
        if not (0 < width <= 6000 and 0 < height <= 6000 and width * height <= 12000000):
            return 0
        return min(width * 3, height * 2)

    score = resolution(current) if current else 0
    if score >= 1296:
        return current
    for url in list(dict.fromkeys(url for url in alternatives if url and url != current))[:5]:
        candidate = resolution(url)
        # An unreadable current size is not evidence that a small replacement is better.
        if candidate > score and (not current or score or candidate >= 1296):
            current, score = url, candidate
        if score >= 1296:
            break
    return current


def card(book, previous=None):
    authors = book_authors(book)
    original_isbns = isbn_keys(isbns(book.get("default_physical_edition")) +
                              isbns(book.get("default_ebook_edition")))
    corrected_edition = CORRECTED_EDITIONS.get(book["id"])
    russian_editions = [] if corrected_edition else [row for row in book["editions"] if russian_text(row.get("title"))]
    edition = next(iter(russian_editions), None)
    description = book.get("description")
    localized_title = edition["title"] if edition else None
    localized_isbns = isbns(edition)
    localized_cover = cover(edition)
    status = "russian_found" if edition else "unconfirmed"
    edition_source = "Hardcover" if edition else None
    description_source = "Hardcover" if russian_text(description) else None
    fantlab_checked = False
    match = None
    if corrected_edition:
        # Hardcover associates an unrelated Russian book with this work.
        localized_title, isbn = corrected_edition
        localized_isbns = [isbn]
        status, edition_source = "russian_found", "Фантлаб"
    try:
        work = find_fantlab_work(book, original_isbns, authors)
        match = fantlab_russian_edition(work)
        if corrected_edition and work and not description_source and russian_text(work.get("work_description")):
            description, description_source = work["work_description"], "Фантлаб"
        if match:
            if not localized_title:
                localized_title, localized_isbns, localized_cover = match["title"], match["isbns"], match["cover"]
                status, edition_source = "russian_found", "Фантлаб"
            if not description_source and match["description"]:
                description, description_source = match["description"], "Фантлаб"
        elif work and not localized_title:
            fantlab_checked = bool((work.get("editions_info") or {}).get("isbn_list"))
    except CATALOGUE_ERRORS:
        pass
    try:
        if not localized_title:
            library, checked = open_library_russian_edition(original_isbns, book["title"], authors)
            if library:
                localized_title, localized_isbns = library["title"], library["isbns"]
                status, edition_source = "russian_found", "Open Library"
            elif checked and fantlab_checked:
                status = "no_russian_in_catalog"
    except CATALOGUE_ERRORS:
        pass
    publisher_edition = PUBLISHED_RUSSIAN_EDITIONS.get(book["id"])
    if publisher_edition and not localized_title:
        localized_title = publisher_edition[0]
        status, edition_source = "russian_found", "Boys&Books"
    previous = {} if corrected_edition else previous or {}
    if previous.get("edition_status") == "russian_found":
        if not localized_title:
            localized_title = previous["title"]
            edition_source, status = previous.get("edition_source"), "russian_found"
        if not localized_isbns and compact(localized_title) == compact(previous.get("title")):
            localized_isbns = previous.get("russian_isbns") or []
        if not description_source and previous.get("description_language") == "ru":
            description, description_source = previous.get("description"), previous.get("description_source")
    other_editions = (match or {}).get("editions") or []
    if localized_title and not description_source:
        numbers = localized_isbns + [isbn for row in russian_editions for isbn in isbns(row)]
        numbers += [isbn for row in other_editions for isbn in (row.get("isbn") or "").split(",")]
        numbers = isbn_keys(numbers)[:16]
        if publisher_edition:
            description_from_publisher, publisher = boys_and_books_description(publisher_edition[1]), "Boys&Books"
        else:
            description_from_publisher, publisher = publisher_description(book["id"])
        if description_from_publisher:
            description, description_source = description_from_publisher, publisher
        elif numbers:
            try:
                found = open_library_description(numbers)
            except CATALOGUE_ERRORS:
                found = None
            if found:
                description, description_source = found, "Open Library"
            else:
                found = labirint_description(numbers[:8])
                if found:
                    description, description_source = found, "Лабиринт"
    if status == "russian_found" and not description_source:
        description = None
    if localized_title and book["id"] in VERIFIED_COVERS:
        localized_cover = VERIFIED_COVERS[book["id"]]
    elif localized_title:
        previous_cover = previous.get("cover_url") if previous.get("russian_cover") else None
        other_editions = sorted(other_editions, key=lambda row: (
            not bool(set(isbn_keys((row.get("isbn") or "").split(","))) & set(isbn_keys(localized_isbns))),
            compact(row.get("name")) != compact(localized_title),
        ))
        preferred_cover = FANTLAB_COVERS.get(book["id"])
        alternatives = [cover(row) for row in russian_editions]
        if preferred_cover:
            alternatives.insert(0, f"https://fantlab.ru/images/editions/big/{preferred_cover}")
        alternatives.extend(fantlab_cover(row) for row in other_editions)
        localized_cover = better_cover(previous_cover or localized_cover, [localized_cover, *alternatives])
    if status == "unconfirmed":
        description = None
    translation = None
    if not localized_title and book["id"] in RULIT_TRANSLATIONS:
        translation = rulit_translation(book["id"])
        if not translation and previous.get("translation_kind") and previous.get("translation_url"):
            translation = {"title": previous["title"], "kind": previous["translation_kind"],
                           "url": previous["translation_url"], "description": previous.get("description")}
        if translation and russian_text(translation["description"]):
            description, description_source = translation["description"], "RuLit"
    return {
        "id": book["id"],
        "url": "https://hardcover.app/books/" + book["slug"],
        "title": localized_title or (translation or {}).get("title") or book["title"],
        "original_title": book["title"],
        "authors": list(dict.fromkeys(authors)),
        "published": book.get("release_date"),
        "rating": book.get("rating"),
        "rating_count": book.get("ratings_count"),
        "isbns": original_isbns,
        "russian_edition_id": edition["id"] if edition else previous.get("russian_edition_id"),
        "russian_isbns": localized_isbns,
        "cover_url": localized_cover or (cover(book) if status != "unconfirmed" else None),
        "russian_cover": bool(localized_cover),
        "edition_status": status,
        "translation_kind": (translation or {}).get("kind"),
        "translation_url": (translation or {}).get("url"),
        "edition_source": edition_source,
        "description_source": description_source,
        "description": description,
        "description_language": "ru" if russian_text(description) else None,
    }


def main():
    # Resolve the language identifier from Hardcover's own catalogue.
    lists = graphql('''{
      month: books_trending(duration: month, limit: 30) { ids error }
      year: books_trending(duration: one_year, limit: 100) { ids error }
      languages(where: {language: {_eq: "Russian"}}) { id }
    }''')
    languages = lists["languages"]
    if len(languages) != 1:
        raise ValueError("Could not resolve the Russian language in Hardcover")
    for period, expected in (("month", 30), ("year", 100)):
        chart = lists[period]
        if chart["error"] or len(chart["ids"] or []) != expected or len(set(chart["ids"])) != expected:
            raise ValueError(f"Hardcover did not return a complete {period} chart")

    ids = list(dict.fromkeys(lists["month"]["ids"] + lists["year"]["ids"]))
    books = {}
    for offset in range(0, len(ids), 25):
        batch = graphql(BOOKS, {"ids": ids[offset:offset + 25], "russian": languages[0]["id"]})
        books.update({book["id"]: book for book in batch["books"]})
    if any(book_id not in books or not books[book_id].get("title") or
           not books[book_id].get("slug") for book_id in ids):
        raise ValueError("Hardcover returned incomplete book records")

    previous = {}
    for period in ("year", "month"):
        path = Path(f"hardcover-{period}.json")
        if path.exists():
            for row in json.loads(path.read_text(encoding="utf-8"))["books"]:
                previous[row["id"]] = row
    with ThreadPoolExecutor(max_workers=4) as workers:
        cards = dict(zip(ids, workers.map(card, (books[book_id] for book_id in ids), map(previous.get, ids))))

    for period in ("month", "year"):
        data = {
            "schema_version": 1,
            "source": "Hardcover",
            "period": period,
            "books": [dict(cards[book_id], rank=rank)
                      for rank, book_id in enumerate(lists[period]["ids"], 1)],
        }
        Path(f"hardcover-{period}.json").write_text(
            json.dumps(data, ensure_ascii=False, indent=2) + "\n", encoding="utf-8"
        )


if __name__ == "__main__":
    try:
        main()
    except HTTPError as error:
        sys.exit(f"Hardcover HTTP {error.code}")
    except (URLError, ValueError, KeyError, TimeoutError) as error:
        sys.exit(f"Hardcover refresh failed: {error}")
