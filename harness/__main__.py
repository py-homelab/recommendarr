import argparse

from . import db


def main() -> None:
    parser = argparse.ArgumentParser(prog="harness")
    parser.add_argument("command", choices=["pull", "resolve", "split", "catalogue", "eval", "tune"])
    parser.add_argument("grid", nargs="?", default="content", help="tune: which grid to run")
    args = parser.parse_args()

    con = db.connect()
    if args.command == "pull":
        from . import pull

        pull.run(con)
    elif args.command == "resolve":
        from . import resolve

        resolve.run(con)
    elif args.command == "split":
        import time

        from . import engagement, split

        engagement.run(con)
        split.run(con, int(time.time()))
    elif args.command == "catalogue":
        from . import catalogue

        catalogue.run(con)
    elif args.command == "eval":
        from . import baselines, content, eval as evaluation, graph, movielens, signals, tune

        reqs = signals.requests_by_user(con)
        scorers = [cls() for cls in baselines.BASELINES] + [
            graph.GraphPPR(name="G1_graph_ppr"),
            content.ContentSeedKNN(name="C1_content_knn"),
            movielens.MovieLensEASE(name="M1_movielens_ease"),
            tune.final_blend(),
            signals.IntentSeeds(tune.final_blend(), reqs),
            signals.FamilyFilter(signals.IntentSeeds(tune.final_blend(), reqs)),
        ]
        evaluation.run(con, scorers)
    elif args.command == "tune":
        from . import tune

        tune.run(con, args.grid)


if __name__ == "__main__":
    main()
