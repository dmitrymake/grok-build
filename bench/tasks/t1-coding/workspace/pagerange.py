"""Parse compact page-range specifications into explicit page lists."""


def parse_page_range(spec):
    """Turn a spec like "1-3,5,7-9" into [1, 2, 3, 5, 7, 9].

    See TASK.md for the full contract this must satisfy.
    """
    pages = []
    for part in spec.split(","):
        if "-" in part:
            a, b = part.split("-")
            for n in range(int(a), int(b)):
                pages.append(n)
        else:
            pages.append(int(part))
    return pages
