"""Publish ordered Hardcover charts suitable for PocketLib."""
import json
import os
import sys
from pathlib import Path
from urllib.error import HTTPError, URLError
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
    return bool(value and any("А" <= char <= "я" or char in "Ёё" for char in value))


def card(book, rank):
    contributors = book.get("cached_contributors") or []
    authors = [entry["author"]["name"] for entry in contributors
               if isinstance(entry, dict) and isinstance(entry.get("author"), dict)
               and entry["author"].get("name") and not entry.get("contribution")]
    if not authors:
        authors = [entry["author"]["name"] for entry in contributors
                   if isinstance(entry, dict) and isinstance(entry.get("author"), dict)
                   and entry["author"].get("name")][:1]
    edition = next(
        (row for row in book["editions"] if russian_text(row.get("title"))), None
    )
    description = book.get("description")
    edition_cover = cover(edition)
    return {
        "rank": rank,
        "id": book["id"],
        "url": "https://hardcover.app/books/" + book["slug"],
        "title": edition["title"] if edition else book["title"],
        "original_title": book["title"],
        "authors": list(dict.fromkeys(authors)),
        "published": book.get("release_date"),
        "rating": book.get("rating"),
        "rating_count": book.get("ratings_count"),
        "isbns": list(dict.fromkeys(isbns(book.get("default_physical_edition")) +
                                     isbns(book.get("default_ebook_edition")))),
        "russian_edition_id": edition["id"] if edition else None,
        "russian_isbns": isbns(edition),
        "cover_url": edition_cover or cover(book),
        "russian_cover": bool(edition_cover),
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

    for period in ("month", "year"):
        data = {
            "schema_version": 1,
            "source": "Hardcover",
            "period": period,
            "books": [card(books[book_id], rank)
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

