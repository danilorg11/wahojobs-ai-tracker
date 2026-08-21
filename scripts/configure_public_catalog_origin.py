"""Create a new nonsecret public-catalog origin configuration file."""

from __future__ import annotations

import argparse
from hashlib import sha256
import json
import os
from pathlib import Path, PurePosixPath
import sys
from urllib.parse import urlsplit


def create_configuration(
    database_path,
    output_path,
    *,
    public_origin,
    deployment_environment,
    bind_port,
    runtime_database_path=None,
):
    database = Path(database_path).resolve(strict=True)
    output = Path(output_path)
    if (
        not database.is_file()
        or not output.is_absolute()
        or output.exists()
        or output.parent.resolve(strict=True) != output.parent
        or deployment_environment not in {"preview", "production"}
        or type(bind_port) is not int
        or not 1024 <= bind_port <= 65535
    ):
        raise ValueError("invalid_configuration_target")
    parsed = urlsplit(public_origin)
    if (
        parsed.scheme != "https"
        or not parsed.netloc
        or parsed.path
        or parsed.query
        or parsed.fragment
        or parsed.username is not None
        or parsed.password is not None
    ):
        raise ValueError("invalid_public_origin")
    if deployment_environment == "preview" and parsed.hostname in {
        "wahojobs.com",
        "www.wahojobs.com",
    }:
        raise ValueError("production_origin_forbidden_in_preview")
    digest = sha256()
    with database.open("rb") as stream:
        for chunk in iter(lambda: stream.read(1024 * 1024), b""):
            digest.update(chunk)
    if runtime_database_path is None:
        runtime_database = str(database)
    else:
        if type(runtime_database_path) is not str:
            raise ValueError("invalid_runtime_database_path")
        runtime_path = PurePosixPath(runtime_database_path)
        if (
            not runtime_path.is_absolute()
            or ".." in runtime_path.parts
            or str(runtime_path) != runtime_database_path
        ):
            raise ValueError("invalid_runtime_database_path")
        runtime_database = str(runtime_path)
    document = {
        "version": 1,
        "deployment_environment": deployment_environment,
        "bind_host": "127.0.0.1",
        "bind_port": bind_port,
        "public_origin": public_origin,
        "database_path": runtime_database,
        "database_sha256": digest.hexdigest(),
    }
    descriptor = os.open(output, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
    with os.fdopen(descriptor, "w", encoding="utf-8", newline="\n") as stream:
        json.dump(document, stream, sort_keys=True, indent=2)
        stream.write("\n")
    return document


def main(argv=None):
    parser = argparse.ArgumentParser()
    parser.add_argument("--database", required=True)
    parser.add_argument("--output", required=True)
    parser.add_argument("--public-origin", required=True)
    parser.add_argument(
        "--deployment-environment", choices=("preview", "production"), required=True
    )
    parser.add_argument("--bind-port", type=int, default=8080)
    parser.add_argument("--runtime-database-path")
    arguments = parser.parse_args(argv)
    try:
        result = create_configuration(
            arguments.database,
            arguments.output,
            public_origin=arguments.public_origin,
            deployment_environment=arguments.deployment_environment,
            bind_port=arguments.bind_port,
            runtime_database_path=arguments.runtime_database_path,
        )
    except (KeyboardInterrupt, SystemExit, GeneratorExit):
        raise
    except Exception:
        print("Public catalog origin configuration failed safely.", file=sys.stderr)
        return 1
    print(
        "PUBLIC_CATALOG_ORIGIN_CONFIGURATION_OK "
        f"environment={result['deployment_environment']} "
        f"database_sha256={result['database_sha256']}"
    )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
