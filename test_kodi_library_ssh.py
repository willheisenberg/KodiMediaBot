import subprocess
import shlex
import sys

# We need to test the while loop logic exactly as the bot executes it
files = [
    "/storage/videos/stars-akira.1080p.mkv",
    "/storage/videos/anora.2024.german.dl.1080p.web.h264-wayne.mkv",
    "/storage/videos/sundry-a.world.beyond.2015.german.dl.1080p.web.h264.mkv"
]
file_list_str = "\n".join(f for f in files if f) + "\n"

# A mock for the SSH command. Instead of SSH, we just run the while read locally
# but since stat format %Z works on linux, we can just run it. 
# Or we just echo it to test the pipe.
cmd = "bash -c 'while read -r f; do echo \"$f|1234567890\"; done'"

res = subprocess.run(cmd, shell=True, input=file_list_str, text=True, capture_output=True)

ctime_map = {}
for line in res.stdout.splitlines():
    parts = line.split("|")
    if len(parts) == 2:
        try:
            ctime_map[parts[0]] = int(parts[1])
        except ValueError:
            pass

print(f"Processed {len(ctime_map)} files out of {len(files)}")
for f, ts in ctime_map.items():
    print(f" -> {f}: {ts}")
    
if len(ctime_map) != len(files):
    sys.exit(1)
