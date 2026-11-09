import os
import gzip
import hashlib
import subprocess
import tempfile
import argparse
from pathlib import Path
from datetime import datetime, timezone
from getpass import getpass

import boto3
from dotenv import load_dotenv

load_dotenv()

# AWS configuration
AWS_REGION = os.getenv("AWS_REGION", "ap-southeast-2")
S3_BUCKET = os.environ["S3_BUCKET"]
S3_PREFIX = os.getenv(
    "S3_PREFIX", "cloudrecover/backups"
).strip("/")

# Default database configuration from .env
DEFAULT_CONTAINER = os.getenv(
    "CONTAINER_NAME", "cloudrecover-mysql"
)
DEFAULT_DB_NAME = os.getenv("DB_NAME", "student_db")
DEFAULT_DB_USER = os.getenv("DB_USER", "backup_user")
DEFAULT_DB_PASSWORD = os.getenv("DB_PASSWORD", "")


def parse_arguments():
    parser = argparse.ArgumentParser(
        description="Back up a MySQL Docker container to Amazon S3."
    )

    parser.add_argument(
        "--container",
        default=DEFAULT_CONTAINER,
        help="Name of the MySQL Docker container.",
    )
    parser.add_argument(
        "--database",
        default=DEFAULT_DB_NAME,
        help="Name of the database to back up.",
    )
    parser.add_argument(
        "--user",
        default=DEFAULT_DB_USER,
        help="MySQL username.",
    )
    parser.add_argument(
        "--password-prompt",
        action="store_true",
        help="Prompt securely for the MySQL password.",
    )

    return parser.parse_args()


def create_backup(container_name, db_name, db_user, db_password):
    timestamp = datetime.now(timezone.utc).strftime(
        "%Y%m%dT%H%M%SZ"
    )

    with tempfile.TemporaryDirectory() as temp_dir:
        temp = Path(temp_dir)
        sql_file = temp / "backup.sql"
        compressed_file = temp / "backup.sql.gz"

        print(f"Container: {container_name}")
        print(f"Database: {db_name}")
        print("Creating MySQL dump...")

        result = subprocess.run(
            [
                "docker",
                "exec",
                "-e",
                "MYSQL_PWD=" + db_password,
                container_name,
                "mysqldump",
                "-h",
                "127.0.0.1",
                "-u",
                db_user,
                "--single-transaction",
                "--quick",
                "--routines",
                "--triggers",
                "--events",
                db_name,
            ],
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            check=False,
        )

        if result.returncode != 0:
            error = result.stderr.decode(
                "utf-8", errors="replace"
            )
            raise RuntimeError(
                f"MySQL dump failed:\n{error}"
            )

        if not result.stdout:
            raise RuntimeError("The SQL dump is empty.")

        sql_file.write_bytes(result.stdout)
        print("MySQL dump created successfully.")

        print("Compressing backup...")

        with sql_file.open("rb") as source:
            with gzip.open(compressed_file, "wb") as target:
                while chunk := source.read(1024 * 1024):
                    target.write(chunk)

        digest = hashlib.sha256()

        with compressed_file.open("rb") as file:
            while chunk := file.read(1024 * 1024):
                digest.update(chunk)

        checksum = digest.hexdigest()

        s3_key = (
            f"{S3_PREFIX}/{db_name}/"
            f"backup_{timestamp}.sql.gz"
            if S3_PREFIX
            else f"{db_name}/backup_{timestamp}.sql.gz"
        )
        checksum_key = s3_key + ".sha256"

        s3 = boto3.client(
            "s3",
            region_name=AWS_REGION,
        )

        print(f"Uploading s3://{S3_BUCKET}/{s3_key}")

        s3.upload_file(
            str(compressed_file),
            S3_BUCKET,
            s3_key,
            ExtraArgs={
                "ServerSideEncryption": "AES256"
            },
        )

        s3.put_object(
            Bucket=S3_BUCKET,
            Key=checksum_key,
            Body=f"{checksum}  {s3_key}\n".encode(),
            ServerSideEncryption="AES256",
        )

        print("\nBACKUP COMPLETED SUCCESSFULLY")
        print(f"Container: {container_name}")
        print(f"Database: {db_name}")
        print(f"S3 location: s3://{S3_BUCKET}/{s3_key}")
        print(
            "Compressed size: "
            f"{compressed_file.stat().st_size} bytes"
        )
        print(f"SHA-256: {checksum}")


if __name__ == "__main__":
    try:
        args = parse_arguments()

        password = (
            getpass("MySQL password: ")
            if args.password_prompt
            else DEFAULT_DB_PASSWORD
        )

        if not password:
            raise ValueError(
                "No MySQL password configured. Set DB_PASSWORD "
                "in .env or use --password-prompt."
            )

        create_backup(
            container_name=args.container,
            db_name=args.database,
            db_user=args.user,
            db_password=password,
        )

    except Exception as exc:
        print(f"BACKUP FAILED: {exc}")
        raise SystemExit(1)
