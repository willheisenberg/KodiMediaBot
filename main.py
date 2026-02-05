import os

import telegram_ui


def main():
    token = os.environ["TG_TOKEN"]
    telegram_ui.run(token)


if __name__ == "__main__":
    main()
