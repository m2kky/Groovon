import json, re
from typing import List, Dict


def parse_excel(content: bytes) -> List[Dict]:
    import openpyxl, io
    wb = openpyxl.load_workbook(io.BytesIO(content))
    sheet = wb.active
    headers = [str(c.value).lower().strip() if c.value else "" for c in sheet[1]]
    rows = []
    for row in sheet.iter_rows(min_row=2, values_only=True):
        d = dict(zip(headers, row))
        if d.get("title"):
            rows.append({"title": str(d["title"]), "venue": d.get("name"), "city": d.get("city")})
    return rows


def parse_json(content: bytes) -> List[Dict]:
    data = json.loads(content)
    if isinstance(data, list):
        items = data
    elif isinstance(data, dict):
        items = data.get("events", data.get("data", data.get("items", [data])))
    rows = []
    for item in items:
        title = item.get("title") or item.get("name") or item.get("event")
        if title:
            rows.append({"title": str(title), "venue": item.get("venue"), "city": item.get("city")})
    return rows


def parse_markdown(content: bytes) -> List[Dict]:
    text = content.decode("utf-8", errors="ignore")
    rows = []
    for line in text.splitlines():
        line = line.strip()
        if not line or line.startswith("#"):
            continue
        # Remove markdown formatting
        line = re.sub(r"[*_`\[\]|]", "", line).strip("- •").strip()
        if len(line) > 2:
            rows.append({"title": line, "venue": None, "city": None})
    return rows


def parse_html(content: bytes) -> List[Dict]:
    text = content.decode("utf-8", errors="ignore")
    # Extract text from common event tags
    patterns = [
        r'class="[^"]*(?:title|event|name)[^"]*"[^>]*>([^<]+)<',
        r'<(?:h[1-4]|td|li)[^>]*>([^<]{3,80})</',
    ]
    rows = []
    seen = set()
    for pattern in patterns:
        for match in re.finditer(pattern, text, re.IGNORECASE):
            title = re.sub(r"\s+", " ", match.group(1)).strip()
            if title and title not in seen and len(title) > 2:
                seen.add(title)
                rows.append({"title": title, "venue": None, "city": None})
    return rows


def parse_file(filename: str, content: bytes) -> List[Dict]:
    ext = filename.lower().split(".")[-1]
    if ext in ("xlsx", "xls"):
        return parse_excel(content)
    elif ext == "json":
        return parse_json(content)
    elif ext == "md":
        return parse_markdown(content)
    elif ext in ("html", "htm"):
        return parse_html(content)
    raise ValueError(f"Unsupported format: {ext}")
