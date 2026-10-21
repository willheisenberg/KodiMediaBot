"""Tests for deploy/sync_compose_env.awk, which the deploy script runs on the host."""

import os
import shutil
import subprocess

import pytest

ROOT = os.path.dirname(os.path.dirname(__file__))
SCRIPT = os.path.join(ROOT, "deploy", "sync_compose_env.awk")

pytestmark = pytest.mark.skipif(shutil.which("awk") is None, reason="awk not installed")

REPO = """\
services:
  telegram-bot-api:
    image: aiogram/telegram-bot-api:latest
    environment:
      TELEGRAM_API_ID: "${TELEGRAM_API_ID}"
      TELEGRAM_NEW: "1"
    volumes:
      - data:/var/lib/telegram-bot-api

  kodi-media-bot:
    build: .
    environment:
      KODI_HOST: "${KODI_HOST}"
      NEW_ONE: "${NEW_ONE:-3}"
      KODI_PORT: "${KODI_PORT}"
      NEW_TWO: "x"
    volumes:
      - /a:/b

  caddy-webapp:
    image: caddy:2-alpine
    environment:
      ACME_EMAIL: "${ACME_EMAIL}"

volumes:
  data:
"""

HOST = """\
services:
  jdownloader-2:
    image: jd
    environment:
      - MYJD_USER=me
    restart: always

  telegram-bot-api:
    image: aiogram/telegram-bot-api:latest
    environment:
      TELEGRAM_API_ID: "${TELEGRAM_API_ID}"
    volumes:
      - data:/var/lib/telegram-bot-api

  partyqueue:
    build: ./docker/partyqueue/vOpus
#    build: ./docker/partyqueue/vTesting
    container_name: partyqueue
    environment:
      KODI_HOST: "172.17.0.1"
      KODI_PORT: "${KODI_PORT}"
      HOST_ONLY: "keep"
    volumes:
      - /a:/b
volumes:
  data:
"""


def sync(tmp_path, repo=REPO, host=HOST, mapping="kodi-media-bot=partyqueue"):
    repo_file = tmp_path / "repo.yml"
    host_file = tmp_path / "host.yml"
    repo_file.write_text(repo)
    host_file.write_text(host)
    return subprocess.run(
        ["awk", "-v", f"map={mapping}", "-f", SCRIPT, repo_file, host_file, host_file],
        capture_output=True,
        text=True,
        check=True,
    )


def test_adds_missing_variables_to_every_shared_service(tmp_path):
    res = sync(tmp_path)

    assert res.stdout == HOST.replace(
        '      TELEGRAM_API_ID: "${TELEGRAM_API_ID}"\n',
        '      TELEGRAM_API_ID: "${TELEGRAM_API_ID}"\n      TELEGRAM_NEW: "1"\n',
    ).replace(
        '      HOST_ONLY: "keep"\n',
        '      HOST_ONLY: "keep"\n      NEW_ONE: "${NEW_ONE:-3}"\n      NEW_TWO: "x"\n',
    )
    assert "partyqueue: NEW_ONE hinzugefuegt" in res.stderr
    assert "telegram-bot-api: TELEGRAM_NEW hinzugefuegt" in res.stderr


def test_only_adds_lines(tmp_path):
    """Service names, existing values and host-only entries stay untouched."""
    out = sync(tmp_path).stdout.splitlines()
    host = HOST.splitlines()

    added = [line for line in out if line not in host]
    assert [line.split(":")[0].strip() for line in added] == [
        "TELEGRAM_NEW",
        "NEW_ONE",
        "NEW_TWO",
    ]
    assert [line for line in out if line in host] == host
    assert "kodi-media-bot" not in "\n".join(out)


def test_second_run_changes_nothing(tmp_path):
    once = sync(tmp_path).stdout
    twice = sync(tmp_path, host=once)

    assert twice.stdout == once
    assert "hinzugefuegt" not in twice.stderr


def test_skips_services_missing_on_host(tmp_path):
    res = sync(tmp_path)

    assert "ACME_EMAIL" not in res.stdout
    assert "caddy-webapp: Service fehlt auf dem Host" in res.stderr


def test_unmapped_service_name_is_not_matched(tmp_path):
    res = sync(tmp_path, mapping="")

    assert "NEW_ONE" not in res.stdout
    assert "kodi-media-bot: Service fehlt auf dem Host" in res.stderr


def test_list_form_environment_is_left_alone(tmp_path):
    repo = REPO.replace("  caddy-webapp:", "  jdownloader-2:")
    res = sync(tmp_path, repo=repo)

    assert "ACME_EMAIL" not in res.stdout
    assert "jdownloader-2: environment ist eine Liste" in res.stderr


def test_keeps_host_indentation(tmp_path):
    host = HOST.replace("      KODI", "        KODI").replace(
        "      HOST_ONLY", "        HOST_ONLY"
    )
    res = sync(tmp_path, host=host)

    assert '        NEW_ONE: "${NEW_ONE:-3}"\n' in res.stdout
