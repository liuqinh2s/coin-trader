from infra.env import SHORT_STRATEGY

if __name__ == '__main__':
    if SHORT_STRATEGY:
        from core.short_strategy import main_short
        main_short()
    else:
        from core.live_trading import main
        main()
