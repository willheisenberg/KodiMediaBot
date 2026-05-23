import re
import requests
import logging
from kodibot.config import CFG

log = logging.getLogger(__name__)

def search_tv_channels(query: str, limit: int = 10) -> list[dict]:
    """
    Downloads M3U IPTV playlists from the comma-separated `CFG.iptv_m3u_url` string,
    parses them, and searches for channels matching the given query, deduplicating
    by stream URL.
    """
    raw_urls = CFG.iptv_m3u_url
    if not raw_urls:
        log.warning("IPTV M3U URL is not configured.")
        return []
    
    urls = [u.strip() for u in raw_urls.split(",") if u.strip()]
    results = []
    seen_urls = set()
    query_lower = query.strip().lower()
    logo_re = re.compile(r'tvg-logo="([^"]*)"')

    for url in urls:
        try:
            log.info(f"Downloading IPTV M3U list from: {url}")
            resp = requests.get(url, timeout=10)
            resp.raise_for_status()
            m3u_content = resp.text
        except Exception as e:
            log.error(f"Failed to fetch IPTV playlist from {url}: {e}")
            continue

        lines = m3u_content.splitlines()
        i = 0
        while i < len(lines):
            line = lines[i].strip()
            if line.startswith("#EXTINF:"):
                # Find the channel name after the last comma
                comma_idx = line.rfind(",")
                if comma_idx != -1:
                    name = line[comma_idx + 1:].strip()
                else:
                    name = line[8:].strip()
                
                logo_match = logo_re.search(line)
                logo = logo_match.group(1) if logo_match else ""
                
                # Find the stream URL on the subsequent non-empty, non-comment line
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
                    if query_lower in name.lower() and url_line not in seen_urls:
                        seen_urls.add(url_line)
                        results.append({
                            "name": name,
                            "url": url_line,
                            "logo": logo
                        })
                        if len(results) >= limit:
                            log.info(f"Fuzzy search for '{query}' reached limit of {limit} channels.")
                            return results
            i += 1
        
    log.info(f"Fuzzy search for '{query}' returned {len(results)} IPTV channels across all lists.")
    return results
