"""Synthetic no-side-effect role inspection. Does not launch POA services."""
import argparse
import json

from poa_runtime.roles import Role, RoleBlocked, asgi_for


def main():
    parser = argparse.ArgumentParser(description="POA offline role preflight only")
    parser.add_argument("--role", required=True, choices=[item.value for item in Role])
    args = parser.parse_args()
    try:
        asgi_for(Role(args.role))
    except RoleBlocked as error:
        parser.exit(77, error.code + "\n")
    print(json.dumps({"role": args.role, "trading_allowed": False,
                      "live_read_allowed": False, "db_attached": False,
                      "server_started": False}, sort_keys=True))


if __name__ == "__main__":
    main()
