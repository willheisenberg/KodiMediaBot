import sys
import subprocess
import shlex

host = "172.17.0.1" # Default docker bridge to host
ssh = f"ssh -o StrictHostKeyChecking=no -o UserKnownHostsFile=/dev/null root@{host}"

files = [
    "/storage/videos/sundry-a.world.beyond.2015.german.dl.1080p.web.h264.mkv"
]
file_list_str = "\n".join(files) + "\n"

cmd = f"{ssh} 'while read -r f; do echo \"DEBUG: Checking \\$f\" >&2; stat -c \"%n|%Z\" \"$f\" 2>&1; done'"

res = subprocess.run(cmd, shell=True, input=file_list_str, text=True, capture_output=True)

print(f"RC: {res.returncode}")
print(f"STDOUT:\n{res.stdout}")
print(f"STDERR:\n{res.stderr}")
