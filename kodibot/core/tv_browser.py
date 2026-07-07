import re
import requests
import logging
from kodibot.config import CFG

log = logging.getLogger(__name__)

_QUALITY_RE = re.compile(
    r'\s*\((?:1080p?|720p?|480p?|4k|uhd|fhd|hd|sd)\)\s*$'
    r'|\s+(?:1080p|720p|480p|4k|uhd|fhd|hd|sd)\s*$',
    re.IGNORECASE,
)


def _normalize_name(name: str) -> str:
    result = name
    while True:
        stripped = _QUALITY_RE.sub("", result).strip()
        if stripped == result:
            break
        result = stripped
    return result.lower()


def _url_reachable(url: str) -> bool:
    try:
        r = requests.head(url, timeout=3, allow_redirects=True,
                          headers={"User-Agent": "Mozilla/5.0"})
        return r.status_code not in (404, 410)
    except Exception:
        return False


def _load_m3u(source: str) -> str | None:
    if source.startswith("/") or source.startswith("file://"):
        path = source.removeprefix("file://")
        try:
            with open(path, encoding="utf-8") as f:
                return f.read()
        except Exception as e:
            log.error(f"Failed to read local IPTV playlist {path}: {e}")
            return None
    try:
        resp = requests.get(source, timeout=10)
        resp.raise_for_status()
        return resp.text
    except Exception as e:
        log.error(f"Failed to fetch IPTV playlist from {source}: {e}")
        return None


def _parse_entries(m3u_content: str, query_lower: str, logo_re: re.Pattern) -> list[dict]:
    entries = []
    lines = m3u_content.splitlines()
    i = 0
    while i < len(lines):
        line = lines[i].strip()
        if line.startswith("#EXTINF:"):
            comma_idx = line.rfind(",")
            name = line[comma_idx + 1:].strip() if comma_idx != -1 else line[8:].strip()
            logo_match = logo_re.search(line)
            logo = logo_match.group(1) if logo_match else ""
            url_line = ""
            j = i + 1
            while j < len(lines):
                candidate = lines[j].strip()
                if not candidate:
                    j += 1
                    continue
                if candidate.startswith("#"):
                    break
                url_line = candidate
                break
            if url_line and name and query_lower in name.lower():
                entries.append({"name": name, "url": url_line, "logo": logo})
        i += 1
    return entries


def search_tv_channels(query: str, limit: int = 10) -> list[dict]:
    """
    Searches IPTV playlists for channels matching query.
    Local sources (file paths) take priority: if a local entry's URL is reachable,
    its normalized name blocks duplicates from online sources. If the local URL is
    dead, the online alternative is used instead.
    """
    raw_urls = CFG.iptv_m3u_url
    if not raw_urls:
        log.warning("IPTV M3U URL is not configured.")
        return []

    sources = [u.strip() for u in raw_urls.split(",") if u.strip()]
    logo_re = re.compile(r'tvg-logo="([^"]*)"')
    query_lower = query.strip().lower()

    # Collect candidates per source (preserve source order)
    all_candidates: list[tuple[str, dict]] = []  # (source, entry)
    for source in sources:
        log.info(f"Loading IPTV M3U from: {source}")
        content = _load_m3u(source)
        if content is None:
            continue
        is_local = source.startswith("/") or source.startswith("file://")
        for entry in _parse_entries(content, query_lower, logo_re):
            all_candidates.append((source if not is_local else "__local__", entry))

    results = []
    seen_urls: set[str] = set()
    seen_names: set[str] = set()

    # First pass: local entries that are reachable claim their normalized name
    for source, entry in all_candidates:
        if source != "__local__":
            continue
        url = entry["url"]
        if url in seen_urls:
            continue
        name_norm = _normalize_name(entry["name"])
        if _url_reachable(url):
            seen_urls.add(url)
            seen_names.add(name_norm)
            results.append(entry)
            log.info(f"Local entry accepted: {entry['name']}")
        else:
            log.info(f"Local entry unreachable, will allow online fallback: {entry['name']}")

    # Second pass: online entries fill gaps (skip if name already covered)
    for source, entry in all_candidates:
        if source == "__local__":
            continue
        if len(results) >= limit:
            break
        url = entry["url"]
        if url in seen_urls:
            continue
        name_norm = _normalize_name(entry["name"])
        if name_norm in seen_names:
            continue
        seen_urls.add(url)
        seen_names.add(name_norm)
        results.append(entry)

    log.info(f"Search for '{query}' returned {len(results)} channels.")
    return results[:limit]


def get_tv_channel_info_by_url(stream_url: str) -> dict | None:
    """
    Reverse lookup: Find IPTV channel name and logo by its stream URL.
    """
    raw_urls = CFG.iptv_m3u_url
    if not raw_urls:
        return None
        
    urls = [u.strip() for u in raw_urls.split(",") if u.strip()]
    logo_re = re.compile(r'tvg-logo="([^"]*)"')
    
    # Clean the target URL for comparison
    def clean_u(u):
        if not u:
            return ""
        return u.split('|')[0].strip().rstrip('/').replace("https://", "http://").replace("://www.", "://")
        
    target_clean = clean_u(stream_url)
    
    for url in urls:
        m3u_content = _load_m3u(url)
        if m3u_content is None:
            continue
            
        lines = m3u_content.splitlines()
        i = 0
        while i < len(lines):
            line = lines[i].strip()
            if line.startswith("#EXTINF:"):
                comma_idx = line.rfind(",")
                if comma_idx != -1:
                    name = line[comma_idx + 1:].strip()
                else:
                    name = line[8:].strip()
                    
                logo_match = logo_re.search(line)
                logo = logo_match.group(1) if logo_match else ""
                
                url_line = ""
                j = i + 1
                while j < len(lines):
                    candidate = lines[j].strip()
                    if not candidate:
                        j += 1
                        continue
                    if candidate.startswith("#"):
                        break
                    url_line = candidate
                    break
                    
                if url_line and name:
                    if clean_u(url_line) == target_clean:
                        return {
                            "name": name,
                            "logo": logo
                        }
            i += 1
    return None

