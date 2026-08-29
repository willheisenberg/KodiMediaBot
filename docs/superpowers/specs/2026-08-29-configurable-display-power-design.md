# Configurable display power control

Date: 2026-08-29
Branch: `refactor/modularize`

## Problem

The projector power buttons are hard-wired to infrared. `panel.py` renders the
literal strings `📽 Beamer On` / `📽 Beamer Off`, `ui_callbacks.py` imports the
`projector` singleton directly, and that singleton writes NEC pulses to
`/dev/lirc0`. Anyone whose screen is a TV rather than an IR projector cannot use
these buttons at all, and there is no way to change the wording, the target
device, or the mechanism without editing Python.

## Goal

The two buttons drive whatever the operator configures: an IR projector, a TV
over CEC, an HTTP endpoint, or a local script. Label, target address, and both
power commands come from the `.env` file. No code change is needed to switch
between them.

## Approach

Infrared stops being a special case and becomes one command among others.
`projector.py` gains a CLI entry point, and the shipped default for the power
commands invokes it. There is exactly one execution path in the button handler:
run the configured command line.

Rejected alternatives:

- A `POWER_MODE=ir|command` switch. Keeps two code paths alive, and every future
  change has to pick one.
- Command with implicit IR fallback. A typo in the command silently fires the
  projector instead of reporting an error.

## Components

### `kodibot/core/power.py` (new)

```python
def run_display_power(on: bool) -> bool
```

Picks `CFG.display_power_on_cmd` or `CFG.display_power_off_cmd`, runs it through
`subprocess.run(cmd, shell=True, capture_output=True, text=True,
timeout=CFG.display_command_timeout)`, returns whether the return code was zero.
On failure it logs return code and stderr; on `TimeoutExpired` it logs the
timeout and returns `False`. This mirrors `run_cec_power()` in `kodi_hifi.py`,
which is the existing pattern for shelling out.

`shell=True` is required so operators can write pipes, `&&`, and environment
variable expansion. The command string comes from the operator-controlled `.env`
and never incorporates user input from Telegram, so there is no injection path
from the bot's users.

### `kodibot/core/projector.py` (modified)

Line 20 stops hard-coding the device: `self.device_path = CFG.projector_lirc_device`.
Everything else about `ProjectorController` and the `projector` singleton stays
as it is. A `__main__` block is appended:

```python
if __name__ == "__main__":
    import sys
    action = sys.argv[1] if len(sys.argv) > 1 else ""
    if action == "on":
        ok = projector.power_on()
    elif action == "off":
        ok = projector.power_off()
    else:
        print("usage: python -m kodibot.core.projector on|off", file=sys.stderr)
        sys.exit(2)
    sys.exit(0 if ok else 1)
```

### `kodibot/config.py` (modified)

| Variable | Default | Purpose |
|---|---|---|
| `DISPLAY_BUTTON_LABEL` | `📽 Beamer` | Free-form string including emoji, e.g. `📺 TV` |
| `DISPLAY_POWER_ON_CMD` | `python -m kodibot.core.projector on` | Shell line |
| `DISPLAY_POWER_OFF_CMD` | `python -m kodibot.core.projector off` | Shell line |
| `DISPLAY_COMMAND_TIMEOUT` | `15` | Seconds before a hanging command is killed |
| `TV_HOST` | *(empty)* | Address of the TV |

`TV_HOST` needs no substitution logic in the code. Because the command runs
through a shell and inherits the process environment, the operator references it
directly:

```
TV_HOST=192.168.178.42
DISPLAY_POWER_OFF_CMD=wget -qO- --post-data='' http://$TV_HOST:8001/api/v2/power
```

It is still added to the `Config` dataclass so it appears in the configuration
overview and can be asserted in tests.

### `kodibot/telegram/panel.py` (modified)

Line 149 builds both button captions from `CFG.display_button_label` instead of
the literal, producing `f"{label} On"` and `f"{label} Off"`.

### `kodibot/telegram/ui_callbacks.py` (modified)

The `beamer:on` / `beamer:off` branches become `display:on` / `display:off`,
drop the `projector` import, and call `run_display_power(on)` inside
`asyncio.to_thread`, as the current code already does. The toast text is built
from the label rather than hard-coded.

Renaming the callback data is safe: the panel message is re-rendered on every
refresh, so no stale buttons carrying the old value survive in a way that
matters. No alias is kept.

### `scripts/` (new directory)

Operator-supplied power scripts live here. `.gitignore` gets:

```
scripts/*
!scripts/.gitkeep
```

The directory stays versioned while its contents remain private. Without the
`.gitkeep` exception a fresh clone would have no `scripts/` directory and
`COPY scripts /scripts` would fail the Docker build.

`Dockerfile` gains `COPY scripts /scripts` and a `chmod +x /scripts/*` guarded
against the empty case. `deploy_libreelec_partyqueue.sh` gains `scripts` in its
`scp -r` list, so the directory survives the `rm -rf` of the remote folder.

### Deployment surface

`docker-compose.local-bot-api.yml` passes the five new variables through to the
container. `.env.local-bot-api.example` documents them with a commented TV
example next to the IR default. The README's *Projector (Beamer) Infrared
Controls* section is retitled and gains the three configurations (IR, CEC, HTTP)
as worked examples.

Note: `.env.local-bot-api.example` is currently outside Claude's permitted
paths. Either the permission is granted before implementation, or the operator
adds those lines by hand.

## Error handling

A failing command yields `False` and a warning log containing return code and
stderr; the Telegram toast reports `⚠ {label} On failed`. A command exceeding
`DISPLAY_COMMAND_TIMEOUT` is killed and treated as a failure — the current
`run_cec_power()` has no timeout and can block its worker thread indefinitely,
which this design deliberately avoids. Missing or empty command strings are
treated as failures with an explicit log line, not as silent no-ops.

## Testing

`tests/test_power.py` (new), with `subprocess.run` patched so nothing is ever
executed:

- return code 0 → `True`
- non-zero return code → `False`, stderr present in the log
- `TimeoutExpired` → `False`
- `on=True` uses `display_power_on_cmd`, `on=False` uses `display_power_off_cmd`
- empty command string → `False`, no call to `subprocess.run`

`tests/test_telegram_panel.py` gains a case asserting the configured label
appears in the rendered button row.

Test files carry the usual `os.environ.setdefault` preamble before importing
`kodibot`, since `config.py` reads the environment at import time.

## Inert configuration variables

Four variables look like knobs but change nothing. No production code reads any
of them; the only reader is `tests/test_projector.py:31-32`, which asserts that
the config loads what the config loads. Turning a knob that does nothing is
worse than having no knob, because the failure is silent.

| Variable | Why it is inert | Resolution |
|---|---|---|
| `PROJECTOR_GPIO` | The pin belongs to the kernel overlay loaded from `/flash/config.txt` at boot; a containerised process cannot rebind it | Replaced by `PROJECTOR_LIRC_DEVICE` |
| `PROJECTOR_PROTOCOL` | `send_command()` hard-codes NEC header and bit timings | Removed |
| `PIGPIO_HOST` | Left over from the pre-LIRC architecture | Removed |
| `PIGPIO_PORT` | Same | Removed |

`README.md:257` already states that no `pigpiod` daemon is required, so the two
`PIGPIO_*` entries in `docker-compose.local-bot-api.yml:44-45` contradict the
project's own documentation.

### `PROJECTOR_LIRC_DEVICE`

Default `/dev/lirc0`, consumed at line 20 of `projector.py`. This is a real
setting: it matters as soon as a machine exposes more than one IR transmitter.
The GPIO pin number moves to the README's boot-configuration instructions, where
it actually applies.

### Protocols other than NEC

Not implemented in Python. Encoders for RC5, RC6 and Sony SIRC would be roughly
twenty lines each and untestable here, since no such device is available to
verify against — untested IR code that fails on first real use is worse than
none. Approach A already solves this: another protocol is a command.

```
DISPLAY_POWER_ON_CMD=ir-ctl -d /dev/lirc0 -S rc5:0x1234
```

`ir-ctl` ships in `v4l-utils`, which is **not** installed in the Alpine image and
is deliberately not added here. Anyone needing it adds `v4l-utils` to the
Dockerfile's `apk add` line; the README notes this next to the example.

### Files touched

`config.py`, `projector.py:20`, `docker-compose.local-bot-api.yml:44-47`,
`README.md` (lines 93, 281-282, and the boot-configuration section), and
`tests/test_projector.py:15-16,31-32`.
