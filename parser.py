import re

DESC_RE = re.compile(
    r"WIFI\s*[:：]\s*(?P<wifi>[^\-–:：]+?)\s*[-–]\s*PUBLI[CS]E?\s*IP\s*[:：]\s*"
    r"(?P<ip>\d{1,3}(?:\.\d{1,3}){3})",
    re.IGNORECASE,
)


def parse_description(text):
    if not text:
        return []
    found = []
    for match in DESC_RE.finditer(text):
        found.append(
            {
                "wifi": match.group("wifi").strip(),
                "ip": match.group("ip").strip(),
            }
        )
    return found
