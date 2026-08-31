# Configurable Display Power Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Make the projector power buttons drive any device — IR projector, TV over CEC, an HTTP endpoint, or a local script — configured entirely from `.env`.

**Architecture:** Infrared stops being a special case. `projector.py` gains a CLI entry point, and the shipped default power commands invoke it. A new `kodibot/core/power.py` runs whatever command line the operator configured, so the button handler has exactly one execution path. Four inert configuration variables are cleaned up in the same pass.

**Tech Stack:** Python 3.12 (container) / 3.14 (local venv), python-telegram-bot, pytest, Docker Compose on LibreELEC.

**Spec:** `docs/superpowers/specs/2026-08-29-configurable-display-power-design.md`

## Global Constraints

- **Never commit or push.** Leave every change in the working tree and report status. This overrides any task-contract default. It applies to subagents executing this plan.
- Code must run on **Python 3.12** — the container is `python:3.12-alpine` while the local venv is 3.14.
- Run tests with `.venv/bin/python -m pytest -q`. The full suite must pass before a task counts as done.
- `os.environ` is read in `kodibot/config.py` and nowhere else.
- Every test file sets its own env defaults **before** importing `kodibot`; there is no `conftest.py`.
- `CFG` is a **frozen** dataclass. Tests must not `setattr` on it. Override with `monkeypatch.setattr(module, "CFG", dataclasses.replace(module.CFG, field=value))`.
- `.env.local-bot-api.example` is outside Claude's permitted paths. Task 6 states the exact lines; the operator adds them by hand unless the permission is granted first.

## File Structure

| File | Responsibility |
|---|---|
| `kodibot/core/power.py` (new) | Runs the configured power command. One function, no device knowledge. |
| `kodibot/core/projector.py` (modify) | IR transmission, unchanged; gains a CLI entry point and reads its device path from config. |
| `kodibot/config.py` (modify) | Five new variables in, four inert ones out. |
| `kodibot/telegram/panel.py` (modify) | Renders button captions from the configured label. |
| `kodibot/telegram/ui_callbacks.py` (modify) | Dispatches `display:on/off` to `run_display_power`. |
| `scripts/` (new) | Operator-supplied scripts. Versioned directory, private contents. |
| `tests/test_power.py` (new) | Command execution behaviour with `subprocess.run` patched. |

---

### Task 1: Configuration variables

**Files:**
- Modify: `kodibot/config.py:91-99` (dataclass fields), `kodibot/config.py:215-223` (factory)
- Test: `tests/test_projector.py:15-16,31-32`

**Interfaces:**
- Produces: `CFG.display_button_label: str`, `CFG.display_power_on_cmd: str`, `CFG.display_power_off_cmd: str`, `CFG.display_command_timeout: float`, `CFG.tv_host: str | None`, `CFG.projector_lirc_device: str`
- Removes: `CFG.projector_gpio`, `CFG.projector_protocol`, `CFG.pigpio_host`, `CFG.pigpio_port`

- [ ] **Step 1: Update the existing config test to the new fields**

In `tests/test_projector.py`, replace lines 15-16:

```python
os.environ.setdefault("PROJECTOR_LIRC_DEVICE", "/dev/lirc0")
```

and replace lines 31-32:

```python
    assert CFG.projector_lirc_device == "/dev/lirc0"
```

Then append this test to the same file:

```python
def test_display_power_config_defaults():
    """Display power settings fall back to the shipped IR commands."""
    from kodibot.config import CFG

    assert CFG.display_button_label == "📽 Beamer"
    assert CFG.display_power_on_cmd == "python -m kodibot.core.projector on"
    assert CFG.display_power_off_cmd == "python -m kodibot.core.projector off"
    assert CFG.display_command_timeout == 15.0
```

- [ ] **Step 2: Run the test to verify it fails**

Run: `.venv/bin/python -m pytest tests/test_projector.py -q`
Expected: FAIL — `AttributeError: 'Config' object has no attribute 'projector_lirc_device'`

- [ ] **Step 3: Replace the projector block in the dataclass**

In `kodibot/config.py`, replace lines 91-99:

```python
    # ── Projector (Beamer) ────────────────────────────────────────────
    projector_lirc_device: str
    projector_address: int
    projector_power_on_code: int
    projector_power_off_code: int
    projector_power_on_repeats: int

    # ── Display power (projector or TV) ───────────────────────────────
    display_button_label: str
    display_power_on_cmd: str
    display_power_off_cmd: str
    display_command_timeout: float
    tv_host: str | None
```

Note that `pigpio_host` and `pigpio_port` sit in this same block and are deleted along with `projector_gpio` and `projector_protocol`.

- [ ] **Step 4: Replace the corresponding factory block**

In `kodibot/config.py`, replace lines 215-223:

```python
            # Projector (Beamer)
            projector_lirc_device=os.environ.get("PROJECTOR_LIRC_DEVICE", "/dev/lirc0"),
            projector_address=int(os.environ.get("PROJECTOR_ADDRESS", "0x08"), 16),
            projector_power_on_code=int(os.environ.get("PROJECTOR_POWER_ON_CODE", "0x03"), 16),
            projector_power_off_code=int(os.environ.get("PROJECTOR_POWER_OFF_CODE", "0x00"), 16),
            projector_power_on_repeats=int(os.environ.get("PROJECTOR_POWER_ON_REPEATS", "4")),
            # Display power (projector or TV)
            display_button_label=(os.environ.get("DISPLAY_BUTTON_LABEL") or "📽 Beamer").strip(),
            display_power_on_cmd=(
                os.environ.get("DISPLAY_POWER_ON_CMD")
                or "python -m kodibot.core.projector on"
            ),
            display_power_off_cmd=(
                os.environ.get("DISPLAY_POWER_OFF_CMD")
                or "python -m kodibot.core.projector off"
            ),
            display_command_timeout=float(os.environ.get("DISPLAY_COMMAND_TIMEOUT", "15")),
            tv_host=os.environ.get("TV_HOST") or None,
```

Delete the `pigpio_host=` and `pigpio_port=` lines from the same block.

- [ ] **Step 5: Run the tests to verify they pass**

Run: `.venv/bin/python -m pytest tests/test_projector.py -q`
Expected: PASS

Run: `.venv/bin/python -m pytest -q`
Expected: PASS — the whole suite, to catch any other reader of the removed fields.

---

### Task 2: The power command runner

**Files:**
- Create: `kodibot/core/power.py`
- Test: `tests/test_power.py` (new)

**Interfaces:**
- Consumes: `CFG.display_power_on_cmd`, `CFG.display_power_off_cmd`, `CFG.display_command_timeout` from Task 1
- Produces: `run_display_power(on: bool) -> bool`

- [ ] **Step 1: Write the failing tests**

Create `tests/test_power.py`:

```python
"""Tests for the configurable display power command runner."""

import dataclasses
import os
import subprocess
import sys
from unittest.mock import patch

os.environ.setdefault("KODI_HOST", "127.0.0.1")
os.environ.setdefault("KODI_PORT", "8080")
os.environ.setdefault("KODI_WS_PORT", "9090")
os.environ.setdefault("KODI_USER", "kodi")
os.environ.setdefault("KODI_PASS", "kodi")
os.environ.setdefault("TG_TOKEN", "test:token")

sys.path.insert(0, os.path.dirname(os.path.dirname(__file__)))

from kodibot.core import power


def _with_commands(monkeypatch, on_cmd="cmd-on", off_cmd="cmd-off", timeout=15.0):
    """Replaces the frozen CFG on the power module with a tweaked copy."""
    monkeypatch.setattr(
        power,
        "CFG",
        dataclasses.replace(
            power.CFG,
            display_power_on_cmd=on_cmd,
            display_power_off_cmd=off_cmd,
            display_command_timeout=timeout,
        ),
    )


def _result(returncode, stderr=""):
    return subprocess.CompletedProcess(args="cmd", returncode=returncode, stdout="", stderr=stderr)


class TestRunDisplayPower:
    def test_returns_true_on_exit_zero(self, monkeypatch):
        _with_commands(monkeypatch)
        with patch("subprocess.run", return_value=_result(0)) as run:
            assert power.run_display_power(True) is True
        assert run.call_count == 1

    def test_returns_false_on_nonzero_exit(self, monkeypatch):
        _with_commands(monkeypatch)
        with patch("subprocess.run", return_value=_result(1, "boom")):
            assert power.run_display_power(True) is False

    def test_returns_false_on_timeout(self, monkeypatch):
        _with_commands(monkeypatch)
        with patch("subprocess.run", side_effect=subprocess.TimeoutExpired("cmd", 15)):
            assert power.run_display_power(True) is False

    def test_on_uses_the_on_command(self, monkeypatch):
        _with_commands(monkeypatch, on_cmd="turn-it-on")
        with patch("subprocess.run", return_value=_result(0)) as run:
            power.run_display_power(True)
        assert run.call_args.args[0] == "turn-it-on"

    def test_off_uses_the_off_command(self, monkeypatch):
        _with_commands(monkeypatch, off_cmd="turn-it-off")
        with patch("subprocess.run", return_value=_result(0)) as run:
            power.run_display_power(False)
        assert run.call_args.args[0] == "turn-it-off"

    def test_passes_the_configured_timeout(self, monkeypatch):
        _with_commands(monkeypatch, timeout=42.0)
        with patch("subprocess.run", return_value=_result(0)) as run:
            power.run_display_power(True)
        assert run.call_args.kwargs["timeout"] == 42.0

    def test_empty_command_is_a_failure_and_runs_nothing(self, monkeypatch):
        _with_commands(monkeypatch, on_cmd="   ")
        with patch("subprocess.run") as run:
            assert power.run_display_power(True) is False
        assert run.call_count == 0
```

- [ ] **Step 2: Run the tests to verify they fail**

Run: `.venv/bin/python -m pytest tests/test_power.py -q`
Expected: FAIL — `ModuleNotFoundError: No module named 'kodibot.core.power'`

- [ ] **Step 3: Write the implementation**

Create `kodibot/core/power.py`:

```python
"""Runs the configured display power commands.

The projector/TV power buttons execute an operator-supplied shell command from
the environment, so infrared, CEC, HTTP requests and local scripts are all the
same thing from the bot's point of view.

``shell=True`` is required so operators can write pipes, ``&&`` and environment
variable expansion such as ``$TV_HOST``.  The command string comes from the
operator-controlled ``.env`` and never incorporates input from Telegram users,
so there is no injection path from the bot's users.  This mirrors
``run_cec_power()`` in ``kodi_hifi.py``.
"""

import logging
import subprocess

from kodibot.config import CFG

log = logging.getLogger(__name__)


def run_display_power(on: bool) -> bool:
    """Runs the configured power-on or power-off command.

    Returns True when the command exits zero.  Returns False on a non-zero
    exit, a timeout, an empty command, or an unexpected error.
    """
    action = "on" if on else "off"
    cmd = CFG.display_power_on_cmd if on else CFG.display_power_off_cmd
    if not cmd.strip():
        log.warning("No display power %s command configured", action)
        return False
    try:
        res = subprocess.run(
            cmd,
            shell=True,
            capture_output=True,
            text=True,
            timeout=CFG.display_command_timeout,
        )
    except subprocess.TimeoutExpired:
        log.warning(
            "Display power %s command timed out after %ss: %s",
            action,
            CFG.display_command_timeout,
            cmd,
        )
        return False
    except Exception as e:
        log.warning("Display power %s command error: %s", action, e)
        return False
    if res.returncode != 0:
        log.warning(
            "Display power %s command failed: rc=%d stderr=%s",
            action,
            res.returncode,
            (res.stderr or "").strip(),
        )
        return False
    return True
```

- [ ] **Step 4: Run the tests to verify they pass**

Run: `.venv/bin/python -m pytest tests/test_power.py -q`
Expected: PASS, 7 tests

---

### Task 3: Infrared as a command

**Files:**
- Modify: `kodibot/core/projector.py:20` (device path), end of file (CLI entry)
- Test: `tests/test_projector.py`

**Interfaces:**
- Consumes: `CFG.projector_lirc_device` from Task 1
- Produces: `python -m kodibot.core.projector on|off`, exit code 0 on success, 1 on failure, 2 on bad usage

- [ ] **Step 1: Write the failing test**

Append to `tests/test_projector.py`:

```python
def test_projector_uses_configured_lirc_device(monkeypatch):
    """The device path comes from configuration, not a hard-coded literal."""
    import dataclasses

    from kodibot.config import CFG

    monkeypatch.setattr(
        PJ, "CFG", dataclasses.replace(CFG, projector_lirc_device="/dev/lirc9")
    )
    controller = PJ.ProjectorController()

    assert controller.device_path == "/dev/lirc9"
```

- [ ] **Step 2: Run the test to verify it fails**

Run: `.venv/bin/python -m pytest tests/test_projector.py::test_projector_uses_configured_lirc_device -q`
Expected: FAIL — `assert '/dev/lirc0' == '/dev/lirc9'`

- [ ] **Step 3: Read the device path from config**

In `kodibot/core/projector.py`, replace line 20:

```python
        self.device_path = CFG.projector_lirc_device
```

`CFG` is already imported at the top of the file.

- [ ] **Step 4: Run the test to verify it passes**

Run: `.venv/bin/python -m pytest tests/test_projector.py -q`
Expected: PASS

- [ ] **Step 5: Add the CLI entry point**

Append to the end of `kodibot/core/projector.py`, after the `projector = ProjectorController()` line:

```python


def main(argv: list[str]) -> int:
    """Entry point so IR can be driven as a shell command.

    This is what the default DISPLAY_POWER_ON_CMD / DISPLAY_POWER_OFF_CMD
    invoke, which keeps infrared on the same footing as CEC, HTTP or scripts.
    """
    action = argv[1] if len(argv) > 1 else ""
    if action == "on":
        return 0 if projector.power_on() else 1
    if action == "off":
        return 0 if projector.power_off() else 1
    print("usage: python -m kodibot.core.projector on|off", file=sys.stderr)
    return 2


if __name__ == "__main__":
    logging.basicConfig(level=logging.INFO, format="%(levelname)s %(name)s: %(message)s")
    sys.exit(main(sys.argv))
```

Add `import sys` to the imports at the top of the file (`logging`, `os`, `struct` and `time` are already there).

- [ ] **Step 6: Write tests for the entry point**

Append to `tests/test_projector.py`:

```python
def test_main_on_returns_zero_on_success(monkeypatch):
    monkeypatch.setattr(PJ.projector, "power_on", lambda: True)
    assert PJ.main(["projector", "on"]) == 0


def test_main_on_returns_one_on_failure(monkeypatch):
    monkeypatch.setattr(PJ.projector, "power_on", lambda: False)
    assert PJ.main(["projector", "on"]) == 1


def test_main_off_calls_power_off(monkeypatch):
    calls = []
    monkeypatch.setattr(PJ.projector, "power_off", lambda: calls.append("off") or True)
    assert PJ.main(["projector", "off"]) == 0
    assert calls == ["off"]


def test_main_rejects_unknown_action():
    assert PJ.main(["projector", "sideways"]) == 2
    assert PJ.main(["projector"]) == 2
```

- [ ] **Step 7: Run the tests to verify they pass**

Run: `.venv/bin/python -m pytest tests/test_projector.py -q`
Expected: PASS

---

### Task 4: Button label and handler

**Files:**
- Modify: `kodibot/telegram/panel.py:148-151`
- Modify: `kodibot/telegram/ui_callbacks.py:597-606`
- Test: `tests/test_telegram_panel.py`

**Interfaces:**
- Consumes: `CFG.display_button_label` from Task 1, `run_display_power` from Task 2
- Produces: callback data `display:on` and `display:off`, replacing `beamer:on` / `beamer:off`

- [ ] **Step 1: Write the failing test**

Append this class to `tests/test_telegram_panel.py`:

```python
class TestDisplayPowerButtons:
    def test_buttons_use_configured_label_and_callbacks(self, monkeypatch):
        import dataclasses

        monkeypatch.setattr(panel.ha, "ha_available", lambda: False)
        monkeypatch.setattr(
            panel, "CFG", dataclasses.replace(panel.CFG, display_button_label="📺 TV")
        )

        markup = panel.control_panel(mode="main")
        row = markup.inline_keyboard[6]

        assert row[0].text == "📺 TV On"
        assert row[0].callback_data == "display:on"
        assert row[1].text == "📺 TV Off"
        assert row[1].callback_data == "display:off"
```

- [ ] **Step 2: Run the test to verify it fails**

Run: `.venv/bin/python -m pytest tests/test_telegram_panel.py::TestDisplayPowerButtons -q`
Expected: FAIL — `assert '📽 Beamer On' == '📺 TV On'`

- [ ] **Step 3: Render the buttons from config**

In `kodibot/telegram/panel.py`, replace lines 148-151:

```python
        [
            InlineKeyboardButton(f"{CFG.display_button_label} On", callback_data="display:on"),
            InlineKeyboardButton(f"{CFG.display_button_label} Off", callback_data="display:off"),
        ],
```

`CFG` is already imported at the top of the file.

- [ ] **Step 4: Run the test to verify it passes**

Run: `.venv/bin/python -m pytest tests/test_telegram_panel.py -q`
Expected: PASS

- [ ] **Step 5: Update the callback handler**

In `kodibot/telegram/ui_callbacks.py`, replace lines 597-606:

```python
    elif cmd == "display:on":
        from kodibot.core.power import run_display_power
        label = UI.CFG.display_button_label
        ok = await asyncio.to_thread(run_display_power, True)
        await q.answer(text=f"{label} On" if ok else f"⚠ {label} On failed")
        sent = True
    elif cmd == "display:off":
        from kodibot.core.power import run_display_power
        label = UI.CFG.display_button_label
        ok = await asyncio.to_thread(run_display_power, False)
        await q.answer(text=f"{label} Off" if ok else f"⚠ {label} Off failed")
        sent = True
```

The local import mirrors the pattern the removed `projector` import used, keeping `ui_callbacks` free of an import-time dependency on the power module.

- [ ] **Step 6: Verify no reference to the old callback names survives**

Run: `grep -rn 'beamer' kodibot/ tests/`
Expected: no output.

- [ ] **Step 7: Run the full suite**

Run: `.venv/bin/python -m pytest -q`
Expected: PASS

---

### Task 5: Script directory and deployment

**Files:**
- Create: `scripts/.gitkeep`
- Modify: `.gitignore`
- Modify: `Dockerfile`
- Modify: `deploy_libreelec_partyqueue.sh:47-55`

**Interfaces:**
- Produces: `/scripts` inside the container, populated from the project's `scripts/` directory

- [ ] **Step 1: Create the directory**

```bash
mkdir -p scripts && touch scripts/.gitkeep
```

- [ ] **Step 2: Ignore the contents but keep the directory**

Append to `.gitignore`:

```
scripts/*
!scripts/.gitkeep
```

Without the exception a fresh clone would have no `scripts/` directory and the Docker build in Step 3 would fail.

- [ ] **Step 3: Copy the directory into the image**

In `Dockerfile`, after the `COPY kodibot /kodibot` line:

```dockerfile
COPY scripts /scripts
RUN find /scripts -type f -exec chmod +x {} +
```

`find -exec` is used rather than `chmod +x /scripts/*` because the latter fails when the directory holds only `.gitkeep`.

- [ ] **Step 4: Verify the directory reaches the build context**

A local `docker build` is not a reliable check here — the target runs on ARM
while the development machine is x86, and the build pulls packages from the
network. Verify the two things that can actually break instead:

Run: `grep -n 'scripts' .dockerignore`
Expected: no output — nothing excludes the directory from the build context.

Run: `ls -a scripts/`
Expected: `.gitkeep` is present, so the directory exists after a fresh clone.

Run: `git status --short scripts/`
Expected: `.gitkeep` shows as a new file; other files placed there show nothing.

- [ ] **Step 5: Ship the directory on deploy**

In `deploy_libreelec_partyqueue.sh`, add `scripts` to the `scp -r` list so it survives the `rm -rf` of the remote folder. The list becomes:

```bash
scp -r \
  "${LOCAL_ROOT}/.dockerignore" \
  "${LOCAL_ROOT}/Caddyfile" \
  "${LOCAL_ROOT}/Dockerfile" \
  "${LOCAL_ROOT}/README.md" \
  "${LOCAL_ROOT}/main.py" \
  "${LOCAL_ROOT}/kodibot" \
  "${LOCAL_ROOT}/scripts" \
  "${SSH_TARGET}:${REMOTE_DIR}/"
```

- [ ] **Step 6: Check the script parses**

Run: `bash -n deploy_libreelec_partyqueue.sh`
Expected: no output, exit code 0.

---

### Task 6: Deployment surface and documentation

**Files:**
- Modify: `docker-compose.local-bot-api.yml:44-47`
- Modify: `README.md:93, 253-286`
- Modify: `.env.local-bot-api.example` (operator, see Global Constraints)

**Interfaces:**
- Consumes: every variable defined in Task 1

- [ ] **Step 1: Update the compose environment**

In `docker-compose.local-bot-api.yml`, replace lines 44-47 (`PIGPIO_HOST`, `PIGPIO_PORT`, `PROJECTOR_GPIO`, `PROJECTOR_PROTOCOL`) with:

```yaml
      PROJECTOR_LIRC_DEVICE: "${PROJECTOR_LIRC_DEVICE:-/dev/lirc0}"
      DISPLAY_BUTTON_LABEL: "${DISPLAY_BUTTON_LABEL}"
      DISPLAY_POWER_ON_CMD: "${DISPLAY_POWER_ON_CMD}"
      DISPLAY_POWER_OFF_CMD: "${DISPLAY_POWER_OFF_CMD}"
      DISPLAY_COMMAND_TIMEOUT: "${DISPLAY_COMMAND_TIMEOUT:-15}"
      TV_HOST: "${TV_HOST}"
```

Leaving `DISPLAY_BUTTON_LABEL` and the two command variables without a compose-level default is deliberate: an unset variable arrives as an empty string, and `config.py` falls back to the shipped IR defaults.

Deliberately **no** volume mount for `/scripts`. A bind mount would shadow the
copy baked in by Task 5, and Docker silently creates an empty directory when the
host path is missing — which would leave `/scripts` empty and every script
command failing. Scripts reach the container through the image only: put the
file in the project's `scripts/`, deploy, done.

- [ ] **Step 2: Verify the compose file is valid YAML**

Run: `.venv/bin/python -c "import yaml,sys; yaml.safe_load(open('docker-compose.local-bot-api.yml')); print('ok')"`
Expected: `ok`

If PyYAML is missing, install it into the venv first: `.venv/bin/pip install pyyaml`

- [ ] **Step 3: Update the README compose example**

In `README.md`, replace line 93 (`PROJECTOR_GPIO: "17"`) with:

```
      PROJECTOR_LIRC_DEVICE: "/dev/lirc0"
```

- [ ] **Step 4: Rewrite the projector section**

In `README.md`, retitle the section at line 253 from *Projector (Beamer) Infrared Controls* to *Display power (projector or TV)* and replace the variable block at lines 280-286 with:

````
### Configuration

The two power buttons run a shell command you configure. Infrared, CEC, an HTTP
request and a local script are all handled the same way.

```
DISPLAY_BUTTON_LABEL=📽 Beamer
DISPLAY_POWER_ON_CMD=python -m kodibot.core.projector on
DISPLAY_POWER_OFF_CMD=python -m kodibot.core.projector off
DISPLAY_COMMAND_TIMEOUT=15
TV_HOST=
```

The defaults above drive the built-in infrared transmitter, which needs:

```
PROJECTOR_LIRC_DEVICE=/dev/lirc0
PROJECTOR_ADDRESS=0x08
PROJECTOR_POWER_ON_CODE=0x03
PROJECTOR_POWER_OFF_CODE=0x00
PROJECTOR_POWER_ON_REPEATS=4
```

The GPIO pin is not configured here — it belongs to the kernel overlay
`dtoverlay=gpio-ir-tx,gpio_pin=17` in `/flash/config.txt`.

### Driving a TV instead

`TV_HOST` is available to the command as `$TV_HOST`, since commands run through
a shell and inherit the environment.

```
DISPLAY_BUTTON_LABEL=📺 TV
TV_HOST=192.168.178.42
DISPLAY_POWER_ON_CMD=wget -qO- --post-data='' http://$TV_HOST:8001/api/v2/power
DISPLAY_POWER_OFF_CMD=ssh -o StrictHostKeyChecking=no root@$CEC_HOST cec-ctl --standby -t0
```

Or point at a script. Put it in the project's `scripts/` directory — its
contents are gitignored, and it is both baked into the image and mounted at
`/scripts`:

```
DISPLAY_POWER_ON_CMD=/scripts/tv_on.sh
```

For IR protocols other than NEC, use `ir-ctl` rather than the built-in
transmitter. It ships in `v4l-utils`, which is not installed in the image — add
it to the `apk add` line in the `Dockerfile` first:

```
DISPLAY_POWER_ON_CMD=ir-ctl -d /dev/lirc0 -S rc5:0x1234
```
````

- [ ] **Step 5: Note the lines for the env example**

`.env.local-bot-api.example` cannot be edited by Claude. Report these lines to the operator for manual insertion, replacing the existing `PROJECTOR_GPIO`, `PROJECTOR_PROTOCOL`, `PIGPIO_HOST` and `PIGPIO_PORT` entries:

```
# Display power (projector or TV)
DISPLAY_BUTTON_LABEL=📽 Beamer
DISPLAY_POWER_ON_CMD=python -m kodibot.core.projector on
DISPLAY_POWER_OFF_CMD=python -m kodibot.core.projector off
DISPLAY_COMMAND_TIMEOUT=15
TV_HOST=
PROJECTOR_LIRC_DEVICE=/dev/lirc0
```

- [ ] **Step 6: Final verification**

Run: `.venv/bin/python -m pytest -q`
Expected: PASS, all tests

Run: `.venv/bin/ruff check .`
Expected: no new findings in `kodibot/core/power.py`, `kodibot/core/projector.py`, `kodibot/telegram/panel.py`, `kodibot/telegram/ui_callbacks.py` or `tests/test_power.py`

Run: `grep -rn 'projector_gpio\|projector_protocol\|pigpio\|PIGPIO' kodibot/ tests/ docker-compose.local-bot-api.yml README.md`
Expected: only the README prose at former line 257 stating that no `pigpiod` daemon is required.

- [ ] **Step 7: Report status**

Do not commit. Summarise which files changed and hand the deploy decision to the operator, who runs `./deploy_libreelec_partyqueue.sh` themselves.
