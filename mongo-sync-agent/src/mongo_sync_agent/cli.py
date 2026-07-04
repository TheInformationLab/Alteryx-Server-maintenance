import argparse, sys

def main(argv=None):
    parser = argparse.ArgumentParser(
        prog="msa",
        description="Mongo Sync Agent - incremental MongoDB -> S3 extraction agent."
    )
    parser.add_argument("--config", "-c", required=True, metavar="PATH",
                        help="Path to the TOML configuration file.")
    parser.add_argument("--only", nargs="+", choices=["mongo", "logs", "hostmetrics"],
                        metavar="MODULE",
                        help="Run only the specified module(s). Default: all enabled modules.")
    parser.add_argument("--collection", "-C", metavar="NAME",
                        help="Only extract this collection (mongo module only).")
    parser.add_argument("--dry-run", action="store_true",
                        help="Process data but do not upload to S3 or advance watermarks.")
    parser.add_argument("--version", action="version", version="%(prog)s 0.1.0")

    args = parser.parse_args(argv)

    only = set(args.only) if args.only else None

    try:
        from .config import load_config, ConfigError
        from .runner import run_cycle
        cfg = load_config(args.config)
        exit_code = run_cycle(cfg, only=only, collection=args.collection, dry_run=args.dry_run)
        sys.exit(exit_code)
    except Exception as e:
        print(f"Error: {e}", file=sys.stderr)
        sys.exit(2)

if __name__ == "__main__":
    main()
