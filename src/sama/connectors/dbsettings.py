"""The checked form of a database connection (docs/architecture.md, Sections 10.4 and 10.6).

``DbSettings.parse`` reads the form values of a connection of any engine. It does not import
a driver.
"""

from __future__ import annotations

import base64
import binascii
import hashlib
import ipaddress
import json
import re
from collections.abc import Mapping
from dataclasses import dataclass, field
from typing import Final, Literal

from sama.connectors.dbforms import MAX_BYTES_BILLED, SPECS, Engine
from sama.connectors.http_api import DriverError
from sama.connectors.manifest import Manifest
from sama.connectors.sql import (
    BIGQUERY,
    CLICKHOUSE,
    MYSQL,
    POSTGRES,
    SNOWFLAKE,
    TSQL,
    Dialect,
    Policy,
    parse_names,
    policy_of,
)

TlsMode = Literal["disable", "require", "verify-full"]
DEFAULT_PORT: Final = 5432

_HOST: Final = re.compile(r"^[a-z0-9]([a-z0-9-]{0,62})(\.[a-z0-9]([a-z0-9-]{0,62}))*$")
_PG_NAME: Final = re.compile(r"^[a-z_][a-z0-9_$]{0,62}$")
_NAME: Final = re.compile(r"^[A-Za-z0-9_$][A-Za-z0-9_$-]{0,127}$")
"""A name of the other engines (the case is kept)."""
_COLLECTION: Final = re.compile(r"^[A-Za-z0-9_][A-Za-z0-9_.-]{0,119}$")
_PROJECT: Final = re.compile(r"^[a-z][a-z0-9-]{4,28}[a-z0-9]$")
_ACCOUNT: Final = re.compile(r"^[A-Za-z0-9][A-Za-z0-9_.-]{0,200}$")
_IDENT: Final = re.compile(r"^[A-Za-z_][A-Za-z0-9_$]{0,254}$")
_LOCATION: Final = re.compile(r"^[A-Za-z0-9-]{1,40}$")
_ON: Final = frozenset({"on", "yes", "true", "1"})
_OFF: Final = frozenset({"", "off", "no", "false", "0"})
_PEM: Final = re.compile(r"-----BEGIN ([A-Z0-9 ]+)-----(.*?)-----END \1-----", re.DOTALL)
_HOST_KEY: Final = re.compile(
    r"^(ssh-[a-z0-9-]+|ecdsa-sha2-nistp\d+|sk-[a-z0-9@.-]+) [A-Za-z0-9+/=]+"
)
_FINGERPRINT: Final = re.compile(r"^SHA256:[A-Za-z0-9+/]{43}=?$")

DIALECTS: Final[dict[Engine, Dialect]] = {
    "postgres": POSTGRES,
    "mysql": MYSQL,
    "mariadb": MYSQL,
    "mssql": TSQL,
    "clickhouse": CLICKHOUSE,
    "bigquery": BIGQUERY,
    "snowflake": SNOWFLAKE,
    "mongodb": POSTGRES,  # Not used: MongoDB has no SQL.
}
TOKEN_URI: Final = "https://oauth2.googleapis.com/token"  # noqa: S105 (a URL)
"""The Google token endpoint. The ``token_uri`` of a key file is not used."""


@dataclass(frozen=True, slots=True)
class SshSettings:
    """The SSH tunnel of a connection (Section 10.6). ``private_key`` never goes to a log."""

    host: str
    port: int
    user: str
    private_key: str
    host_key: str
    """``<type> <base64>`` or ``SHA256:<fingerprint>``."""

    def fingerprint(self) -> str:
        data = json.dumps([self.host, self.port, self.user, self.private_key, self.host_key])
        return hashlib.sha256(data.encode()).hexdigest()


@dataclass(frozen=True, slots=True)
class DbSettings:
    """The checked form of one database connection. ``password`` never goes to a log.

    - ``host`` and ``port``: the database server. For BigQuery and Snowflake, the API host.
    - ``database``: the database, or the project (BigQuery).
    - ``user`` and ``password``: the login. For BigQuery, the service account e-mail and its
      private key. For Snowflake, the user and the private key.
    - ``options``: the values of one engine (for example the BigQuery dataset).
    - ``ssh``: the SSH tunnel, or ``None``.
    """

    host: str
    port: int
    database: str
    user: str
    password: str
    sslmode: TlsMode
    writes: bool
    policy: Policy
    engine: Engine = "postgres"
    options: Mapping[str, str] = field(default_factory=dict)
    ssh: SshSettings | None = None

    @property
    def dialect(self) -> Dialect:
        return DIALECTS[self.engine]

    @classmethod
    def parse(cls, manifest: Manifest, values: Mapping[str, str]) -> DbSettings:
        """Raises ``DriverError`` with the field that is not valid."""
        block = manifest.database
        engine: Engine = block.engine if block is not None else "postgres"
        default_mode = block.sslmode if block is not None else "require"
        spec = SPECS[engine]
        dialect = DIALECTS[engine]
        writes = _switch(values.get("writes"), "Allow writes")
        ddl = _switch(values.get("ddl"), "Allow schema changes")
        if ddl and not writes:
            raise DriverError("Allow schema changes needs Allow writes on")
        if engine == "mongodb" and (writes or ddl):
            raise DriverError("a MongoDB connection is read only")
        if engine == "bigquery":
            return _bigquery(values, writes, ddl)
        if engine == "snowflake":
            return _snowflake(values, writes, ddl)
        host = _host(values.get("host"))
        port = _port(values.get("port"), spec.default_port or DEFAULT_PORT, "the port")
        database = (values.get("database") or "").strip()
        user = (values.get("user") or "").strip()
        if engine == "clickhouse":
            database = database or "default"
        if not database or (not user and engine != "mongodb"):
            raise DriverError("the database and the user are required")
        if engine != "postgres" and not _NAME.match(database):
            raise DriverError("the database is not a valid name")
        ssh = _ssh(values)
        mode = (values.get("sslmode") or "").strip().lower() or default_mode
        sslmode: TlsMode
        if mode == "require":
            sslmode = "require"
        elif mode == "verify-full":
            sslmode = "verify-full"
        elif mode == "disable" and ssh is not None:
            sslmode = "disable"
        else:
            raise DriverError(
                "the TLS mode must be require or verify-full (disable needs an SSH tunnel)"
            )
        options: dict[str, str] = {}
        if engine == "mongodb":
            source = (values.get("auth_source") or "").strip() or "admin"
            if not _NAME.match(source):
                raise DriverError("the authentication database is not a valid name")
            options["auth_source"] = source
            policy = _mongo_policy(values)
        else:
            fixed: tuple[str, ...] | None = None
            catalog: str | None = None
            if engine in ("mysql", "mariadb", "clickhouse"):
                fixed = (database,)
            elif engine == "mssql":
                fixed, catalog = (), database
            policy = _sql_policy(values, dialect, ddl, fixed, catalog)
        return cls(
            host=host,
            port=port,
            database=database,
            user=user,
            password=values.get("password") or "",
            sslmode=sslmode,
            writes=writes,
            policy=policy,
            engine=engine,
            options=options,
            ssh=ssh,
        )

    def fingerprint(self) -> str:
        """A hash of all values. A changed form gives a new pool."""
        data = json.dumps(
            [
                self.engine,
                self.host,
                self.port,
                self.database,
                self.user,
                self.password,
                self.sslmode,
                self.writes,
                sorted(self.policy.schemas),
                sorted(map(str, self.policy.tables)),
                sorted(map(str, self.policy.hidden)),
                self.policy.ddl,
                sorted(self.options.items()),
                self.ssh.fingerprint() if self.ssh is not None else None,
            ]
        )
        return hashlib.sha256(data.encode()).hexdigest()


def writes_on(fields: Mapping[str, str]) -> bool:
    """True if the ``writes`` field of a connection is on. A value that is not valid is off."""
    return (fields.get("writes") or "").strip().lower() in _ON


def _switch(value: str | None, label: str) -> bool:
    text = (value or "").strip().lower()
    if text in _ON:
        return True
    if text in _OFF:
        return False
    raise DriverError(f"{label} must be on or off")


def _host(value: str | None, label: str = "the host") -> str:
    host = (value or "").strip().lower().rstrip(".")
    if host.startswith("[") and host.endswith("]"):
        host = host[1:-1]
    if not host:
        raise DriverError(f"{label} is empty")
    try:
        ipaddress.ip_address(host)
    except ValueError:
        if not _HOST.match(host):
            raise DriverError(f"{label} must be a host name or an IP address, not a URL") from None
    return host


def _port(value: str | None, default: int, label: str) -> int:
    text = (value or "").strip()
    try:
        port = int(text) if text else default
    except ValueError:
        raise DriverError(f"{label} must be a number") from None
    if not 0 < port < 65_536:
        raise DriverError(f"{label} must be 1 to 65535")
    return port


def _sql_policy(
    values: Mapping[str, str],
    dialect: Dialect,
    ddl: bool,
    fixed: tuple[str, ...] | None,
    catalog: str | None,
) -> Policy:
    schemas = parse_names(values.get("schemas") or "", dialect)
    tables = parse_names(values.get("tables") or "", dialect)
    hidden = parse_names(values.get("hidden_columns") or "", dialect)
    pattern = _PG_NAME if dialect is POSTGRES else _NAME
    for label, items, parts in (
        ("Allowed schemas", schemas, 1),
        ("Allowed tables", tables, 2),
        ("Hidden columns", hidden, 3),
    ):
        for item in items:
            names = item.split(".")
            if len(names) > parts or not all(pattern.match(n) for n in names):
                raise DriverError(f"{label}: {item[:60]} is not a valid name")
    if fixed is not None:
        fixed = tuple(dialect.fold_name(n) for n in fixed)
    if catalog is not None:
        catalog = dialect.fold_name(catalog)
    return policy_of(
        schemas=schemas,
        tables=tables,
        hidden_columns=hidden,
        ddl=ddl,
        fixed_path=fixed,
        catalog=catalog,
    )


def _mongo_policy(values: Mapping[str, str]) -> Policy:
    collections = parse_names(values.get("tables") or "", MYSQL)
    hidden = parse_names(values.get("hidden_columns") or "", MYSQL)
    for item in collections:
        if not _COLLECTION.match(item) or item.startswith("system."):
            raise DriverError(f"Allowed collections: {item[:60]} is not a valid name")
    pairs: set[tuple[str | None, str]] = set()
    for item in hidden:
        owner, _, name = item.rpartition(".")
        if not name or not _NAME.match(name) or (owner and not _COLLECTION.match(owner)):
            raise DriverError(f"Hidden fields: {item[:60]} is not a valid name")
        pairs.add((owner.lower() or None, name.lower()))
    return Policy(
        tables=frozenset((None, c) for c in collections),
        hidden=frozenset(pairs),
        fixed_path=(),
    )


def pem(text: str, label: str) -> str:
    """A PEM key with its line breaks. A form field can give the key on one line, with spaces
    for the line breaks."""
    text = text.strip().replace("\\n", "\n").replace("\r\n", "\n")
    found = _PEM.search(text)
    if found is None:
        raise DriverError(f"the {label} is not a PEM key (-----BEGIN ... KEY-----)")
    kind, body = found.group(1), "".join(found.group(2).split())
    try:
        base64.b64decode(body, validate=True)
    except (binascii.Error, ValueError):
        raise DriverError(f"the {label} is not a valid PEM key") from None
    lines = [body[i : i + 64] for i in range(0, len(body), 64)]
    return "\n".join([f"-----BEGIN {kind}-----", *lines, f"-----END {kind}-----", ""])


def _ssh(values: Mapping[str, str]) -> SshSettings | None:
    host_text = (values.get("ssh_host") or "").strip()
    if not host_text:
        if any((values.get(n) or "").strip() for n in ("ssh_user", "ssh_private_key")):
            raise DriverError("the SSH host is empty, but other SSH fields are set")
        return None
    host = _host(host_text, "the SSH host")
    port = _port(values.get("ssh_port"), 22, "the SSH port")
    user = (values.get("ssh_user") or "").strip()
    if not user or len(user) > 64 or any(c.isspace() for c in user):
        raise DriverError("the SSH user is required")
    key_text = values.get("ssh_private_key") or ""
    if not key_text.strip():
        raise DriverError("the SSH private key is required for an SSH tunnel")
    host_key = " ".join((values.get("ssh_host_key") or "").split())
    if not host_key:
        raise DriverError("the SSH host key is required for an SSH tunnel")
    if not (_HOST_KEY.match(host_key) or _FINGERPRINT.match(host_key)):
        raise DriverError("the SSH host key must be '<type> <base64>' or 'SHA256:<fingerprint>'")
    if not _FINGERPRINT.match(host_key):
        host_key = " ".join(host_key.split(" ")[:2])
    return SshSettings(host, port, user, pem(key_text, "SSH private key"), host_key)


def _bigquery(values: Mapping[str, str], writes: bool, ddl: bool) -> DbSettings:
    project = (values.get("project") or "").strip()
    if not _PROJECT.match(project):
        raise DriverError("the project must be a Google Cloud project ID")
    dataset = (values.get("dataset") or "").strip()
    if dataset and not re.match(r"^[A-Za-z0-9_]{1,1024}$", dataset):
        raise DriverError("the default dataset is not a valid name")
    location = (values.get("location") or "").strip()
    if location and not _LOCATION.match(location):
        raise DriverError("the location is not valid (for example US or EU)")
    limit_text = (values.get("max_bytes_billed") or "").replace(",", "").replace("_", "").strip()
    try:
        limit = int(limit_text) if limit_text else MAX_BYTES_BILLED
    except ValueError:
        raise DriverError("the maximum bytes billed must be a number") from None
    if limit < 10_485_760:
        raise DriverError("the maximum bytes billed must be at least 10485760 (10 MB)")
    try:
        key = json.loads(values.get("service_account_json") or "")
    except ValueError:
        raise DriverError("the service account key is not JSON") from None
    if not isinstance(key, dict) or key.get("type") != "service_account":
        raise DriverError("the service account key must be a JSON key of a service account")
    email = key.get("client_email")
    if not isinstance(email, str) or "@" not in email or len(email) > 200:
        raise DriverError("the service account key has no client_email")
    private = key.get("private_key")
    if not isinstance(private, str):
        raise DriverError("the service account key has no private_key")
    policy = _sql_policy(values, BIGQUERY, ddl, (dataset,) if dataset else (), project)
    return DbSettings(
        host="bigquery.googleapis.com",
        port=443,
        database=project,
        user=email,
        password=pem(private, "service account private key"),
        sslmode="verify-full",
        writes=writes,
        policy=policy,
        engine="bigquery",
        options={
            "dataset": dataset,
            "location": location,
            "max_bytes_billed": str(limit),
            "key_id": str(key.get("private_key_id") or "")[:100],
        },
    )


def _snowflake(values: Mapping[str, str], writes: bool, ddl: bool) -> DbSettings:
    account = (values.get("account") or "").strip()
    account = account.removesuffix(".snowflakecomputing.com")
    if not _ACCOUNT.match(account):
        raise DriverError("the account must be an account identifier, for example myorg-acct")
    host = f"{account.lower().replace('_', '-')}.snowflakecomputing.com"
    if not _HOST.match(host):
        raise DriverError("the account must be an account identifier, for example myorg-acct")
    user = (values.get("user") or "").strip()
    if not user or len(user) > 255:
        raise DriverError("the user is required")
    options: dict[str, str] = {"account": account}
    for name, label, required in (
        ("warehouse", "warehouse", True),
        ("database", "database", True),
        ("schema", "schema", True),
        ("role", "role", False),
    ):
        value = (values.get(name) or "").strip()
        if not value and not required:
            options[name] = ""
            continue
        if not _IDENT.match(value):
            raise DriverError(f"the {label} must be a name without quotes")
        options[name] = value.upper()
    policy = _sql_policy(values, SNOWFLAKE, ddl, (options["schema"],), options["database"])
    return DbSettings(
        host=host,
        port=443,
        database=options["database"],
        user=user,
        password=pem(values.get("private_key") or "", "private key"),
        sslmode="verify-full",
        writes=writes,
        policy=policy,
        engine="snowflake",
        options=options,
    )
