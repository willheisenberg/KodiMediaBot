import logging
import requests
import socket
import json
import os
from kodibot.config import CFG

log = logging.getLogger(__name__)

def get_api_url():
    """
    Returns the configured API URL. In a more advanced version, 
    this could perform DNS lookup on all.api.radio-browser.info.
    """
    return CFG.radio_api_url

def search_stations(query, limit=10):
    """
    Search for radio stations by name or tag.
    """
    url = f"{get_api_url()}/stations/search"
    params = {
        'name': query,
        'hidebroken': 'true',
        'limit': limit,
        'order': 'clickcount',
        'reverse': 'true'
    }
    headers = {'User-Agent': 'KodiMediaBot/1.0'}
    
    try:
        resp = requests.get(url, params=params, headers=headers, timeout=10)
        resp.raise_for_status()
        return resp.json()
    except Exception as e:
        log.error(f"Radio search failed: {e}")
        return []

def get_station_info_by_url(stream_url):
    """
    Reverse lookup: Find station info by its stream URL.
    """
    url = f"{get_api_url()}/stations/byurl"
    params = {'url': stream_url}
    headers = {'User-Agent': 'KodiMediaBot/1.0'}
    try:
        resp = requests.get(url, params=params, headers=headers, timeout=5)
        resp.raise_for_status()
        data = resp.json()
        return data[0] if data else None
    except Exception:
        return None

def report_click(station_uuid):
    """
    Report a click/play to the API to help with popularity rankings.
    """
    url = f"{get_api_url()}/url/{station_uuid}"
    try:
        requests.get(url, timeout=5)
    except Exception as e:
        log.warning(f"Could not report click for {station_uuid}: {e}")

def append_to_m3u(name, url, logo_url=None):
    """
    Appends a station to the kodi.m3u file if it doesn't already exist.
    """
    path = CFG.radio_m3u_path
    if not os.path.exists(path):
        # Create file with header if it doesn't exist
        with open(path, "w", encoding="utf-8") as f:
            f.write("#EXTM3U\n")
    
    # Check if URL already exists to avoid duplicates
    try:
        with open(path, "r", encoding="utf-8", errors="ignore") as f:
            content = f.read()
            if url in content:
                log.info(f"Station {name} already in M3U.")
                return False
    except Exception as e:
        log.error(f"Error reading M3U: {e}")

    # Prepare entry
    logo_part = f' tvg-logo="{logo_url}"' if logo_url else ""
    entry = f'#EXTINF:-1 group-title="Radio Browser"{logo_part},{name}\n{url}\n'
    
    try:
        with open(path, "a", encoding="utf-8") as f:
            f.write(entry)
        log.info(f"Added {name} to {path}")
        return True
    except Exception as e:
        log.error(f"Could not write to M3U: {e}")
        return False


def list_m3u_stations():
    """
    Returns a list of stations from the kodi.m3u file.
    Each station is a dict with 'name', 'url', and 'logo'.
    """
    path = CFG.radio_m3u_path
    if not os.path.exists(path):
        return []
    
    stations = []
    try:
        with open(path, "r", encoding="utf-8", errors="ignore") as f:
            lines = f.readlines()
            
        current_name = ""
        current_logo = ""
        for line in lines:
            line = line.strip()
            if line.startswith("#EXTINF:"):
                # Simple parsing for name and logo
                if 'tvg-logo="' in line:
                    current_logo = line.split('tvg-logo="')[1].split('"')[0]
                if "," in line:
                    current_name = line.split(",")[-1]
            elif line and not line.startswith("#"):
                stations.append({
                    "name": current_name or "Unbekannt",
                    "url": line,
                    "logo": current_logo
                })
                current_name = ""
                current_logo = ""
    except Exception as e:
        log.error(f"Error reading M3U for listing: {e}")
    
    return stations


def remove_from_m3u(index):
    """
    Removes the station at the given index from kodi.m3u.
    """
    path = CFG.radio_m3u_path
    stations = list_m3u_stations()
    if not (0 <= index < len(stations)):
        return False
    
    stations.pop(index)
    
    try:
        with open(path, "w", encoding="utf-8") as f:
            f.write("#EXTM3U\n")
            for s in stations:
                logo_part = f' tvg-logo="{s["logo"]}"' if s.get("logo") else ""
                f.write(f'#EXTINF:-1 group-title="Radio Browser"{logo_part},{s["name"]}\n{s["url"]}\n')
        return True
    except Exception as e:
        log.error(f"Error writing M3U after removal: {e}")
        return False
