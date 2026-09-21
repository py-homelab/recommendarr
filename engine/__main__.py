import argparse
import sqlite3
import sys


def main() -> None:
    parser = argparse.ArgumentParser(prog="engine")
    parser.add_argument("command", choices=["build", "serve", "check-groups"])
    args = parser.parse_args()
    if args.command == "check-groups":
        sys.exit(check_groups())

    from harness import db

    if args.command == "build":
        from . import build, service

        build.build(db.connect(), service.VERSION)
    else:
        from . import service

        service.serve()


def check_groups() -> int:
    """Validate ENGINE_IDENTITY_GROUPS exactly as the service will at start, and print what it means.

    For proving a value in a throwaway container before it reaches the real one. It starts no build,
    creates no database and changes no table. Exit 0 for a valid (or absent) map, 2 for a malformed
    one, 3 for a well-formed map the nightly build would REFUSE (a canonical account Tautulli has
    never reported — see `build.check_identity`; only detectable with the data directory mounted).
    Mount the data directory READ-ONLY: like every engine command, importing the engine makes a `tmp/`
    folder under a writable data directory when SQLITE_TMPDIR is unusable, and SQLite may leave
    `-shm`/`-wal` files beside a database it opens in a writable directory.

    With a data directory mounted it also reports ids Tautulli has never reported — warnings, exit
    still 0. That read is best-effort: a read-only mount of a database nobody else has open cannot
    always be read (SQLite wants to create its -shm file beside it), and then the ids simply go
    unchecked, with a line saying so, rather than a valid map exiting non-zero.
    """
    from harness import config

    from . import identity

    try:
        identity.members()
    except ValueError as e:
        print(f"INVALID: {e}", file=sys.stderr)
        return 2
    text, warnings = identity.check(None)
    if identity.members() and config.DB_PATH.exists():
        text, warnings = _checked_against_the_database(identity, config.DB_PATH)
    elif identity.members():
        warnings = [f"no database at {config.DB_PATH}, so the ids were not checked against Tautulli's users"]
    print(text)
    for w in warnings:
        print(f"WARNING: {w}")
    return 3 if any("(canonical)" in w for w in warnings) else 0


def _checked_against_the_database(identity, path) -> tuple[str, list[str]]:
    # `mode=ro` first: correct while the service has the database open. `immutable=1` is the fallback
    # for a read-only mount with no -shm beside it, where `mode=ro` fails on the first SELECT.
    for uri in (f"file:{path}?mode=ro", f"file:{path}?immutable=1"):
        con = None
        try:
            con = sqlite3.connect(uri, uri=True)
            return identity.check(con)
        except sqlite3.Error as e:
            failure = e
        finally:
            if con is not None:
                con.close()
    text, _ = identity.check(None)
    return text, [f"could not read the users table ({failure}), so the ids were not checked"]


if __name__ == "__main__":
    main()
