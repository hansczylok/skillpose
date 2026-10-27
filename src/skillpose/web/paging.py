"""Long lists in pages: the size and the page are query parameters (?per=30&page=2), so a link and a reload keep them.

A bad value is no error: the size falls back to the default, the page to 1 (a page past the end to the last one). The
links are plain links, no script. ?posting=<hash> shows the page that holds that item, for a redirect after an action.
"""

from collections.abc import Callable, Sequence
from dataclasses import dataclass, field
from typing import ClassVar
from urllib.parse import urlencode

from fastapi import Request

PAGE_SIZES = (10, 30, 50, 100)
DEFAULT_SIZE = 30
KEPT_OUT = {"page", "per", "posting", "msg", "lang"}  # a link to another page of the list drops these
NEIGHBOURS = 1                                         # page numbers shown on each side of the current page


@dataclass
class Page[T]:
    items: list[T]
    total: int
    per: int
    number: int
    pages: int
    first: int      # 1-based position of the first item shown; 0 for an empty list
    last: int
    path: str
    query: list[tuple[str, str]] = field(default_factory=list)  # the other parameters of the address, kept in links

    sizes: ClassVar[tuple[int, ...]] = PAGE_SIZES  # the size buttons; the same for every list

    @property
    def show_sizes(self) -> bool:
        return self.total > PAGE_SIZES[0]

    @property
    def show_nav(self) -> bool:
        return self.pages > 1

    @property
    def numbers(self) -> list[int | None]:
        """The page numbers to show: the first, the last, the current one with its neighbours; None is a gap (…)."""
        wanted = {1, self.pages, *range(self.number - NEIGHBOURS, self.number + NEIGHBOURS + 1)}
        shown: list[int | None] = []
        for n in sorted(n for n in wanted if 1 <= n <= self.pages):
            if shown and shown[-1] is not None and n - shown[-1] > 1:
                shown.append(shown[-1] + 1 if n - shown[-1] == 2 else None)  # a gap of one page shows that page
            shown.append(n)
        return shown

    def href(self, *, page: int = 1, per: int | None = None) -> str:
        params = [*self.query, ("per", str(per or self.per)), ("page", str(page))]
        return f"{self.path}?{urlencode(params)}"


def _number(value: str | None) -> int | None:
    return int(value) if value and value.isascii() and value.isdigit() and len(value) <= 7 else None


def paginate[T](request: Request, items: Sequence[T], path: str, key: Callable[[T], str] | None = None) -> Page[T]:
    """The page of items that the address asks for. path: the address of the list itself, not of the request (a failed
    form re-renders the list as the answer to a POST, and the links must be GET links). key: the id of an item."""
    params = request.query_params
    per = _number(params.get("per"))
    per = per if per in PAGE_SIZES else DEFAULT_SIZE
    total = len(items)
    pages = max(1, -(-total // per))
    number = _number(params.get("page")) or 1
    wanted = params.get("posting")  # for a redirect that targets one item; no route does yet
    if wanted and key is not None:
        at = next((i for i, item in enumerate(items) if key(item) == wanted), None)
        if at is not None:
            number = at // per + 1
    number = min(number, pages)
    start = (number - 1) * per
    shown = list(items[start:start + per])
    query = [(k, v) for k, v in request.query_params.multi_items() if k not in KEPT_OUT]
    return Page(items=shown, total=total, per=per, number=number, pages=pages,
                first=start + 1 if shown else 0, last=start + len(shown), path=path, query=query)
