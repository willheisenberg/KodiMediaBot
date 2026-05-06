import re
import shlex
import subprocess
import time

import requests

from kodibot.core import kodi_api as KA


def run_cec_volume(times: int, cmd_hex: str) -> bool:
    host = shlex.quote(KA.CFG.cec_host)
    q_times = shlex.quote(str(times))
    q_cmd = shlex.quote(f"ui-cmd={cmd_hex}")
    ssh = f"ssh -o StrictHostKeyChecking=no -o UserKnownHostsFile=/dev/null root@{host}"
    cmd = (
        f"{ssh} seq {q_times} | "
        f"{ssh} xargs -Iz cec-ctl --user-control-pressed {q_cmd} -t5"
    )
    try:
        res = subprocess.run(cmd, shell=True, check=False, capture_output=True, text=True)
        if res.returncode != 0:
            KA.log.warning(f"CEC command failed: rc={res.returncode} stderr={res.stderr.strip()}")
            return False
        return True
    except Exception as e:
        KA.log.warning(f"CEC error: err={e}")
        return False


def run_denon_volume_delta(points: int) -> bool:
    if not KA.CFG.denon_host:
        return False
    if points == 0:
        return True
    cmd = "MVUP" if points > 0 else "MVDOWN"
    steps = abs(points) * KA.CFG.denon_volume_step_commands
    url = f"http://{KA.CFG.denon_host}/goform/formiPhoneAppDirect.xml?{cmd}"
    try:
        for _ in range(steps):
            res = requests.get(url, timeout=4)
            if res.status_code != 200:
                KA.log.warning(f"Denon volume failed: status={res.status_code} host={KA.CFG.denon_host} points={points} cmd={cmd}")
                return False
            time.sleep(0.05)
        return True
    except Exception as e:
        KA.log.warning(f"Denon volume error: host={KA.CFG.denon_host} points={points} err={e}")
        return False


def run_volume_delta(points: int) -> bool:
    if KA.CFG.denon_host:
        return run_denon_volume_delta(points)
    if points == 0:
        return True
    cmd_hex = KA.CEC_CMD_VOL_UP if points > 0 else KA.CEC_CMD_VOL_DOWN
    times = abs(points) * 2
    return run_cec_volume(times, cmd_hex)


def run_denon_power(on: bool) -> bool:
    if not KA.CFG.denon_host:
        return False
    action = "PowerOn" if on else "PowerStandby"
    url = f"http://{KA.CFG.denon_host}/goform/formiPhoneAppPower.xml?1+{action}"
    try:
        res = requests.get(url, timeout=4)
        if res.status_code != 200:
            KA.log.warning(f"Denon power failed: status={res.status_code} host={KA.CFG.denon_host} action={action}")
            return False
        return True
    except Exception as e:
        KA.log.warning(f"Denon power error: host={KA.CFG.denon_host} action={action} err={e}")
        return False


def run_cec_power(on: bool) -> bool:
    if KA.CFG.denon_host:
        if run_denon_power(on):
            return True
        KA.log.info("Denon power command failed, falling back to CEC")
    host = shlex.quote(KA.CFG.cec_host)
    ssh = f"ssh -o StrictHostKeyChecking=no -o UserKnownHostsFile=/dev/null root@{host}"
    if on:
        cmd = (
            f"{ssh} cec-ctl --user-control-pressed ui-cmd=power-on-function -t0 && "
            f"{ssh} cec-ctl --user-control-pressed ui-cmd=power-on-function -t5"
        )
    else:
        cmd = (
            f"{ssh} cec-ctl --standby -t0 && "
            f"{ssh} cec-ctl --standby -t5"
        )
    try:
        res = subprocess.run(cmd, shell=True, check=False, capture_output=True, text=True)
        if res.returncode != 0:
            KA.log.warning("CEC command failed: rc=%d stderr=%s", res.returncode, res.stderr.strip())
            return False
        return True
    except Exception as e:
        KA.log.warning("CEC error: %s", e)
        return False


def run_airplay_kill() -> bool:
    host = shlex.quote(KA.CFG.cec_host)
    ssh = f"ssh -o StrictHostKeyChecking=no -o UserKnownHostsFile=/dev/null root@{host}"
    cmd = f"{ssh} cec-ctl --active-source phys-addr=1.5.0.0 -t0"
    try:
        res = subprocess.run(cmd, shell=True, check=False, capture_output=True, text=True)
        if res.returncode != 0:
            KA.log.warning("CEC command failed: rc=%d stderr=%s", res.returncode, res.stderr.strip())
            return False
        return True
    except Exception as e:
        KA.log.warning("CEC error: %s", e)
        return False


def get_hifi_power_status():
    if KA.CFG.denon_host:
        url = f"http://{KA.CFG.denon_host}/goform/formMainZone_MainZoneXml.xml"
        try:
            res = requests.get(url, timeout=4)
            if res.status_code != 200:
                KA.log.warning(f"Denon power status failed: status={res.status_code} host={KA.CFG.denon_host}")
                return None
            text = res.text or ""
            m = re.search(r"<Power>\s*<value>\s*(ON|OFF|STANDBY)\s*</value>\s*</Power>", text, flags=re.IGNORECASE)
            if not m:
                return None
            state = m.group(1).upper()
            if state == "ON":
                return "On"
            if state in ("OFF", "STANDBY"):
                return "Standby"
            return None
        except Exception as e:
            KA.log.warning(f"Denon power status error: host={KA.CFG.denon_host} err={e}")
            return None

    host = shlex.quote(KA.CFG.cec_host)
    ssh = f"ssh -o StrictHostKeyChecking=no -o UserKnownHostsFile=/dev/null root@{host}"
    cmd = f"{ssh} cec-ctl --show-topology | awk '/Audio System/ {{f=1}} f && /Power Status/ {{print $NF; exit}}'"
    try:
        res = subprocess.run(cmd, shell=True, check=False, capture_output=True, text=True)
        if res.returncode != 0:
            KA.log.warning(f"CEC command failed: rc={res.returncode} stderr={res.stderr.strip()}")
            return None
        val = (res.stdout or "").strip()
        if val in ("On", "Standby"):
            return val
        return None
    except Exception as e:
        KA.log.warning(f"CEC error: err={e}")
        return None


def get_airplay_status():
    if not KA.CFG.denon_host:
        return None
    url = f"http://{KA.CFG.denon_host}/goform/formNetAudio_StatusXml.xml"
    try:
        res = requests.get(url, timeout=4)
        if res.status_code != 200:
            KA.log.warning(f"AirPlay failed: status={res.status_code} host={KA.CFG.denon_host}")
            return None
        text = res.text or ""
        m = re.search(r"<szLine>(.*?)</szLine>", text, flags=re.DOTALL | re.IGNORECASE)
        if not m:
            return None
        values = re.findall(r"<value>(.*?)</value>", m.group(1), flags=re.DOTALL | re.IGNORECASE)
        line1 = values[0].strip() if len(values) >= 1 else ""
        line2 = values[1].strip() if len(values) >= 2 else ""
        if line1 == "Now Playing" and line2 == "AirPlay":
            return "On"
        return "Off"
    except Exception as e:
        KA.log.warning(f"AirPlay error: host={KA.CFG.denon_host} err={e}")
        return None


def get_denon_mainzone_volume():
    if not KA.CFG.denon_host:
        return None
    url = f"http://{KA.CFG.denon_host}/goform/formMainZone_MainZoneXml.xml"
    try:
        res = requests.get(url, timeout=4)
        if res.status_code != 200:
            KA.log.warning(f"Denon volume failed: status={res.status_code} host={KA.CFG.denon_host}")
            return None
        text = res.text or ""
        m = re.search(
            r"<MasterVolume>\s*<value>\s*([+-]?\d+(?:\.\d+)?)\s*</value>",
            text,
            flags=re.IGNORECASE | re.DOTALL,
        )
        if m:
            return m.group(1)
        values = re.findall(r"<value>\s*([+-]?\d+(?:\.\d+)?)\s*</value>", text, flags=re.IGNORECASE)
        if not values:
            return None
        for val in values:
            if val.startswith("-"):
                return val
        return values[0]
    except Exception as e:
        KA.log.warning(f"Denon volume error: host={KA.CFG.denon_host} err={e}")
        return None
