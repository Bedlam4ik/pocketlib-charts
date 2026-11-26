"""Publish ordered Hardcover charts suitable for PocketLib."""
from concurrent.futures import ThreadPoolExecutor
from html import unescape
import json
import re
import os
import sys
from pathlib import Path
from urllib.error import HTTPError, URLError
from urllib.parse import urlencode
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


def catalogue_json(host, path, query=None):
    url = host + path + ("?" + urlencode(query) if query else "")
    request = Request(url, headers={"User-Agent": "PocketLib Hardcover Feed", "Accept": "application/json"})
    with urlopen(request, timeout=7) as response:
        return json.load(response)


def find_fantlab_work(book, original_isbns, authors):
    host = "https://api.fantlab.ru"
    names = {compact(author) for author in authors}
    candidates = catalogue_json(host, "/search-works", {"q": book["title"], "onlymatches": "1"})
    if not isinstance(candidates, list):
        return None
    verified = []
    for row in candidates[:12]:
        if compact(row.get("name")) != compact(book["title"]):
            continue
        if compact(row.get("all_autor_name")) not in names:
            continue
        work_id = int(row["doc"])
        work = catalogue_json(host, f"/work/{work_id}/extended")
        if compact(work.get("work_name_orig") or work.get("work_name")) != compact(book["title"]):
            continue
        if not any(compact(author.get("name_orig")) in names for author in work.get("authors") or []):
            continue
        catalogued_isbns = set((work.get("editions_info") or {}).get("isbn_list", "").split("||"))
        verified.append((bool(set(original_isbns) & catalogued_isbns), work))
    exact_isbn = [work for matches_isbn, work in verified if matches_isbn]
    if len(exact_isbn) == 1:
        return exact_isbn[0]
    return verified[0][1] if len(verified) == 1 else None


def fantlab_russian_edition(work):
    if not work:
        return None
    titles = [
        entry["title"]
        for group in work.get("translations") or [] if group.get("lang_code") == "ru"
        for translation in group.get("translations") or []
        for entry in translation.get("titles") or []
        if entry.get("count", 0) and russian_text(entry.get("title"))
    ]
    if not titles:
        return None
    title = titles[0]
    editions = [
        row for group in (work.get("editions_blocks") or {}).values()
        for row in group.get("list") or []
        if row.get("lang_code") == "ru" and compact(row.get("name")) == compact(title)
    ]
    edition = editions[0] if editions else None
    numbers = []
    image = None
    if edition:
        numbers = [re.sub(r"[^0-9X]", "", value.upper()) for value in edition.get("isbn", "").split(",")]
        numbers = [number for number in numbers if len(number) in (10, 13)]
        if edition.get("pic_num"):
            image = f'https://fantlab.ru/images/editions/big/{int(edition["edition_id"])}'
    description = work.get("work_description")
    return {"title": title, "isbns": numbers, "cover": image,
            "description": description if russian_text(description) else None}


def open_library_russian_edition(original_isbns):
    if not original_isbns:
        return None, False
    host = "https://openlibrary.org"
    result = catalogue_json(host, "/search.json",
                            {"q": "isbn:(" + " OR ".join(original_isbns) + ")", "fields": "key", "limit": "2"})
    works = result.get("docs") or []
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
    return None, not result.get("docs")


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


def labirint_html(path):
    request = Request("https://www.labirint.ru" + path, headers={"User-Agent": "PocketLib Hardcover Feed"})
    with urlopen(request, timeout=7) as response:
        return response.read(4 << 20).decode("utf-8", errors="replace")


def labirint_description(russian_isbns):
    for isbn in russian_isbns:
        try:
            search = labirint_html(f"/search/{isbn}/")
            links = re.findall(r'<a\s+href="(/books/\d+/)"\s+class="product-card__img"', search)
            for path in list(dict.fromkeys(links))[:3]:
                page = labirint_html(path)
                identifiers = re.findall(r'<meta\b[^>]*itemprop="isbn"\s+content="([0-9Xx-]+)"', page)
                if isbn not in {value.replace("-", "").upper() for value in identifiers}:
                    continue
                annotation = re.search(
                    r'id="annotation".*?<div class="_content_[^"]*">(?:\s*<div[^>]*>)+(.+?)</div>',
                    page, re.DOTALL,
                )
                if annotation:
                    description = re.sub(r'<br\s*/?>|</p>', '\n', annotation[1], flags=re.IGNORECASE)
                    description = unescape(re.sub(r'<[^>]+>', '', description)).strip()
                    if russian_text(description):
                        return description
        except (URLError, TimeoutError):
            continue
    return None


def card(book):
    authors = book_authors(book)
    original_isbns = list(dict.fromkeys(isbns(book.get("default_physical_edition")) +
                                       isbns(book.get("default_ebook_edition"))))
    edition = next(
        (row for row in book["editions"] if russian_text(row.get("title"))), None
    )
    description = book.get("description")
    localized_title = edition["title"] if edition else None
    localized_isbns = isbns(edition)
    localized_cover = cover(edition)
    status = "russian_found" if edition else "unconfirmed"
    edition_source = "Hardcover" if edition else None
    description_source = "Hardcover" if russian_text(description) else None
    fantlab_checked = False
    match = None
    try:
        work = find_fantlab_work(book, original_isbns, authors)
        match = fantlab_russian_edition(work)
        if match:
            if not localized_title:
                localized_title, localized_isbns, localized_cover = match["title"], match["isbns"], match["cover"]
                status, edition_source = "russian_found", "Фантлаб"
            if not description_source and match["description"]:
                description, description_source = match["description"], "Фантлаб"
        elif work and not localized_title:
            fantlab_checked = bool((work.get("editions_info") or {}).get("isbn_list"))
    except (URLError, TimeoutError, ValueError, KeyError, TypeError):
        pass
    try:
        if not localized_title:
            library, checked = open_library_russian_edition(original_isbns)
            if library:
                localized_title, localized_isbns = library["title"], library["isbns"]
                status, edition_source = "russian_found", "Open Library"
            elif checked and fantlab_checked:
                status = "no_russian_in_catalog"
    except (URLError, TimeoutError, ValueError, KeyError, TypeError):
        pass
    if localized_title and not description_source:
        # Only look up ISBNs that belong to confirmed Russian editions of this work.
        russian_editions = [row for row in book["editions"] if russian_text(row.get("title"))]
        numbers = localized_isbns + [isbn for row in russian_editions for isbn in isbns(row)]
        if match:
            numbers += match["isbns"]
        numbers = list(dict.fromkeys(re.sub(r'[-\s]', '', value.upper()) for value in numbers))
        numbers = [isbn for isbn in numbers if re.fullmatch(r'\d{13}|\d{9}[\dX]', isbn)]
        if numbers:
            try:
                found = open_library_description(numbers)
            except (URLError, TimeoutError, ValueError, KeyError, TypeError):
                found = None
            if found:
                description, description_source = found, "Open Library"
            else:
                found = labirint_description(numbers)
                if found:
                    description, description_source = found, "Лабиринт"
    if status == "russian_found" and not description_source:
        description = None
    if status == "unconfirmed":
        description = None
    return {
        "id": book["id"],
        "url": "https://hardcover.app/books/" + book["slug"],
        "title": localized_title or (book["title"] if status == "no_russian_in_catalog" else None),
        "original_title": book["title"],
        "authors": list(dict.fromkeys(authors)),
        "published": book.get("release_date"),
        "rating": book.get("rating"),
        "rating_count": book.get("ratings_count"),
        "isbns": original_isbns,
        "russian_edition_id": edition["id"] if edition else None,
        "russian_isbns": localized_isbns,
        "cover_url": localized_cover or (cover(book) if status != "unconfirmed" else None),
        "russian_cover": bool(localized_cover),
        "edition_status": status,
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

    with ThreadPoolExecutor(max_workers=4) as workers:
        cards = dict(zip(ids, workers.map(card, (books[book_id] for book_id in ids))))

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
