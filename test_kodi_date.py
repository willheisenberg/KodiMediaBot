import json
import requests
import datetime

# Replace with the actual Kodi RPC URL
# Let's import the bot's kodi_api instead to use the config properly
import sys
import os
sys.path.append("/home/tesla/githubprojects/KodiMediaBot")

from kodibot.core import kodi_api

res = kodi_api.kodi_call(
    "VideoLibrary.GetMovies",
    {"properties": ["dateadded"], "limits": {"start": 0, "end": 1}}
)
movies = res.get("result", {}).get("movies", [])
if movies:
    print(f"Dateadded: {movies[0].get('dateadded')}")
else:
    print("No movies found.")
