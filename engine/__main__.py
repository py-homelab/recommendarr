import argparse

from harness import db


def main() -> None:
    parser = argparse.ArgumentParser(prog="engine")
    parser.add_argument("command", choices=["build", "serve"])
    args = parser.parse_args()
    if args.command == "build":
        from . import build, service

        build.build(db.connect(), service.VERSION)
    else:
        from . import service

        service.serve()


if __name__ == "__main__":
    main()
