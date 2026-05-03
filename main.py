import logging
import sys

from kodibot.config import CFG
from kodibot.telegram import ui


def main():
    level = logging.DEBUG if CFG.debug_ws else logging.INFO
    logging.basicConfig(
        level=level,
        format="%(asctime)s %(levelname)-5s %(name)s: %(message)s",
        datefmt="%Y-%m-%d %H:%M:%S",
        stream=sys.stdout,
    )
    logging.getLogger("httpx").setLevel(logging.WARNING)
    logging.getLogger("telegram").setLevel(logging.WARNING)
    logging.getLogger("websockets").setLevel(logging.WARNING)

    ui.run(CFG.tg_token)


if __name__ == "__main__":
    main()
