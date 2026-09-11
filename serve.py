#!/usr/bin/env python3
"""seochecker dashboard.

    ./venv/bin/python serve.py            then open http://127.0.0.1:8770

Bound to localhost by default, and that default matters: this app fetches any URL
it is given, so anything that can reach it can use this machine to make requests
on its behalf. Put authentication in front of it before changing --host.
"""

import argparse
import sys

from seochecker.web import create_app


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(prog="serve", description=__doc__,
                                     formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("--host", default="127.0.0.1",
                        help="interface to bind (default: localhost only)")
    parser.add_argument("--port", type=int, default=8770)
    parser.add_argument("--db", default=None, metavar="PATH",
                        help="SQLite file of recorded runs, shown under History")
    parser.add_argument("--debug", action="store_true")
    args = parser.parse_args(argv)

    if args.host not in ("127.0.0.1", "localhost", "::1"):
        print(
            f"warning: binding to {args.host} exposes this dashboard beyond this machine.\n"
            "         It will fetch any URL anyone asks it to, so put authentication in\n"
            "         front of it before leaving it running.",
            file=sys.stderr,
        )

    app = create_app(db_path=args.db)
    print(f"seochecker dashboard on http://{args.host}:{args.port}", file=sys.stderr)
    app.run(host=args.host, port=args.port, debug=args.debug, threaded=True)
    return 0


if __name__ == "__main__":
    sys.exit(main())
