"""Read-only compatibility command; safe to import during pytest collection."""

if __name__ == "__main__":
    from trading_bot.cli import main

    raise SystemExit(main(["check-alpaca"]))
