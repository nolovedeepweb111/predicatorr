"""Командная строка: python -m predicator <команда>."""

from __future__ import annotations

import argparse
import json
import logging
import sys
import time

from .config import get_settings


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(prog="predicator", description="Прогнозы по Dota-миксерам")
    sub = parser.add_subparsers(dest="cmd", required=True)

    p = sub.add_parser("serve", help="запустить сайт")
    p.add_argument("--host")
    p.add_argument("--port", type=int)
    p.add_argument("--no-sync", action="store_true", help="не обновлять данные в фоне")

    p = sub.add_parser("import", help="загрузить бэкап (файл или URL; по умолчанию — публичный)")
    p.add_argument("source", nargs="?")

    sub.add_parser("sync", help="один проход обновления: бэкап, mixer-cup, ставки")

    p = sub.add_parser("backtest", help="проверка модели без одного кубка")
    p.add_argument("--json", action="store_true")

    p = sub.add_parser("predict", help="прогноз из терминала")
    p.add_argument("team_a")
    p.add_argument("team_b")
    p.add_argument("--tournament", type=int)

    args = parser.parse_args(argv)
    settings = get_settings()
    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(name)s %(message)s")

    if args.cmd == "serve":
        import uvicorn

        from .web import create_app
        app = create_app(settings, start_sync=not args.no_sync)
        uvicorn.run(app, host=args.host or settings.host, port=args.port or settings.port,
                    log_level="info")
        return 0

    from .db import connect
    conn = connect(settings.db_path)

    if args.cmd == "import":
        from .importer import refresh_backup
        print(json.dumps(refresh_backup(conn, args.source or settings.backup_url, force=True),
                         ensure_ascii=False))
        return 0

    if args.cmd == "sync":
        from .sync import sync_once
        print(json.dumps(sync_once(settings), ensure_ascii=False, indent=1))
        return 0

    if args.cmd == "backtest":
        from .backtest import format_report, run_backtest
        from .dataset import load_dataset
        rep = run_backtest(load_dataset(conn), int(time.time()))
        print(json.dumps(rep, ensure_ascii=False, indent=1) if args.json else format_report(rep))
        return 0

    if args.cmd == "predict":
        from .pari import best_team
        from .rosters import default_tournament, load_teams
        from .service import PredictorService
        tid = args.tournament or default_tournament(conn)
        if tid is None:
            print("нет турниров с составами", file=sys.stderr)
            return 1
        names = {t.key: t.name for t in load_teams(conn, tid)}
        keys = []
        for q in (args.team_a, args.team_b):
            key, _ = best_team(q, names, threshold=0.5)
            if key is None:
                print(f"команда «{q}» не найдена", file=sys.stderr)
                return 1
            keys.append(key)
        svc = PredictorService(settings)
        res = svc.predict(conn, tid, keys[0], keys[1])
        a, b = res["teams"]["a"], res["teams"]["b"]
        print(f"{a['name']} — {b['name']}: {res['p_a']:.1%} / {1 - res['p_a']:.1%}"
              f"  (честные коэф. {res['fair_odds']['a']} / {res['fair_odds']['b']})")
        return 0
    return 1


if __name__ == "__main__":
    sys.exit(main())
